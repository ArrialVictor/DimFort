"""DimFort language server.

Speaks LSP over stdio. On ``initialize`` the server picks up the
workspace folders and scans them for Fortran sources; thereafter every
relevant event re-runs the pipeline over the whole workset so that
``use mod_other`` resolves and cross-file H004 lights up in the editor
exactly as it does on the command line.

Triggers:

- ``textDocument/didOpen`` and ``didSave``: immediate check.
- ``textDocument/didChange``: debounced live check (in-memory buffer
  text is passed to the pipeline so unsaved edits are honoured).
- ``textDocument/didClose``: clear that file's diagnostics.

Provides:

- ``textDocument/publishDiagnostics`` — H-series + U-series.
- ``textDocument/hover`` — resolved unit for the variable or
  derived-type member under the cursor.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from dimfort import __version__
from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401  populates DEFAULT_TABLE
from dimfort.core import units as _units_mod
from dimfort.core.checker import (
    FuncSig,
    _Resolver,
    collect_intrinsic_names,
)
from dimfort.core.diagnostics import Diagnostic, Severity
from dimfort.core.lfortran import walk
from dimfort.core.multifile import WorksetResult, check_files
from dimfort.core.units import Unit, equal_dim, format_unit
from dimfort.core.units import base_symbols as _base_symbols

log = logging.getLogger("dimfort.lsp")

server = LanguageServer("dimfort", __version__)


# ---------------------------------------------------------------------------
# Feature toggles (set from initializationOptions; off-by-default flags
# would surprise users, so everything defaults on).
# ---------------------------------------------------------------------------


class _FeatureToggles:
    inlay_hints: bool = True
    completion: bool = True
    code_actions: bool = True
    goto_definition: bool = True
    code_lens: bool = False    # opt-in; can clutter dense files


_features = _FeatureToggles()


_FORTRAN_EXTS = {
    ".f90", ".F90", ".f95", ".F95",
    ".f03", ".F03", ".f08", ".F08",
}

_SEVERITY_TO_LSP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}

# Debounce for `didChange`: keep a per-URI monotonically increasing
# version. A scheduled re-check checks the version under the lock
# before actually running, so a burst of keystrokes only runs the
# last one.
_doc_versions: dict[str, int] = {}
_doc_versions_lock = threading.Lock()

# Last successful check result, used for hover.
_last_result: WorksetResult | None = None
_last_result_lock = threading.Lock()

# Workspace folders, captured at initialise time.
_workspace_folders: list[Path] = []

# Tracks every file VSCode (or whichever client) has currently open.
# Keyed by resolved Path so we can recover the *exact* URI the editor
# uses, even when its normalisation differs from ours (symlinks, case,
# percent-encoding). Publishing back to the editor's URI is what makes
# squiggles actually appear.
_opened_uris: dict[Path, str] = {}
_opened_uris_lock = threading.Lock()


def _remember_uri(uri: str) -> None:
    p = _uri_to_path(uri)
    if p is None:
        return
    try:
        resolved = p.resolve()
    except OSError:
        return
    with _opened_uris_lock:
        _opened_uris[resolved] = uri


def _forget_uri(uri: str) -> None:
    p = _uri_to_path(uri)
    if p is None:
        return
    try:
        resolved = p.resolve()
    except OSError:
        return
    with _opened_uris_lock:
        _opened_uris.pop(resolved, None)


def _uri_for_path(path: Path) -> str:
    """Prefer the editor's original URI for a known-open file.

    Falls back to ``Path.as_uri()`` for files the editor hasn't opened
    yet (cross-file diagnostics on closed files).
    """
    with _opened_uris_lock:
        known = _opened_uris.get(path)
    if known is not None:
        return known
    return path.as_uri()


# ---------------------------------------------------------------------------
# URI / position helpers
# ---------------------------------------------------------------------------


def _uri_to_path(uri: str) -> Path | None:
    if not uri.startswith("file:"):
        return None
    return Path(unquote(urlparse(uri).path))


def _to_lsp_diagnostic(d: Diagnostic) -> lsp.Diagnostic:
    start_line = max(d.start.line - 1, 0)
    start_col = max(d.start.column - 1, 0)
    end_line = max(d.end.line - 1, 0)
    end_col = max(d.end.column - 1, 0)
    if (end_line, end_col) <= (start_line, start_col):
        end_col = start_col + 1
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=start_line, character=start_col),
            end=lsp.Position(line=end_line, character=end_col),
        ),
        severity=_SEVERITY_TO_LSP.get(d.severity, lsp.DiagnosticSeverity.Error),
        code=d.code,
        source="DimFort",
        message=d.message,
    )


# ---------------------------------------------------------------------------
# Workspace traversal
# ---------------------------------------------------------------------------


def _discover_fortran_files(roots: list[Path]) -> list[Path]:
    """Walk every workspace folder and collect Fortran sources."""
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in _FORTRAN_EXTS:
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
    return out


def _workset_for(ls: LanguageServer, active_uri: str) -> tuple[list[Path], Path | None]:
    """Return the workset of paths plus the active path (if known).

    Always includes the active file even if it lives outside any
    workspace folder (e.g. the user opened a loose ``.f90``).
    """
    active = _uri_to_path(active_uri)
    paths = _discover_fortran_files(_workspace_folders)
    if active is not None and active.is_file():
        resolved = active.resolve()
        if resolved not in paths:
            paths.append(resolved)
    return paths, active


# ---------------------------------------------------------------------------
# Diagnostic publication
# ---------------------------------------------------------------------------


def _publish_for_uri(ls: LanguageServer, uri: str, *, override_text: str | None = None) -> None:
    paths, active = _workset_for(ls, uri)
    if active is None:
        return
    overrides: dict[Path, str] = {}
    if override_text is not None:
        overrides[active.resolve()] = override_text

    try:
        result = check_files(paths, overrides=overrides)
    except lf.LFortranNotFound as exc:
        log.warning("lfortran not found: %s", exc)
        return
    except Exception:
        log.exception("dimfort pipeline crashed on %s", active)
        return

    with _last_result_lock:
        global _last_result
        _last_result = result

    # Publish per-file. Files that produced no diagnostics still get an
    # empty publish, so stale squiggles clear immediately.
    for path in paths:
        diags = result.diagnostics.get(path, [])
        try:
            file_uri = _uri_for_path(path)
        except ValueError:
            continue
        ls.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(
                uri=file_uri,
                diagnostics=[_to_lsp_diagnostic(d) for d in diags],
            )
        )


def _bump_version(uri: str) -> int:
    with _doc_versions_lock:
        _doc_versions[uri] = _doc_versions.get(uri, 0) + 1
        return _doc_versions[uri]


def _is_current(uri: str, version: int) -> bool:
    with _doc_versions_lock:
        return _doc_versions.get(uri) == version


# ---------------------------------------------------------------------------
# Hover: variable-unit lookup
# ---------------------------------------------------------------------------


def _walk_var_nodes(tree: dict):
    """Yield every ASR reference to a variable.

    ``Variable`` is the declaration site; ``Var`` is each *use*. Hover
    should fire on both, so we yield them in one stream with their
    bare-name field normalised to ``"name"``.
    """
    for n in walk(tree):
        if not isinstance(n, dict):
            continue
        kind = n.get("node")
        if kind == "Variable":
            yield n, n.get("fields", {}).get("name", "")
        elif kind == "Var":
            v = n.get("fields", {}).get("v", "")
            yield n, v.split(" ", 1)[0] if isinstance(v, str) else ""


def _walk_member_nodes(tree: dict):
    for n in walk(tree):
        if isinstance(n, dict) and n.get("node") == "StructInstanceMember":
            yield n


def _loc_contains(
    loc: dict | None,
    line_1based: int,
    col_1based: int,
    expected_basename: str | None = None,
) -> bool:
    if not isinstance(loc, dict):
        return False
    # Multi-file worksets: ASR drags in nodes from `use`d modules whose
    # loc points at the *other* file. Filter by filename to avoid
    # hovering on `side` (in geo.f90) when the cursor is on `s` (in
    # main.f90).
    if expected_basename is not None:
        fn = loc.get("first_filename")
        if isinstance(fn, str) and Path(fn).name != expected_basename:
            return False
    sl = loc.get("first_line")
    sc = loc.get("first_column")
    el = loc.get("last_line")
    ec = loc.get("last_column")
    if not all(isinstance(v, int) for v in (sl, sc, el, ec)):
        return False
    if line_1based < sl or line_1based > el:
        return False
    if line_1based == sl and col_1based < sc:
        return False
    return not (line_1based == el and col_1based > ec)


def _resolve_hover(
    uri: str,
    line_1based: int,
    col_1based: int,
    source_text: str | None,
) -> str | None:
    """Return formatted hover text, or None if nothing useful is here.

    ``source_text`` is the editor's current buffer for ``uri``; it lets
    us print the literal text of expressions in the hover instead of a
    generic placeholder. ``None`` is tolerated and falls back to a
    generic header.
    """
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None
    path = _uri_to_path(uri)
    if path is None:
        return None
    trees = result.trees.get(path.resolve())
    if trees is None:
        return None
    _, asr = trees
    expected = path.name

    # Function / subroutine *definition* (header line). Checked before
    # Var/Variable because LFortran emits synthetic ``Var`` nodes for
    # the formal args on the header line; without this they'd shadow
    # the function-name hover.
    for n in walk(asr):
        if not isinstance(n, dict):
            continue
        if n.get("node") not in ("Function", "Subroutine"):
            continue
        loc = n.get("loc") or {}
        fn = loc.get("first_filename")
        if isinstance(fn, str) and Path(fn).name != expected:
            continue
        if loc.get("first_line") != line_1based:
            continue
        if not _loc_contains(loc, line_1based, col_1based, expected):
            continue
        name = n.get("fields", {}).get("name")
        if not isinstance(name, str):
            continue
        sig = result.signatures.get(name)
        if sig is None:
            continue
        return _hover_signature(name, sig)

    # First try derived-type member access; it's more specific than plain Var.
    for node in _walk_member_nodes(asr):
        if not _loc_contains(node.get("loc"), line_1based, col_1based, expected):
            continue
        m_field = node.get("fields", {}).get("m")
        if not isinstance(m_field, str):
            continue
        qualified = m_field.split(" ", 1)[0]
        if "_" in qualified:
            head, rest = qualified.split("_", 1)
            if head.isdigit():
                qualified = rest
        for (type_name, field_name), unit in result.merged_field_units.items():
            if qualified == f"{type_name}_{field_name}":
                return _hover_text(f"{type_name}%{field_name}", _unit_pretty(unit))

    # Variable or Var: covers declarations and uses, plus the formals
    # inside a function definition (which already are Variable nodes).
    for node, name in _walk_var_nodes(asr):
        if not _loc_contains(node.get("loc"), line_1based, col_1based, expected):
            continue
        if not name:
            continue
        unit = result.merged_var_units.get(name)
        if unit is not None:
            return _hover_text(name, _unit_pretty(unit))
        return _hover_text(name, "no unit annotation", show_unit_label=False)

    # Function / subroutine call: show the signature + variables
    # passed as arguments.
    for node in _walk_call_nodes(asr):
        if not _loc_contains(node.get("loc"), line_1based, col_1based, expected):
            continue
        name = _call_name(node)
        sig = result.signatures.get(name)
        if sig is None:
            continue
        ast, _ = trees
        return _hover_call(
            result, ast, node, name, sig, expected_basename=expected
        )

    # Expression: find the smallest BinOp / UnaryMinus containing the
    # cursor and show its resolved unit + the operands' units.
    smallest = _smallest_expression_at(asr, line_1based, col_1based, expected)
    if smallest is not None:
        ast, _ = trees
        return _hover_expression(
            result, ast, smallest,
            expected_basename=expected, source_text=source_text,
        )

    # Assignment: cursor lands on the `=` (no more-specific node
    # matched it). Show LHS and RHS units, then the variables inside.
    asn = _assignment_containing(asr, line_1based, col_1based, expected)
    if asn is not None:
        ast, _ = trees
        return _hover_assignment(
            result, ast, asn,
            expected_basename=expected, source_text=source_text,
        )
    return None


def _assignment_containing(
    asr: dict, line: int, col: int, expected: str
) -> dict | None:
    best: dict | None = None
    best_size = 1_000_000
    for n in walk(asr):
        if not isinstance(n, dict) or n.get("node") != "Assignment":
            continue
        if not _loc_contains(n.get("loc"), line, col, expected):
            continue
        size = _loc_size(n.get("loc"))
        if size < best_size:
            best = n
            best_size = size
    return best


def _walk_call_nodes(tree: dict):
    for n in walk(tree):
        if not isinstance(n, dict):
            continue
        if n.get("node") in ("FunctionCall", "SubroutineCall"):
            yield n


def _call_name(node: dict) -> str:
    v = node.get("fields", {}).get("name", "")
    return v.split(" ", 1)[0] if isinstance(v, str) else ""


def _fmt_unit_opt(u: Unit | None) -> str:
    return format_unit(u) if u is not None else "?"


_EXPRESSION_NODES = frozenset({
    "RealBinOp", "IntegerBinOp", "ComplexBinOp", "LogicalBinOp",
    "RealUnaryMinus", "IntegerUnaryMinus",
})


def _loc_size(loc: dict | None) -> int:
    """Cheap "size" used to compare two locs; lower = more specific."""
    if not isinstance(loc, dict):
        return 1_000_000
    sl = loc.get("first_line")
    sc = loc.get("first_column")
    el = loc.get("last_line")
    ec = loc.get("last_column")
    if not all(isinstance(v, int) for v in (sl, sc, el, ec)):
        return 1_000_000
    # 1000 cols/line is a generous upper bound; we want a total order.
    return (el - sl) * 1000 + (ec - sc)


def _smallest_expression_at(
    asr: dict, line: int, col: int, expected: str
) -> dict | None:
    best: dict | None = None
    best_size = 1_000_000
    for n in walk(asr):
        if not isinstance(n, dict):
            continue
        if n.get("node") not in _EXPRESSION_NODES:
            continue
        if not _loc_contains(n.get("loc"), line, col, expected):
            continue
        size = _loc_size(n.get("loc"))
        if size < best_size:
            best = n
            best_size = size
    return best


def _build_resolver(result: WorksetResult, ast: dict) -> _Resolver:
    """Spin up a resolver pre-loaded with the workset's tables."""
    intrinsic_names = collect_intrinsic_names(ast)
    table = _units_mod.DEFAULT_TABLE
    return _Resolver(
        var_units=result.merged_var_units,
        table=table,                       # may be None outside the runtime; ok
        file="<hover>",
        intrinsic_names=intrinsic_names,
        functions=result.signatures,
        field_units=result.merged_field_units,
    )


