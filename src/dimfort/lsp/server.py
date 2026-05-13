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
from dimfort.core.diagnostics import Diagnostic, Severity
from dimfort.core.lfortran import walk
from dimfort.core.multifile import WorksetResult, check_files
from dimfort.core.units import format_unit

log = logging.getLogger("dimfort.lsp")

server = LanguageServer("dimfort", __version__)


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


# ---------------------------------------------------------------------------
# URI / position helpers
# ---------------------------------------------------------------------------


def _uri_to_path(uri: str) -> Path | None:
    if not uri.startswith("file:"):
        return None
    return Path(unquote(urlparse(uri).path))


def _path_to_uri(p: Path) -> str:
    return p.as_uri()


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
        source="dimfort",
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
            file_uri = _path_to_uri(path)
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


def _resolve_hover(uri: str, line_1based: int, col_1based: int) -> str | None:
    """Return formatted hover text, or None if nothing useful is here."""
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
                return (
                    f"**{type_name}%{field_name}** — unit "
                    f"`{format_unit(unit)}`"
                )

    # Otherwise look for a Variable declaration or a Var use.
    for node, name in _walk_var_nodes(asr):
        if not _loc_contains(node.get("loc"), line_1based, col_1based, expected):
            continue
        if not name:
            continue
        unit = result.merged_var_units.get(name)
        if unit is not None:
            return f"**{name}** — unit `{format_unit(unit)}`"
        return f"**{name}** — no unit annotation"
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
    log.info("DimFort LSP initialised; workspace folders: %s", folders)


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def _did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    _publish_for_uri(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def _did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    _publish_for_uri(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def _did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
    )


_DEBOUNCE_SECONDS = 0.4


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def _did_change(ls: LanguageServer, params: lsp.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
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
    text = _resolve_hover(uri, line, col)
    if text is None:
        return None
    return lsp.Hover(
        contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=text)
    )


def run_stdio() -> None:
    server.start_io()
