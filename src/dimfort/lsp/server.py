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

import contextlib
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
from dimfort.config import DimfortConfig, load_config
from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core import unit_config  # noqa: F401  populates DEFAULT_TABLE
from dimfort.core import units as _units_mod
from dimfort.core.diagnostics import Diagnostic, Severity
from dimfort.core.multifile import WorksetResult, check_files
from dimfort.core.symbols import FuncSig
from dimfort.core.workspace_index import (
    WorkspaceIndex,
    resolve_workset,
    scan_workspace,
    update_index,
)
from dimfort.core.units import Unit, equal_dim, format_unit
from dimfort.core.units import base_symbols as _base_symbols
from dimfort.lsp import ts_helpers as _ts_h

log = logging.getLogger("dimfort.lsp")

server = LanguageServer("dimfort", __version__)


def _notify(ls: LanguageServer | None, message: str, *, toast: bool = False) -> None:
    """Surface a progress message to both the Python logger and the
    LSP client's output channel. Pygls filters ``log.info`` below
    WARNING by default, so user-relevant events would otherwise be
    invisible in VSCode's "DimFort Language Server" output channel.
    Pass ``toast=True`` for unblock signals worth a status-bar popup.
    """
    log.info(message)
    if ls is None:
        return
    try:
        ls.window_log_message(
            lsp.LogMessageParams(type=lsp.MessageType.Info, message=message)
        )
        if toast:
            ls.window_show_message(
                lsp.ShowMessageParams(type=lsp.MessageType.Info, message=message)
            )
    except Exception:
        log.debug("window/logMessage failed", exc_info=True)


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

# Serialises every pipeline run across didOpen / didSave / didChange.
# Without this, VSCode restoring N tabs after a reload fires N
# concurrent didOpens, each spawning its own LFortran subprocesses
# and ASR JSON in memory; the pile-up exceeds macOS jetsam's budget
# and the LSP process gets SIGKILLed.
_check_lock = threading.Lock()

# Last successful check result, used for hover.
_last_result: WorksetResult | None = None
_last_result_lock = threading.Lock()

# Workspace folders, captured at initialise time.
_workspace_folders: list[Path] = []

# Workspace module index — built once at initialize on a background
# thread (it can take several seconds on large codebases), updated
# incrementally on didChange / didSave. ``None`` until the initial
# scan completes; callers fall back to whole-workspace check while
# ``None``.
_workspace_index: WorkspaceIndex | None = None
_workspace_index_lock = threading.Lock()

# Modules treated as known-external (Fortran intrinsics + common libs).
# Anything `use`d that matches this set is silently dropped from the
# dep chain rather than producing a missing-module diagnostic.
_DEFAULT_EXTERNAL_MODULES: frozenset[str] = frozenset({
    # Fortran 2003+ intrinsic modules
    "iso_fortran_env", "iso_c_binding",
    "ieee_arithmetic", "ieee_exceptions", "ieee_features",
    # Common external libraries
    "mpi", "mpi_f08", "openacc", "omp_lib",
    "netcdf", "netcdf95", "ioipsl", "nrtype",
})
_external_modules: frozenset[str] = _DEFAULT_EXTERNAL_MODULES

# Maximum number of files to feed into a single check. Resolving the
# full transitive `use` closure of a deep LMDZ-scale entry point (e.g.
# `phylmd/physiq_mod.F90` -> ~353 files) holds enough AST/ASR JSON in
# memory to trigger macOS jetsam SIGKILL on the LSP process. The cap
# trades cross-file coverage for stability: when the workset exceeds
# this, we keep the last N entries in topo order — the active file
# plus its nearest deps. Override via `maxWorksetSize` in
# initializationOptions.
_DEFAULT_MAX_WORKSET = 40
_max_workset_size: int = _DEFAULT_MAX_WORKSET

# Resolved project config (``.dimfort.toml``). Loaded once at
# ``initialize`` time; an LSP restart is required to re-read.
# Read from worker threads without a lock: per the LSP protocol the
# client cannot send textDocument/* requests before our initialize
# response, so the write in ``_initialize`` happens-before every
# worker-thread read. Don't introduce code paths that read these
# state vars before the initialize handler returns.
_project_config: DimfortConfig = DimfortConfig()