def _gather_named_references(node: dict, expected_basename: str | None):
    """Yield ``(display_name, ASR sub-node)`` for every Var, Variable, or
    derived-type member access reachable from ``node``, in source order,
    de-duplicated by display name.

    A ``StructInstanceMember`` like ``b%m`` is yielded once as
    ``b%m``; the receiver ``Var`` for ``b`` it contains is suppressed
    so the hover doesn't carry a separate ``- b : ?`` row.

    The filename filter keeps out symbols inlined by ``use`` (whose loc
    points at a different file).
    """
    seen: set[str] = set()
    suppressed_ids: set[int] = set()
    for n in walk(node):
        if not isinstance(n, dict):
            continue
        if id(n) in suppressed_ids:
            continue
        kind = n.get("node")
        loc = n.get("loc")
        if expected_basename is not None and isinstance(loc, dict):
            fn = loc.get("first_filename")
            if isinstance(fn, str) and Path(fn).name != expected_basename:
                continue
        if kind == "Var":
            v = n.get("fields", {}).get("v", "")
            name = v.split(" ", 1)[0] if isinstance(v, str) else ""
        elif kind == "Variable":
            name = n.get("fields", {}).get("name", "")
        elif kind in ("FunctionCall", "SubroutineCall"):
            v = n.get("fields", {}).get("name", "")
            name = v.split(" ", 1)[0] if isinstance(v, str) else ""
        elif kind == "StructInstanceMember":
            v_node = n.get("fields", {}).get("v")
            m_field = n.get("fields", {}).get("m", "")
            if isinstance(v_node, dict):
                vv = v_node.get("fields", {}).get("v", "")
                receiver = vv.split(" ", 1)[0] if isinstance(vv, str) else "?"
            else:
                receiver = "?"
            qualified = m_field.split(" ", 1)[0] if isinstance(m_field, str) else ""
            if "_" in qualified:
                head, rest = qualified.split("_", 1)
                if head.isdigit():
                    qualified = rest
            if "_" in qualified:
                _, field_name = qualified.split("_", 1)
            else:
                field_name = qualified
            name = f"{receiver}%{field_name}"
            # The receiver Var (and anything else inside the member
            # access) is covered by the qualified name we're about to
            # yield; suppress it so we don't list `- b : ?` separately.
            for sub in walk(n):
                if isinstance(sub, dict) and sub is not n:
                    suppressed_ids.add(id(sub))
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        yield name, n


_SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "-": "⁻", "(": "⁽", ")": "⁾", "/": "ᐟ",
}


def _to_superscript(s: str) -> str:
    return "".join(_SUPERSCRIPTS.get(c, c) for c in s)


def _unit_pretty(u: Unit | None) -> str:
    """Render a Unit using Unicode (× for product, ⁿ superscripts, /
    for division). KaTeX isn't enabled in VSCode's default hover, so
    we keep everything in plain text.
    """
    if u is None:
        return "?"
    names = _base_symbols()
    pos: list[str] = []
    neg: list[str] = []
    for sym, exp in zip(names, u.dimension, strict=False):
        if exp == 0:
            continue
        mag = abs(exp)
        if mag == 1:
            term = sym
        elif isinstance(mag, int):
            term = sym + _to_superscript(str(mag))
        else:
            # Rational exponent (e.g. 1/2) — keep ASCII parens since
            # superscript fractions look messy.
            term = f"{sym}^({mag})"
        (pos if exp > 0 else neg).append(term)
    body = " × ".join(pos) if pos else "1"
    if neg:
        denom = " × ".join(neg)
        if len(neg) > 1:
            denom = f"({denom})"
        body = f"{body} / {denom}"
    return body


def _text_for_loc(source_text: str | None, loc: dict | None) -> str | None:
    """Slice the buffer text spanned by ``loc``.

    Multi-line slices are joined with spaces so a continued declaration
    renders as one readable line in the hover. Returns ``None`` if the
    loc looks malformed or the buffer is unavailable.
    """
    if not source_text or not isinstance(loc, dict):
        return None
    sl = loc.get("first_line")
    sc = loc.get("first_column")
    el = loc.get("last_line")
    ec = loc.get("last_column")
    if not all(isinstance(v, int) for v in (sl, sc, el, ec)):
        return None
    lines = source_text.splitlines()
    if sl < 1 or el < 1 or sl > len(lines) or el > len(lines):
        return None
    sl_i, el_i = sl - 1, el - 1
    if sl_i == el_i:
        snippet = lines[sl_i][sc - 1 : ec]
    else:
        parts = [lines[sl_i][sc - 1 :]]
        parts.extend(lines[sl_i + 1 : el_i])
        parts.append(lines[el_i][:ec])
        snippet = " ".join(p.strip() for p in parts)
    return snippet.strip() or None