def _cap_workset(paths: list[Path], active: Path, limit: int) -> list[Path]:
    """Trim a workset down to ``limit`` entries while keeping the active
    file. Takes the last ``limit`` entries in topo order (closest to
    the active file's leaves)."""
    if len(paths) <= limit:
        return paths
    tail = paths[-limit:]
    if active not in tail:
        tail = tail[1:] + [active]
    return tail

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

    Uses the workspace index to follow ``use``-statement dependencies
    from the active file when the index is ready. Falls back to the
    whole-workspace scan otherwise (initial-check window before the
    index finishes building, or no workspace folders configured).

    Always includes the active file even if it lives outside any
    workspace folder (e.g. the user opened a loose ``.f90``).
    """
    active = _uri_to_path(active_uri)
    if active is None or not active.is_file():
        return [], active

    with _workspace_index_lock:
        idx = _workspace_index

    resolved_active = active.resolve()

    if idx is not None:
        res = resolve_workset(
            idx, [resolved_active], external_modules=_external_modules
        )
        paths = list(res.compile_order)
        # Belt-and-braces: ensure the active file is present even if it
        # lives outside the indexed roots (e.g. a loose `.f90` outside
        # any workspace folder).
        if resolved_active not in paths:
            paths.append(resolved_active)
        # Cap to keep the LSP process alive on deep workspaces.
        capped = _cap_workset(paths, resolved_active, _max_workset_size)
        if len(capped) < len(paths):
            _notify(
                ls,
                f"DimFort: workset capped at {_max_workset_size} "
                f"(full deps: {len(paths)}) for {resolved_active.name}",
            )
        return capped, active

    # Fallback: index not ready yet, or no workspace folders. Just
    # check the active file alone. Cross-file deps will surface as
    # transient U007 errors until the index build finishes; that's
    # strictly better than feeding every file in the workspace to
    # the checker — on LMDZ-scale (2435 files) the old behaviour
    # SIGKILLed the LSP via macOS jetsam.
    return [resolved_active], active


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
        result = check_files(
            paths,
            overrides=overrides,
            external_modules=_external_modules,
            cpp_defines=_project_config.cpp_defines,
            include_paths=_project_config.include_paths,
        )
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


def _trees_for(uri: str) -> tuple[Path, object, bytes] | None:
    """Return ``(resolved_path, tree, source_bytes)`` for ``uri`` if loaded."""
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None
    path = _uri_to_path(uri)
    if path is None:
        return None
    entry = result.trees.get(path.resolve())
    if entry is None:
        return None
    tree, source = entry
    return path.resolve(), tree, source


def _build_ts_ctx(result: WorksetResult, source: bytes, file: str) -> ts_checker._Ctx:
    """Spin up a ts_checker ``_Ctx`` pre-loaded with the workset's tables.

    Reused by hover / inlay so identifier-to-unit lookup goes through
    the same logic as the diagnostic pipeline — no second source of
    truth for derived-type / use-chain resolution.
    """
    return ts_checker._Ctx(
        file=file,
        var_units=result.merged_var_units,
        table=_units_mod.DEFAULT_TABLE,
        signatures=result.signatures,
        # var_types / type_field_types are collected per-tree on demand
        # by callers that need member-access resolution.
        var_types={},
        type_field_types={},
        field_units=result.merged_field_units,
    )


# ---------------------------------------------------------------------------
# Hover rendering (parser-agnostic)
# ---------------------------------------------------------------------------


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
            term = f"{sym}^({mag})"
        (pos if exp > 0 else neg).append(term)
    body = " × ".join(pos) if pos else "1"
    if neg:
        denom = " × ".join(neg)
        if len(neg) > 1:
            denom = f"({denom})"
        body = f"{body} / {denom}"
    return body


def _hover_text(name: str, unit_or_message: str, *, show_unit_label: bool = True) -> str:
    """Render a single-symbol hover (variable or struct member)."""
    if show_unit_label:
        body = f"**{name}** : {unit_or_message}"
    else:
        body = f"**{name}** — {unit_or_message}"
    return f"**DimFort**\n\n{body}"


def _sig_render_md(name: str, sig: FuncSig) -> str:
    """Markdown rendering of a call signature."""
    args = ", ".join(
        f"{arg_name}: {_unit_pretty(arg_unit) if arg_unit is not None else '?'}"
        for arg_name, arg_unit in zip(sig.arg_names, sig.arg_units, strict=False)
    )
    if sig.is_subroutine:
        return f"`{name}({args})`"
    ret = _unit_pretty(sig.return_unit) if sig.return_unit is not None else "?"
    return f"`{name}({args})` : {ret}"


def _hover_signature(name: str, sig: FuncSig) -> str:
    return f"**DimFort**\n\n{_sig_render_md(name, sig)}"


# ---------------------------------------------------------------------------
# Hover dispatch (tree-sitter)
# ---------------------------------------------------------------------------


def _resolve_hover(
    uri: str,
    line_1based: int,
    col_1based: int,
    source_text: str | None,  # accepted for caller compatibility; unused
) -> str | None:
    """Return formatted hover text for ``(line, col)``, or ``None``.

    Dispatch order, tightest-fit wins inside each category:

    1. **Function/Subroutine definition header** — the cursor is on the
       ``name`` token of a function or subroutine declaration.
    2. **Derived-type member access** (``a%b``) — show the field's unit.
    3. **Call expression / subroutine call** — show the callee's signature.
    4. **Plain identifier** — variable reference; show its unit.

    Less specific matches (assignment LHS/RHS hovers, BinOp hovers
    showing the resolved expression unit) used to live here on the
    LFortran-AST path. They are intentionally not ported in this pass:
    they degrade gracefully (no hover at that exact position) and the
    diagnostic-driven information is unchanged.
    """
    found = _trees_for(uri)
    if found is None:
        return None
    resolved_path, tree, source = found
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None

    # 1. Function / subroutine definition header on this line.
    for func_or_sub in _ts_h.walk_function_definitions(tree):
        if _ts_h.function_definition_header_line(func_or_sub) != line_1based:
            continue
        nm = _ts_h.function_definition_name(func_or_sub, source)
        if nm is None:
            continue
        name, name_node = nm
        if not _ts_h.node_contains(name_node, line_1based, col_1based):
            continue
        sig = result.signatures.get(name.lower())
        if sig is None:
            continue
        return _hover_signature(name, sig)

    # 2. Derived-type member access — tightest enclosing wins so the
    #    innermost ``a%b`` in ``a%b%c`` doesn't shadow the outer.
    member_hit = _ts_h.smallest_enclosing(
        _ts_h.walk_member_exprs(tree), line_1based, col_1based
    )
    if member_hit is not None:
        # Need var_types for this file to resolve b's type for a%b%c.
        # Build a one-off mini-context.
        ctx = _build_ts_ctx(result, source, str(resolved_path))
        ctx.var_types.update(ts_checker.collect_var_types(tree, source))
        ctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))
        unit = ts_checker._resolve_member_chain(member_hit, ctx, source)
        base, path = _ts_h.member_expr_chain(member_hit, source)
        if base is not None and path:
            display = f"{base}%{'%'.join(path)}"
            return _hover_text(display, _unit_pretty(unit))

    # 3. Call expression / subroutine call.
    call_hit = _ts_h.smallest_enclosing(
        _ts_h.walk_calls(tree), line_1based, col_1based
    )
    if call_hit is not None:
        name = _ts_h.call_name(call_hit, source)
        if name is not None:
            sig = result.signatures.get(name.lower())
            if sig is not None:
                return _hover_signature(name, sig)

    # 4. Bare identifier — variable reference.
    for ident in _ts_h.walk_identifiers(tree):
        if not _ts_h.node_contains(ident, line_1based, col_1based):
            continue
        if _ts_h.is_inside_type_qualifier(ident):
            continue
        # Skip call-callee identifiers (handled above).
        if _ts_h.is_call_callee(ident):
            continue
        name = _ts.node_text(ident, source)
        unit = result.merged_var_units.get(name)
        if unit is not None:
            return _hover_text(name, _unit_pretty(unit))
        # Lower-case fallback for var_units keyed by original case.
        for k, u in result.merged_var_units.items():
            if k.lower() == name.lower():
                return _hover_text(name, _unit_pretty(u))
        return _hover_text(name, "no unit annotation", show_unit_label=False)
    return None


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

    # Load .dimfort.toml from the first workspace folder, if any.
    global _project_config
    if folders:
        _project_config = load_config(folders[0])
    config = _project_config

    # Start from config-provided values; initializationOptions override.
    _external_modules_from_config = _DEFAULT_EXTERNAL_MODULES | frozenset(
        config.external_modules
    )
    opts = params.initialization_options or {}
    global _external_modules, _max_workset_size
    _external_modules = _external_modules_from_config
    if config.max_workset_size is not None:
        _max_workset_size = config.max_workset_size

    if isinstance(opts, dict):
        _features.inlay_hints = bool(opts.get("inlayHintsEnabled", True))
        _features.completion = bool(opts.get("completionEnabled", True))
        _features.code_actions = bool(opts.get("codeActionsEnabled", True))
        _features.goto_definition = bool(opts.get("gotoDefinitionEnabled", True))
        _features.code_lens = bool(opts.get("codeLensEnabled", False))
        # Init options extend whatever config already contributed.
        extra = opts.get("externalModules")
        if isinstance(extra, list):
            _external_modules = _external_modules | frozenset(
                str(m).lower() for m in extra if isinstance(m, str)
            )
        cap = opts.get("maxWorksetSize")
        if isinstance(cap, int) and cap > 0:
            _max_workset_size = cap

    config_note = (
        f" (config: {config.config_path.name})"
        if config.config_path is not None
        else ""
    )
    _notify(
        ls,
        f"DimFort LSP initialised — {len(folders)} folder(s), "
        f"{len(_external_modules)} external module(s) on allowlist"
        f"{config_note}",
    )


@server.feature(lsp.INITIALIZED)
def _initialized(ls: LanguageServer, params: lsp.InitializedParams) -> None:
    """Kick off the workspace scan once the client is ready.

    The workspace scan needs to send server-to-client requests
    (``window/workDoneProgress/create``); these are only valid after the
    client has sent the ``initialized`` notification. Spawning earlier —
    e.g. from inside the ``initialize`` handler — races against the
    client's readiness and produces JsonRpcMethodNotFound responses.
    """
    folders = _workspace_folders
    if not folders:
        return
    # If ``.dimfort.toml`` narrows the source tree via [project].src_paths,
    # scan only those subdirectories. Otherwise scan every workspace
    # folder. Useful on large monorepos where DimFort cares about a
    # handful of subtrees.
    scan_roots = tuple(_project_config.src_paths) or tuple(folders)
    _notify(
        ls,
        f"DimFort: scanning workspace ({len(scan_roots)} root(s))…",
    )
    threading.Thread(
        target=_build_initial_index,
        args=(ls, scan_roots),
        daemon=True,
        name="dimfort-workspace-scan",
    ).start()


def _build_initial_index(ls: LanguageServer, roots: tuple[Path, ...]) -> None:
    """Background scan; assigns the result to the module-level index.

    Emits ``$/progress`` notifications so VSCode shows a status-bar
    spinner with per-file detail. Reports are throttled to ~10/sec so
    a 2435-file scan doesn't flood the wire.
    """
    global _workspace_index
    token = f"dimfort-scan-{int(time.time() * 1000)}"
    progress = ls.work_done_progress
    progress_started = False
    try:
        progress.create(token).result(timeout=2.0)
        progress.begin(
            token,
            lsp.WorkDoneProgressBegin(
                title="DimFort: scanning workspace",
                cancellable=False,
                percentage=0,
            ),
        )
        progress_started = True
    except Exception:
        log.debug("could not start workDoneProgress", exc_info=True)

    last_report_at = 0.0

    def on_progress(scanned: int, total: int, path: Path) -> None:
        nonlocal last_report_at
        if not progress_started:
            return
        now = time.monotonic()
        # Always report the final tick; throttle the rest to ~10/sec.
        if scanned != total and now - last_report_at < 0.1:
            return
        last_report_at = now
        try:
            progress.report(
                token,
                lsp.WorkDoneProgressReport(
                    message=f"{scanned}/{total} {path.name}",
                    percentage=int(scanned * 100 / total) if total else 100,
                ),
            )
        except Exception:
            log.debug("workDoneProgress report failed", exc_info=True)

    try:
        idx = scan_workspace(roots, progress_cb=on_progress)
    except Exception:
        log.exception("workspace index build failed")
        if progress_started:
            try:
                progress.end(token, lsp.WorkDoneProgressEnd(message="failed"))
            except Exception:
                log.debug("workDoneProgress end failed", exc_info=True)
        return

    with _workspace_index_lock:
        _workspace_index = idx

    if progress_started:
        try:
            progress.end(token, lsp.WorkDoneProgressEnd(message="done"))
        except Exception:
            log.debug("workDoneProgress end failed", exc_info=True)

    _notify(
        ls,
        f"DimFort workspace index ready: {len(idx.modules)} modules "
        f"across {len(idx.uses_by_file)} files",
        toast=True,
    )

    # Re-check every currently-open file. Without this, files opened
    # during the scan published diagnostics under the single-file
    # fallback workset (their use-deps surfacing as bogus U007s) and
    # nothing ever re-triggered them. Now that the index is in
    # place, ``_workset_for`` will resolve full topo-sorted
    # closures.
    with _opened_uris_lock:
        opened = list(_opened_uris.values())
    if opened:
        _notify(
            ls,
            f"DimFort: refreshing diagnostics for {len(opened)} open file(s)",
        )
        for uri in opened:
            try:
                with _check_lock:
                    _publish_for_uri(ls, uri)
            except Exception:
                log.exception("post-index refresh failed for %s", uri)


def _update_index_for(path: Path, *, new_text: str | None = None) -> None:
    """Incrementally re-scan one file into the index. No-op when the
    initial build hasn't completed."""
    with _workspace_index_lock:
        idx = _workspace_index
    if idx is None:
        return
    try:
        update_index(idx, path, new_text=new_text)
    except Exception:
        log.exception("workspace index update failed for %s", path)


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def _did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)

    def worker() -> None:
        with _check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didOpen check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-open").start()


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def _did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)
    saved = _uri_to_path(uri)
    if saved is not None:
        _update_index_for(saved.resolve())

    def worker() -> None:
        with _check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didSave check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-save").start()


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
        with _check_lock:
            # Re-check version inside the lock: a later keystroke may
            # have arrived while we were waiting for our turn.
            if not _is_current(uri, version):
                return
            try:
                # Reflect the in-memory buffer in the index so a freshly
                # added `use M` is picked up on the same keystroke.
                active = _uri_to_path(uri)
                if active is not None:
                    _update_index_for(active.resolve(), new_text=text)
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
    """Inlay hints (``[unit]`` ghost text) at variable uses, calls, and member accesses.

    Walks the visible range only — VSCode requests inlays in the
    currently-on-screen range — and pulls each candidate node through
    the ts_checker resolver so the unit-text matches what the
    diagnostic pipeline computes.
    """
    if not _features.inlay_hints:
        return None
    found = _trees_for(params.text_document.uri)
    if found is None:
        return []
    resolved_path, tree, source = found
    with _last_result_lock:
        result = _last_result
    if result is None:
        return []

    visible_start_line = params.range.start.line + 1   # 1-based
    visible_end_line = params.range.end.line + 1

    ctx = _build_ts_ctx(result, source, str(resolved_path))
    ctx.var_types.update(ts_checker.collect_var_types(tree, source))
    ctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))

    seen: set[tuple[int, int]] = set()
    hints: list[lsp.InlayHint] = []

    def _emit(node, unit: Unit | None) -> None:
        if unit is None:
            return
        # Anchor on the node's last column so the hint sits flush against
        # the trailing character of the variable/call.
        er, ec = node.end_point
        line = er + 1
        if line < visible_start_line or line > visible_end_line:
            return
        key = (line, ec)
        if key in seen:
            return
        seen.add(key)
        hints.append(
            lsp.InlayHint(
                position=lsp.Position(line=er, character=ec),
                label=f"[{_unit_pretty(unit)}]",
                kind=lsp.InlayHintKind.Type,
                padding_left=False,
            )
        )

    # Member accesses (a%b, a%b%c) — emit on the whole chain expression.
    for member in _ts_h.walk_member_exprs(tree):
        _emit(member, ts_checker._resolve_member_chain(member, ctx, source))

    # Calls — emit on the full call expression so the [unit] sits past
    # the closing paren.
    for call in _ts_h.walk_calls(tree):
        if call.type == "subroutine_call":
            continue  # subroutines have no return unit
        _emit(call, ts_checker._resolve(call, ctx, source))

    # Plain identifier uses — skip declaration-site identifiers,
    # type-qualifier identifiers, member-expression parts (handled
    # above), and the callee position of a call (the call itself
    # carries the hint).
    for ident in _ts_h.walk_identifiers(tree):
        if _ts_h.is_inside_declaration(ident):
            continue
        if _ts_h.is_inside_type_qualifier(ident):
            continue
        if _ts_h.is_call_callee(ident):
            continue
        # If this identifier is the LHS of a derived-type member, the
        # member-expression hint covers it.
        parent = ident.parent
        if parent is not None and parent.type == "derived_type_member_expression":
            continue
        _emit(ident, ts_checker._resolve(ident, ctx, source))
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
    """Go-to-definition.

    Resolves identifiers and call-callees to their declaration site,
    searching every loaded file's tree-sitter tree. Returns the first
    match — F90's case-insensitive name resolution is implemented by
    a lower-cased compare on both ends.
    """
    if not _features.goto_definition:
        return None
    found = _trees_for(params.text_document.uri)
    if found is None:
        return None
    _, tree, source = found
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None

    line = params.position.line + 1
    col = params.position.character + 1

    # Identify the target: a callee name (under a call), or a plain
    # identifier (a variable use). Prefer the call-callee match because
    # it's more specific.
    target_name: str | None = None
    target_kind: str | None = None  # "var" or "callable"
    for call in _ts_h.walk_calls(tree):
        name = _ts_h.call_name(call, source)
        if name is None:
            continue
        # Match only if the cursor is on the callee identifier (not on
        # an argument inside the call).
        for c in call.children:
            if c.type == "identifier" and _ts_h.node_contains(c, line, col):
                target_name = name
                target_kind = "callable"
                break
        if target_name:
            break
    if target_name is None:
        for ident in _ts_h.walk_identifiers(tree):
            if not _ts_h.node_contains(ident, line, col):
                continue
            if _ts_h.is_inside_type_qualifier(ident):
                continue
            target_name = _ts.node_text(ident, source)
            target_kind = "var"
            break
    if target_name is None:
        return None
    target_lc = target_name.lower()

    # Walk every loaded tree for the matching declaration / function.
    for tree_path, (other_tree, other_source) in result.trees.items():
        if target_kind == "callable":
            for func in _ts_h.walk_function_definitions(other_tree):
                nm = _ts_h.function_definition_name(func, other_source)
                if nm is None:
                    continue
                name, name_node = nm
                if name.lower() != target_lc:
                    continue
                sr, sc = name_node.start_point
                er, ec = name_node.end_point
                return [
                    lsp.Location(
                        uri=_uri_for_path(tree_path),
                        range=lsp.Range(
                            start=lsp.Position(line=sr, character=sc),
                            end=lsp.Position(line=er, character=ec),
                        ),
                    )
                ]
        else:
            for decl, name_node in _ts_h.walk_decl_identifiers(other_tree):
                if _ts.node_text(name_node, other_source).lower() != target_lc:
                    continue
                sr, sc = name_node.start_point
                er, ec = name_node.end_point
                return [
                    lsp.Location(
                        uri=_uri_for_path(tree_path),
                        range=lsp.Range(
                            start=lsp.Position(line=sr, character=sc),
                            end=lsp.Position(line=er, character=ec),
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
    """A code lens above each function/subroutine header showing its signature."""
    if not _features.code_lens:
        return None
    found = _trees_for(params.text_document.uri)
    if found is None:
        return None
    _, tree, source = found
    with _last_result_lock:
        result = _last_result
    if result is None:
        return None

    lenses: list[lsp.CodeLens] = []
    seen_lines: set[int] = set()
    for func in _ts_h.walk_function_definitions(tree):
        nm = _ts_h.function_definition_name(func, source)
        if nm is None:
            continue
        name, _ = nm
        header_line_1based = _ts_h.function_definition_header_line(func)
        if header_line_1based in seen_lines:
            continue
        seen_lines.add(header_line_1based)
        sig = result.signatures.get(name.lower())
        if sig is None:
            continue
        title = _sig_render_md(name, sig).replace("`", "")  # plain text only
        lenses.append(
            lsp.CodeLens(
                range=lsp.Range(
                    start=lsp.Position(line=header_line_1based - 1, character=0),
                    end=lsp.Position(line=header_line_1based - 1, character=0),
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


@server.command("dimfort.checkWorkspace")
def _cmd_check_workspace(ls: LanguageServer, *_args) -> None:
    """Run the active checker backend over every file in the workspace
    index, publishing diagnostics for each. Triggered from the client
    via ``workspace/executeCommand`` (palette command "DimFort: Check
    Whole Workspace").

    The work runs on a daemon thread so the LSP stays responsive. The
    server-wide ``_check_lock`` is held for the duration to avoid
    racing with per-file didOpen/didSave/didChange checks.
    """
    threading.Thread(
        target=_check_whole_workspace,
        args=(ls,),
        daemon=True,
        name="dimfort-check-workspace",
    ).start()


def _check_whole_workspace(ls: LanguageServer) -> None:
    with _workspace_index_lock:
        idx = _workspace_index
    if idx is None:
        _notify(
            ls,
            "DimFort: workspace index not ready yet — wait for the scan "
            "to finish, then try again.",
        )
        return

    files = sorted(idx.uses_by_file.keys())
    if not files:
        _notify(ls, "DimFort: no Fortran files in workspace")
        return

    token = f"dimfort-workspace-check-{int(time.time() * 1000)}"
    progress = ls.work_done_progress
    progress_started = False
    try:
        progress.create(token).result(timeout=2.0)
        progress.begin(
            token,
            lsp.WorkDoneProgressBegin(
                title=f"DimFort: checking workspace ({len(files)} files)",
                cancellable=False,
                percentage=0,
            ),
        )
        progress_started = True
    except Exception:
        log.debug("could not start workDoneProgress for checkWorkspace", exc_info=True)

    _notify(ls, f"DimFort: checking workspace ({len(files)} files)…")

    last_report_at = [0.0]   # mutable via closure
    # Each AST pipeline phase walks every file once. Show the user
    # which phase we're in plus per-file detail so the spinner doesn't
    # look stuck during the post-load passes (which take comparable
    # time on a 2400-file workspace).
    _phase_labels = {
        "load": "loading",
        "index": "indexing modules",
        "check": "checking",
    }

    def on_load_progress(phase: str, scanned: int, total: int, path: Path) -> None:
        if not progress_started:
            return
        now = time.monotonic()
        # Always emit the final tick of each phase; throttle the rest
        # to ~10/sec so we don't flood the wire on a 2400-file workspace.
        if scanned != total and now - last_report_at[0] < 0.1:
            return
        last_report_at[0] = now
        label = _phase_labels.get(phase, phase)
        with contextlib.suppress(Exception):
            progress.report(
                token,
                lsp.WorkDoneProgressReport(
                    message=f"{label} {scanned}/{total} {path.name}",
                    percentage=int(scanned * 100 / total) if total else 100,
                ),
            )

    with _check_lock:
        try:
            result = check_files(
                files,
                external_modules=_external_modules,
                cpp_defines=_project_config.cpp_defines,
                include_paths=_project_config.include_paths,
                progress_cb=on_load_progress,
            )
        except Exception:
            log.exception("workspace check failed")
            if progress_started:
                with contextlib.suppress(Exception):
                    progress.end(
                        token, lsp.WorkDoneProgressEnd(message="failed")
                    )
            return

        with _last_result_lock:
            global _last_result
            _last_result = result

        published = 0
        for path in files:
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
            published += 1
            # Throttle progress reports the same way the scan does.
            if progress_started and (published % 100 == 0 or published == len(files)):
                with contextlib.suppress(Exception):
                    progress.report(
                        token,
                        lsp.WorkDoneProgressReport(
                            message=f"published {published}/{len(files)}",
                            percentage=int(published * 100 / len(files)),
                        ),
                    )

    if progress_started:
        with contextlib.suppress(Exception):
            progress.end(token, lsp.WorkDoneProgressEnd(message="done"))

    h_count = sum(
        1 for diags in result.diagnostics.values() for d in diags
        if d.code.startswith("H")
    )
    u_count = sum(
        1 for diags in result.diagnostics.values() for d in diags
        if d.code.startswith("U")
    )
    _notify(
        ls,
        f"DimFort workspace check complete: {len(files)} files, "
        f"{h_count} H-diags, {u_count} U-diags",
        toast=True,
    )


def run_stdio() -> None:
    # Raise DimFort's own log level so progress messages emitted via
    # ``log.info`` reach handlers. Pygls's root threshold is WARNING;
    # without this, namespace-scoped INFO logs would be silently
    # dropped before reaching the client's output channel.
    logging.getLogger("dimfort").setLevel(logging.INFO)
    server.start_io()