def _variables_list_md(
    resolver, node: dict, expected_basename: str | None, *, bulleted: bool = True
) -> list[str]:
    """One entry per variable / member access / call reachable from ``node``."""
    out: list[str] = []
    prefix = "- " if bulleted else ""
    for name, sub in _gather_named_references(node, expected_basename):
        kind = sub.get("node")
        if kind in ("FunctionCall", "SubroutineCall"):
            sig = resolver.functions.get(name)
            if sig is not None:
                out.append(f"{prefix}{_sig_render_md(name, sig)}")
            continue
        u = resolver.resolve(sub)
        out.append(f"{prefix}`{name}` : {_unit_pretty(u)}")
    return out


def _hard_break_lines(lines: list[str]) -> str:
    """Join lines so each renders on its own visual line in Markdown
    (trailing two spaces = hard linebreak)."""
    return "\n".join(line + "  " if line else "" for line in lines).rstrip()


def _hover_expression(
    result: WorksetResult,
    ast: dict,
    node: dict,
    *,
    expected_basename: str,
    source_text: str | None,
) -> str | None:
    resolver = _build_resolver(result, ast)
    own = resolver.resolve(node)

    snippet = _text_for_loc(source_text, node.get("loc"))
    header = (
        f"`{snippet}` : {_unit_pretty(own)}"
        if snippet
        else f"expression : {_unit_pretty(own)}"
    )

    rows = _variables_list_md(resolver, node, expected_basename)
    body = header if not rows else header + "\n" + "\n".join(rows)
    return f"**DimFort**\n\n{body}"


def _leaf_display_name(node: dict | None) -> str | None:
    """Return the bare display name when ``node`` is itself a single
    Var / Variable / StructInstanceMember (i.e. shown literally on the
    LHS or RHS line). Otherwise ``None``.
    """
    if not isinstance(node, dict):
        return None
    kind = node.get("node")
    if kind == "Var":
        v = node.get("fields", {}).get("v", "")
        return v.split(" ", 1)[0] if isinstance(v, str) else None
    if kind == "Variable":
        return node.get("fields", {}).get("name") or None
    if kind == "StructInstanceMember":
        # Reuse the same parsing the variable-list code does so the
        # names match for dedup.
        for name, sub in _gather_named_references(node, None):
            if sub is node:
                return name
        return None
    return None


def _hover_assignment(
    result: WorksetResult,
    ast: dict,
    node: dict,
    *,
    expected_basename: str,
    source_text: str | None,
) -> str | None:
    resolver = _build_resolver(result, ast)
    fields = node.get("fields", {})
    target = fields.get("target")
    value = fields.get("value")
    if not isinstance(target, dict) or not isinstance(value, dict):
        return None

    lhs_unit = resolver.resolve(target)
    rhs_unit = resolver.resolve(value)

    # Header only when both sides agree on a known unit: a single line
    # showing the shared dimension. On mismatch we skip the header
    # entirely because the H001 diagnostic already shows the
    # `lhs ≠ rhs` comparison.
    header: str | None = None
    if (
        lhs_unit is not None
        and rhs_unit is not None
        and equal_dim(lhs_unit, rhs_unit)
    ):
        header = _unit_pretty(lhs_unit)

    # Variables / members reachable from the whole assignment, deduped
    # by display name, in source order, without bullets.
    rows = _variables_list_md(
        resolver, node, expected_basename, bulleted=False
    )

    parts: list[str] = []
    if header is not None:
        parts.append(header)
    if rows:
        parts.append(_hard_break_lines(rows))
    if not parts:
        return None
    return "**DimFort**\n\n" + "\n\n".join(parts)


def _sig_render_md(name: str, sig: FuncSig) -> str:
    """Markdown rendering of a call: the call form in backticks, then
    ``: return-unit`` outside for functions. Mirrors the
    ``\\`name\\` : unit`` shape used by the variables list so the rows
    line up visually.
    """
    args = ", ".join(
        f"{arg_name}: {_unit_pretty(arg_unit) if arg_unit is not None else '?'}"
        for arg_name, arg_unit in zip(sig.arg_names, sig.arg_units, strict=False)
    )
    if sig.is_subroutine:
        return f"`{name}({args})`"
    ret = _unit_pretty(sig.return_unit) if sig.return_unit is not None else "?"
    return f"`{name}({args})` : {ret}"


def _hover_signature(name: str, sig: FuncSig) -> str:
    # Header-only fallback. The richer renderer that also lists arg
    # variables is :func:`_hover_call`, used when we have the ASR.
    return f"**DimFort**\n\n{_sig_render_md(name, sig)}"


def _hover_call(
    result: WorksetResult,
    ast: dict,
    node: dict,
    name: str,
    sig: FuncSig,
    *,
    expected_basename: str,
) -> str:
    """Hover for a user-defined call: signature line + variables passed."""
    resolver = _build_resolver(result, ast)
    header = _sig_render_md(name, sig)

    # Variables / members / nested calls inside the actual arguments,
    # de-duplicated by display name.
    seen: set[str] = set()
    rows: list[str] = []
    for arg in node.get("fields", {}).get("args") or []:
        if not isinstance(arg, dict):
            continue
        val = arg.get("fields", {}).get("value")
        if not isinstance(val, dict):
            continue
        for nm, sub in _gather_named_references(val, expected_basename):
            if nm in seen:
                continue
            seen.add(nm)
            kind = sub.get("node")
            if kind in ("FunctionCall", "SubroutineCall"):
                sub_sig = resolver.functions.get(nm)
                if sub_sig is not None:
                    rows.append(_sig_render_md(nm, sub_sig))
                continue
            u = resolver.resolve(sub)
            rows.append(f"`{nm}` : {_unit_pretty(u)}")

    body = header if not rows else "\n\n".join([header, _hard_break_lines(rows)])
    return f"**DimFort**\n\n{body}"


def _hover_text(name: str, unit_or_message: str, *, show_unit_label: bool = True) -> str:
    """Render a single-symbol hover (variable or struct member).

    ``unit_or_message`` is either an inline-math snippet (e.g.
    ``$\\mathrm{m}/\\mathrm{s}$``) or a plain message when there's no
    unit to display.
    """
    if show_unit_label:
        body = f"**{name}** : {unit_or_message}"
    else:
        body = f"**{name}** — {unit_or_message}"
    return f"**DimFort**\n\n{body}"


# ---------------------------------------------------------------------------
# LSP handlers
# ---------------------------------------------------------------------------


@server.feature(lsp.INITIALIZE)
def _initialize(ls: LanguageServer, params: lsp.InitializeParams) -> None:
    global _workspace_folders
    folders: list[Path] = []
    if params.workspace_folders:
        for folder in params.workspace_folders:
            p = _uri_to_path(folder.uri)
            if p is not None:
                folders.append(p)
    elif params.root_uri:
        p = _uri_to_path(params.root_uri)
        if p is not None:
            folders.append(p)
    _workspace_folders = folders

    opts = params.initialization_options or {}
    if isinstance(opts, dict):
        _features.inlay_hints = bool(opts.get("inlayHintsEnabled", True))
        _features.completion = bool(opts.get("completionEnabled", True))
        _features.code_actions = bool(opts.get("codeActionsEnabled", True))
        _features.goto_definition = bool(opts.get("gotoDefinitionEnabled", True))
        _features.code_lens = bool(opts.get("codeLensEnabled", False))
    log.info(
        "DimFort LSP initialised; folders=%s features=%s",
        folders,
        vars(_features),
    )


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def _did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    _remember_uri(params.text_document.uri)
    _publish_for_uri(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def _did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    _remember_uri(params.text_document.uri)
    _publish_for_uri(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def _did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    _forget_uri(params.text_document.uri)
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
    )


_DEBOUNCE_SECONDS = 0.4


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def _did_change(ls: LanguageServer, params: lsp.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)
    version = _bump_version(uri)

    # Pygls keeps a TextDocument with the up-to-date buffer source.
    doc = ls.workspace.get_text_document(uri)
    text = doc.source

    def delayed() -> None:
        time.sleep(_DEBOUNCE_SECONDS)
        if not _is_current(uri, version):
            return  # superseded by a later keystroke
        try:
            _publish_for_uri(ls, uri, override_text=text)
        except Exception:
            log.exception("debounced check failed for %s", uri)

    threading.Thread(target=delayed, daemon=True).start()


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def _hover(ls: LanguageServer, params: lsp.HoverParams) -> Any:
    uri = params.text_document.uri
    # LSP positions are 0-based; our internal helpers are 1-based.
    line = params.position.line + 1
    col = params.position.character + 1
    source_text: str | None = None
    try:
        source_text = ls.workspace.get_text_document(uri).source
    except Exception:
        log.debug("could not fetch buffer text for %s", uri)
    text = _resolve_hover(uri, line, col, source_text)
    if text is None:
        return None
    return lsp.Hover(
        contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=text)
    )


# ---------------------------------------------------------------------------
# Inlay hints
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_INLAY_HINT,
    lsp.InlayHintOptions(resolve_provider=False),
)
def _inlay_hint(
    ls: LanguageServer, params: lsp.InlayHintParams
) -> list[lsp.InlayHint] | None:
    if not _features.inlay_hints:
        return None
    with _last_result_lock:
        result = _last_result
    if result is None:
        return []
    path = _uri_to_path(params.text_document.uri)
    if path is None:
        return []
    trees = result.trees.get(path.resolve())
    if trees is None:
        return []
    _, asr = trees

    expected = path.name
    visible_start_line = params.range.start.line + 1   # 1-based
    visible_end_line = params.range.end.line + 1
    seen_positions: set[tuple[int, int]] = set()
    hints: list[lsp.InlayHint] = []

    for node in walk(asr):
        if not isinstance(node, dict):
            continue
        kind = node.get("node")
        if kind == "Var":
            name = node.get("fields", {}).get("v", "").split(" ", 1)[0]
            unit = result.merged_var_units.get(name)
        elif kind == "FunctionCall":
            name = node.get("fields", {}).get("name", "").split(" ", 1)[0]
            sig = result.signatures.get(name)
            unit = sig.return_unit if sig is not None else None
        else:
            continue
        if unit is None:
            continue
        loc = node.get("loc") or {}
        if not isinstance(loc, dict):
            continue
        fn = loc.get("first_filename")
        if isinstance(fn, str) and Path(fn).name != expected:
            continue
        line = loc.get("last_line")
        col = loc.get("last_column")
        if not isinstance(line, int) or not isinstance(col, int):
            continue
        if line < visible_start_line or line > visible_end_line:
            continue
        key = (line, col)
        if key in seen_positions:
            continue
        seen_positions.add(key)
        hints.append(
            lsp.InlayHint(
                position=lsp.Position(line=line - 1, character=col),
                # No leading space in the label; padding_left=False so the
                # hint sits flush against the variable / call.
                label=f"[{_unit_pretty(unit)}]",
                kind=lsp.InlayHintKind.Type,
                padding_left=False,
            )
        )
    return hints


# ---------------------------------------------------------------------------
# Completion inside `@unit{…}`
# ---------------------------------------------------------------------------


_UNIT_TRIGGER_RE = re.compile(r"@unit\s*\{([^}]*)$")


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=["{", " ", "/", "*", "^"]),
)
def _completion(
    ls: LanguageServer, params: lsp.CompletionParams
) -> lsp.CompletionList | None:
    if not _features.completion:
        return None
    table = _units_mod.DEFAULT_TABLE
    if table is None:
        return None
    try:
        doc = ls.workspace.get_text_document(params.text_document.uri)
    except Exception:
        return None
    line_text = doc.lines[params.position.line] if params.position.line < len(doc.lines) else ""
    prefix = line_text[: params.position.character]
    # Only fire when the cursor is inside an unclosed `@unit{…}`.
    if not _UNIT_TRIGGER_RE.search(prefix):
        return None

    items: list[lsp.CompletionItem] = []
    for name in sorted(table.base):
        items.append(
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Unit,
                detail="base unit",
            )
        )
    for name in sorted(table.derived):
        items.append(
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Unit,
                detail="derived unit",
            )
        )
    for prefix_sym in sorted(table.prefixes):
        items.append(
            lsp.CompletionItem(
                label=prefix_sym,
                kind=lsp.CompletionItemKind.Constant,
                detail=f"SI prefix ({table.prefixes[prefix_sym]})",
            )
        )
    return lsp.CompletionList(is_incomplete=False, items=items)


# ---------------------------------------------------------------------------
# Go to definition
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def _definition(
    ls: LanguageServer, params: lsp.DefinitionParams
) -> list[lsp.Location] | None:
    if not _features.goto_definition:
        return None
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None
    path = _uri_to_path(params.text_document.uri)
    if path is None:
        return None
    trees = result.trees.get(path.resolve())
    if trees is None:
        return None
    _, asr = trees

    expected = path.name
    line = params.position.line + 1
    col = params.position.character + 1

    # Identify what's under the cursor: a Var (use) or a call.
    target_name: str | None = None
    target_kind: str | None = None
    for n in walk(asr):
        if not isinstance(n, dict):
            continue
        kind = n.get("node")
        if kind not in ("Var", "FunctionCall", "SubroutineCall"):
            continue
        if not _loc_contains(n.get("loc"), line, col, expected):
            continue
        v = n.get("fields", {}).get("v" if kind == "Var" else "name", "")
        if not isinstance(v, str):
            continue
        bare = v.split(" ", 1)[0]
        if bare:
            target_name = bare
            target_kind = kind
            break

    if not target_name:
        return None

    # Search every loaded ASR for the matching declaration / function.
    for tree_path, (_, file_asr) in result.trees.items():
        for n in walk(file_asr):
            if not isinstance(n, dict):
                continue
            kind = n.get("node")
            want_variable = target_kind == "Var" and kind == "Variable"
            want_callable = (
                target_kind in ("FunctionCall", "SubroutineCall")
                and kind in ("Function", "Subroutine")
            )
            if not (want_variable or want_callable):
                continue
            if n.get("fields", {}).get("name") != target_name:
                continue
            loc = n.get("loc") or {}
            sl = loc.get("first_line")
            sc = loc.get("first_column")
            el = loc.get("last_line")
            ec = loc.get("last_column")
            if not all(isinstance(v, int) for v in (sl, sc, el, ec)):
                continue
            return [
                lsp.Location(
                    uri=_uri_for_path(tree_path),
                    range=lsp.Range(
                        start=lsp.Position(line=sl - 1, character=sc - 1),
                        end=lsp.Position(line=el - 1, character=ec),
                    ),
                )
            ]
    return None


# ---------------------------------------------------------------------------
# Code action: insert a `!< @unit{}` skeleton on annotation-less decls
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_CODE_ACTION,
    lsp.CodeActionOptions(code_action_kinds=[lsp.CodeActionKind.QuickFix]),
)
def _code_action(
    ls: LanguageServer, params: lsp.CodeActionParams
) -> list[lsp.CodeAction] | None:
    if not _features.code_actions:
        return None
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None
    path = _uri_to_path(params.text_document.uri)
    if path is None:
        return None
    resolved = path.resolve()
    attached = result.attachments.get(resolved)
    if attached is None:
        return None
    try:
        doc = ls.workspace.get_text_document(params.text_document.uri)
    except Exception:
        return None

    # Decide which DeclarationSites overlap the cursor / selection.
    selection_start = params.range.start.line + 1
    selection_end = params.range.end.line + 1
    actions: list[lsp.CodeAction] = []
    # Reach into the ScanResult to know which decls have no annotation
    # yet. attach.AttachmentResult doesn't track this directly, so we
    # diff: any declaration whose names aren't all in var_units|field_units.
    scan_decls = _last_scan_declarations(path)
    if scan_decls is None:
        return None
    for decl in scan_decls:
        if decl.line_end < selection_start or decl.line_start > selection_end:
            continue
        any_annotated = False
        if decl.enclosing_type is not None:
            any_annotated = any(
                (decl.enclosing_type, name) in attached.field_units
                for name in decl.names
            )
        else:
            any_annotated = any(name in attached.var_units for name in decl.names)
        if any_annotated:
            continue
        # Build the edit: append ` !< @unit{}` at end of the declaration's
        # first source line.
        target_line_idx = decl.line_start - 1
        if target_line_idx >= len(doc.lines):
            continue
        line = doc.lines[target_line_idx].rstrip("\n").rstrip("\r")
        # If the line already has a `!` comment, splice before it; else
        # append at end-of-line.
        comment_col = _comment_column(line)
        insert_col = comment_col if comment_col is not None else len(line)
        # Use a command (handled by the VSCode extension) so the cursor
        # lands inside the braces ready for typing. Plain LSP TextEdits
        # can't position the cursor; non-VSCode clients that don't have
        # the `dimfort.insertSnippet` command registered would see this
        # action as a no-op — acceptable for v1.
        snippet = "  !< @unit{$0}"
        action = lsp.CodeAction(
            title=f"DimFort: Add @unit{{}} to {', '.join(decl.names)}",
            kind=lsp.CodeActionKind.QuickFix,
            command=lsp.Command(
                title="DimFort: insert @unit{} snippet",
                command="dimfort.insertSnippet",
                arguments=[
                    params.text_document.uri,
                    target_line_idx,
                    insert_col,
                    snippet,
                ],
            ),
        )
        actions.append(action)
    return actions or None


def _last_scan_declarations(path: Path):
    """Re-scan the file on disk to recover the source-side declarations.

    We don't currently cache DeclarationSites in WorksetResult, so this
    is the simplest path. Reads off-disk (the buffer text path used by
    didChange isn't accessible here).
    """
    from dimfort.core.annotations import scan_file

    try:
        return scan_file(path).declarations
    except OSError:
        return None


# ---------------------------------------------------------------------------
# CodeLens — signature shown above function/subroutine definitions.
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_CODE_LENS,
    lsp.CodeLensOptions(resolve_provider=False),
)
def _code_lens(
    ls: LanguageServer, params: lsp.CodeLensParams
) -> list[lsp.CodeLens] | None:
    if not _features.code_lens:
        return None
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None
    path = _uri_to_path(params.text_document.uri)
    if path is None:
        return None
    trees = result.trees.get(path.resolve())
    if trees is None:
        return None
    _, asr = trees

    expected = path.name
    lenses: list[lsp.CodeLens] = []
    seen_lines: set[int] = set()
    for n in walk(asr):
        if not isinstance(n, dict):
            continue
        if n.get("node") not in ("Function", "Subroutine"):
            continue
        loc = n.get("loc") or {}
        fn = loc.get("first_filename")
        if isinstance(fn, str) and Path(fn).name != expected:
            continue
        first_line = loc.get("first_line")
        if not isinstance(first_line, int):
            continue
        if first_line in seen_lines:
            continue
        seen_lines.add(first_line)
        name = n.get("fields", {}).get("name")
        if not isinstance(name, str):
            continue
        sig = result.signatures.get(name)
        if sig is None:
            continue
        title = _sig_render_md(name, sig).replace("`", "")  # plain text only
        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(
                    start=lsp.Position(line=first_line - 1, character=0),
                    end=lsp.Position(line=first_line - 1, character=0),
                ),
                command=lsp.Command(title=title, command=""),
            )
        )
    return lenses or None


def _comment_column(line: str) -> int | None:
    """Find the column where the line's `!` comment starts, or None."""
    in_quote: str | None = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_quote is None:
            if c == "!":
                return i
            if c in ("'", '"'):
                in_quote = c
        else:
            if c == in_quote:
                if i + 1 < len(line) and line[i + 1] == in_quote:
                    i += 1
                else:
                    in_quote = None
        i += 1
    return None


def run_stdio() -> None:
    server.start_io()
