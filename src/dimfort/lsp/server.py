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
- ``textDocument/didClose``: republish the cached workspace
  diagnostics so the Problems panel keeps showing them after the
  file is closed.

Provides:

- ``textDocument/publishDiagnostics`` — H-series + U-series.
- ``textDocument/hover`` — resolved unit for the variable or
  derived-type member under the cursor.

This module is the *spine* of the LSP package: it owns the pygls
``LanguageServer`` instance and every ``@server.feature`` registration, the
lifecycle handlers (``initialize`` / ``initialized`` / document-sync), the
diagnostic publish side (``_publish_for_uri`` / ``_ensure_uri_loaded`` /
``_refresh_inlay_hints``), the feature toggles, and the entry point
(``run_stdio``).

Each ``@server.feature`` handler here is a *thin wrapper*: it does the
feature-flag check, calls ``_ensure_uri_loaded`` if needed, acquires
``state.ts_handler_lock`` if it traverses the cached tree, then delegates to a
logic function in a feature module (``hover`` / ``completion`` / ``definition``
/ ``inlay`` / ``interactions`` / ``code_action`` / ``panel``). Shared logic
lives in ``state`` / ``tree_access`` / ``tree_nav`` / ``decl_scan`` /
``expr_tree`` / ``hover_render`` / ``markers``. See
``docs/design/lsp-architecture.md`` for the full module map and the three
load-bearing patterns (singleton state, handler delegation, lock discipline).

Cross-cutting concerns:

- Handlers go through ``_ensure_uri_loaded(ls, uri)`` first so tab switches
  don't leave them querying a stale workset.
- Tree-walking handlers that read the *cached* tree (hover, definition, inlay)
  acquire ``state.ts_handler_lock`` so they can't race on tree-sitter's
  not-thread-safe traversal. Handlers that parse a *fresh* tree (interactions,
  panel) and code-action do not.
- Mutations of ``state.last_result`` / ``state.workspace_index`` /
  ``state.doc_versions`` / ``state.opened_uris`` are guarded by the matching
  ``state.*_lock`` and never accessed without it.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Any

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from dimfort import __version__
from dimfort.config import load_config
from dimfort.core import (
    unit_config,  # noqa: F401  populates DEFAULT_TABLE
)
from dimfort.core._source_io import FORTRAN_EXTS as _FORTRAN_EXTS
from dimfort.core.cache_store import CacheStore
from dimfort.core.diagnostics import (
    Diagnostic,
    Severity,
    set_severity_overrides,
)
from dimfort.core.multifile import check_files
from dimfort.core.workspace_index import (
    resolve_workset,
    scan_workspace,
    update_index,
)
from dimfort.lsp import (
    code_action,
    completion,
    definition,
    hover,
    inlay,
    interactions,
    panel,
)
from dimfort.lsp.state import DEFAULT_EXTERNAL_MODULES, state
from dimfort.lsp.tree_access import (
    _trees_for,
    _uri_for_path,
    _uri_to_path,
)

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
    # Hover verbosity, a single tri-state applied to every hover surface
    # (call pairing, expression, variable):
    #   "disabled" — no hover at all (the panel is the unit surface)
    #   "short"    — one-line summary
    #   "detailed" — full pairing / unit-algebra tree
    # The side panel is unaffected: it is always detailed, governed only
    # by its own open/closed state.
    hover: str = "short"   # "disabled" | "short" | "detailed"


_features = _FeatureToggles()


_SEVERITY_TO_LSP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}

# Shared mutable server state (locks, caches, config) lives on the single
# ``state`` object imported above. See ``lsp/state.py`` for the concurrency
# contract — in particular ``state.ts_handler_lock`` and ``state.check_lock``.

def _cap_workset(
    paths: list[Path], active: Path, limit: int,
    *, must_keep: frozenset[Path] = frozenset(),
) -> list[Path]:
    """Trim a workset down to ``limit`` entries while keeping the active
    file and any explicit ``must_keep`` entries.

    Topo order alone isn't enough on real codebases: a direct
    callee that DimFort pulled in via the procedure index can sit
    early in the topo sort (its own deps are shallow) and get
    dropped by a naive ``paths[-limit:]`` cap, even though it's
    semantically central to the active file. ``must_keep`` lets the
    caller pin those entries.

    Algorithm: keep the active file plus every ``must_keep`` entry,
    then fill the remaining budget from the topo-last entries that
    aren't already pinned.
    """
    if len(paths) <= limit:
        return paths
    pinned: set[Path] = {active} | {p for p in must_keep if p in paths}
    if len(pinned) >= limit:
        # Pinned set already exceeds the cap; keep all pins, drop
        # everything else. ``active`` is always present.
        return [p for p in paths if p in pinned]
    budget = limit - len(pinned)
    out_set: set[Path] = set(pinned)
    # Walk paths from the end backwards, picking topo-last entries
    # that aren't already pinned, until the budget is exhausted.
    for p in reversed(paths):
        if budget == 0:
            break
        if p in out_set:
            continue
        out_set.add(p)
        budget -= 1
    # Return in topo order so downstream consumers (multifile)
    # process deps before users.
    return [p for p in paths if p in out_set]


def _remember_uri(uri: str) -> None:
    p = _uri_to_path(uri)
    if p is None:
        return
    try:
        resolved = p.resolve()
    except OSError:
        return
    with state.opened_uris_lock:
        state.opened_uris[resolved] = uri


def _forget_uri(uri: str) -> None:
    p = _uri_to_path(uri)
    if p is None:
        return
    try:
        resolved = p.resolve()
    except OSError:
        return
    with state.opened_uris_lock:
        state.opened_uris.pop(resolved, None)


# ---------------------------------------------------------------------------
# URI / position helpers
# ---------------------------------------------------------------------------


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

    with state.workspace_index_lock:
        idx = state.workspace_index

    resolved_active = active.resolve()

    if idx is not None:
        res = resolve_workset(
            idx, [resolved_active], external_modules=state.external_modules
        )
        paths = list(res.compile_order)
        # Belt-and-braces: ensure the active file is present even if it
        # lives outside the indexed roots (e.g. a loose `.f90` outside
        # any workspace folder).
        if resolved_active not in paths:
            paths.append(resolved_active)
        # Pin the active file's direct dependencies (modules used,
        # procedures called) so the cap doesn't drop them. Without
        # this, an external callee can land mid-topo and get cut.
        must_keep: set[Path] = set()
        for use in idx.uses_by_file.get(resolved_active, ()):
            tgt = idx.modules.get(use.module)
            if tgt is not None:
                must_keep.add(tgt)
        for callee in idx.calls_by_file.get(resolved_active, ()):
            tgt = idx.procedures.get(callee)
            if tgt is not None:
                must_keep.add(tgt)
        # Cap to keep the LSP process alive on deep workspaces.
        capped = _cap_workset(
            paths, resolved_active, state.max_workset_size,
            must_keep=frozenset(must_keep),
        )
        if len(capped) < len(paths):
            _notify(
                ls,
                f"DimFort: workset capped at {state.max_workset_size} "
                f"(full deps: {len(paths)}) for {resolved_active.name}",
            )
        return capped, active

    # Fallback: index not ready yet, or no workspace folders. Just
    # check the active file alone. Cross-file deps will surface as
    # transient U007 errors until the index build finishes; that's
    # strictly better than feeding every file in the workspace to
    # the checker — at large scale (~2400 files) the old behaviour
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
            external_modules=state.external_modules,
            cpp_defines=state.project_config.cpp_defines,
            include_paths=state.project_config.include_paths,
            cache=state.cache,
            cache_mode=state.cache_mode,
            units_file=state.project_config.units_file,
            diagnostic_severities=state.project_config.diagnostic_severities,
            scale_mode=state.scale_mode,
        )
    except Exception:
        log.exception("dimfort pipeline crashed on %s", active)
        return

    with state.last_result_lock:
        state.last_result = result

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
    _refresh_inlay_hints(ls)


def _refresh_inlay_hints(ls: LanguageServer) -> None:
    """Ask the client to re-query inlay hints for every open buffer.

    The client may issue a ``textDocument/inlayHint`` request *before*
    the server's initial workspace check has populated
    ``state.last_result``; that early request returns empty and the
    client caches "no hints". Without this nudge the user has to
    perform a buffer edit to coax the client into re-querying. The
    method is opt-in via the LSP spec (``workspace.inlayHint.refreshSupport``),
    so we fire it unconditionally and let the framework drop it when
    the client didn't advertise support.
    """
    with contextlib.suppress(Exception):
        ls.workspace_inlay_hint_refresh(None)


def _bump_version(uri: str) -> int:
    with state.doc_versions_lock:
        state.doc_versions[uri] = state.doc_versions.get(uri, 0) + 1
        return state.doc_versions[uri]


def _is_current(uri: str, version: int) -> bool:
    with state.doc_versions_lock:
        return state.doc_versions.get(uri) == version


# ---------------------------------------------------------------------------
# Tree access for handlers (workset-cache lookup + ctx builder)
# ---------------------------------------------------------------------------


def _ensure_uri_loaded(ls: LanguageServer, uri: str) -> None:
    """Re-publish for ``uri`` if its tree isn't in ``state.last_result``.

    The LSP keeps a single global ``state.last_result``, updated on every
    didOpen / didSave / didChange. When the user navigates between
    open tabs, VSCode doesn't fire any LSP event — but the last
    publish may have been for a *different* active file whose
    workset doesn't include the now-active one (typical when the
    user jumped from a caller to a callee via goto-def: the
    callee's workset is downward-only and doesn't loop back).

    Detect that by asking ``_trees_for`` and, if it returns None
    for what's actually a known Fortran file, fire a synchronous
    publish for the URI so the next hover / goto-def / inlay
    request sees fresh trees.
    """
    if _trees_for(uri) is not None:
        return
    path = _uri_to_path(uri)
    if path is None or not path.is_file():
        return
    if path.suffix.lower() not in _FORTRAN_EXTS:
        return
    with state.check_lock:
        _publish_for_uri(ls, uri)


# ---------------------------------------------------------------------------
# Lifecycle handlers (initialize, initialized, background index build)
# ---------------------------------------------------------------------------


@server.feature(lsp.INITIALIZE)
def _initialize(ls: LanguageServer, params: lsp.InitializeParams) -> None:
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
    state.workspace_folders = folders

    # Load .dimfort.toml from the first workspace folder, if any.
    if folders:
        state.project_config = load_config(folders[0])
    config = state.project_config

    # Project-specific unit table (projects ship a ``*_units.toml`` with
    # ``degree``, ``hPa``, ``day``, etc.). Install before any check
    # fires so var_units parsing doesn't drop those annotations.
    if config.units_file is not None:
        from dimfort.core import unit_config as _unit_config
        _unit_config.install_default(config.units_file)

    # Start from config-provided values; initializationOptions override.
    _external_modules_from_config = DEFAULT_EXTERNAL_MODULES | frozenset(
        config.external_modules
    )
    opts = params.initialization_options or {}
    state.external_modules = _external_modules_from_config
    if config.max_workset_size is not None:
        state.max_workset_size = config.max_workset_size
    # Scale mode: config default, optionally overridden per-client.
    state.scale_mode = config.scale_mode

    # [diagnostics] severity overrides are applied by finalize_diagnostics
    # via a process-wide global. The CLI sets it; the LSP must too, or
    # editor diagnostics silently ignore every [diagnostics] override.
    set_severity_overrides(config.diagnostic_severities)

    if isinstance(opts, dict):
        _features.inlay_hints = bool(opts.get("inlayHintsEnabled", True))
        _features.completion = bool(opts.get("completionEnabled", True))
        _features.code_actions = bool(opts.get("codeActionsEnabled", True))
        _features.goto_definition = bool(opts.get("gotoDefinitionEnabled", True))
        # Single tri-state hover verbosity. Accept the legacy
        # ``traceHoverEnabled`` / per-surface keys defensively in case an
        # older client connects, but the modern key is ``hover``.
        hv = opts.get("hover")
        if hv in ("disabled", "short", "detailed"):
            _features.hover = hv
        else:
            # Back-compat with pre-1.x clients: traceHoverEnabled=true or
            # any per-surface "detailed" means detailed; else short.
            legacy_detailed = bool(opts.get("traceHoverEnabled", False)) or any(
                opts.get(k) == "detailed"
                for k in ("hoverFunctionCalls", "hoverSubroutineCalls",
                          "hoverExpressions")
            )
            _features.hover = "detailed" if legacy_detailed else "short"
        # Init options extend whatever config already contributed.
        extra = opts.get("externalModules")
        if isinstance(extra, list):
            state.external_modules = state.external_modules | frozenset(
                str(m).lower() for m in extra if isinstance(m, str)
            )
        cap = opts.get("maxWorksetSize")
        if isinstance(cap, int) and cap > 0:
            state.max_workset_size = cap

        # Opt-in scale checking: initializationOption overrides config.
        scale_opt = opts.get("scaleMode")
        if isinstance(scale_opt, bool):
            state.scale_mode = scale_opt

        # Content-hash cache: opt-in via initializationOptions. The
        # cache directory defaults to ``.dimfort-cache/`` under the
        # first workspace folder; clients can override with cacheDir.
        requested = opts.get("cacheMode", "off")
        if requested in ("off", "read-only", "read-write") and folders:
            state.cache_mode = requested
            if requested != "off":
                from dimfort.core.cache_store import default_cache_dir
                cache_dir_opt = opts.get("cacheDir")
                cache_root = (
                    Path(cache_dir_opt) if isinstance(cache_dir_opt, str)
                    else default_cache_dir(folders[0])
                )
                state.cache = CacheStore(root=cache_root)

    config_note = (
        f" (config: {config.config_path.name})"
        if config.config_path is not None
        else ""
    )
    _notify(
        ls,
        f"DimFort LSP initialised — {len(folders)} folder(s), "
        f"{len(state.external_modules)} external module(s) on allowlist"
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
    folders = state.workspace_folders
    if not folders:
        return
    # If ``.dimfort.toml`` narrows the source tree via [project].src_paths,
    # scan only those subdirectories. Otherwise scan every workspace
    # folder. Useful on large monorepos where DimFort cares about a
    # handful of subtrees.
    scan_roots = tuple(state.project_config.src_paths) or tuple(folders)
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

    with state.workspace_index_lock:
        state.workspace_index = idx

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
    with state.opened_uris_lock:
        opened = list(state.opened_uris.values())
    if opened:
        _notify(
            ls,
            f"DimFort: refreshing diagnostics for {len(opened)} open file(s)",
        )
        for uri in opened:
            try:
                with state.check_lock:
                    _publish_for_uri(ls, uri)
            except Exception:
                log.exception("post-index refresh failed for %s", uri)


def _update_index_for(path: Path, *, new_text: str | None = None) -> None:
    """Incrementally re-scan one file into the index. No-op when the
    initial build hasn't completed."""
    with state.workspace_index_lock:
        idx = state.workspace_index
    if idx is None:
        return
    try:
        update_index(idx, path, new_text=new_text)
    except Exception:
        log.exception("workspace index update failed for %s", path)


# ---------------------------------------------------------------------------
# Document-sync handlers (did_open / did_save / did_close / did_change)
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def _did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)

    def worker() -> None:
        with state.check_lock:
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
        with state.check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didSave check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-save").start()


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def _did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    uri = params.text_document.uri
    path = _uri_to_path(uri)
    _forget_uri(uri)

    # DimFort is a workspace-wide checker, so a file's diagnostics
    # remain true after the user closes it. Republish the cached
    # workspace-check diagnostics instead of clearing them; clear only
    # if we have nothing on file (e.g. a single-file workset whose
    # entry no longer applies).
    cached: list[Diagnostic] = []
    if path is not None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = None
        if resolved is not None:
            with state.last_result_lock:
                result = state.last_result
            if result is not None:
                cached = result.diagnostics.get(resolved, [])

    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(
            uri=uri, diagnostics=[_to_lsp_diagnostic(d) for d in cached]
        )
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
        with state.check_lock:
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


# ---------------------------------------------------------------------------
# Hover handler (registration; dispatch + rendering live in lsp/hover.py)
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def _hover(ls: LanguageServer, params: lsp.HoverParams) -> Any:
    # Hover disabled entirely (the panel is the unit surface). Bail
    # before doing any work.
    if _features.hover == "disabled":
        return None
    # Tab switches to a different open document don't fire any LSP
    # event, but their workset may not include the now-active file.
    # Trigger a fresh publish before reading trees.
    _ensure_uri_loaded(ls, params.text_document.uri)
    with state.ts_handler_lock:
        uri = params.text_document.uri
        # LSP positions are 0-based; our internal helpers are 1-based.
        line = params.position.line + 1
        col = params.position.character + 1
        source_text: str | None = None
        try:
            source_text = ls.workspace.get_text_document(uri).source
        except Exception:
            log.debug("could not fetch buffer text for %s", uri)
        hit = hover.resolve(uri, line, col, source_text, hover_mode=_features.hover)
        if hit is None:
            return None
        text, range_ = hit
        return lsp.Hover(
            contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=text),
            range=range_,
        )


_VERDICT_TO_MARKER = {
    "homogeneous": "🟢",
    "autocast": "🟢",
    "wrapper_untag": "🟡",
    "mismatch": "🔴",
    "unresolved": "🟡",
}


# ---------------------------------------------------------------------------
# Side-panel info — structured-tree builders for the dimfort/panelInfo
# request. The two functions below mirror _render_ast_tree's resolution
# logic but return data instead of rendered strings, so each editor's
# side panel can lay it out in its own idiom (Nvim split, Emacs window,
# VSCode webview). See docs/design/panel-info.md.
# ---------------------------------------------------------------------------




@server.feature("dimfort/panelInfo")
def _panel_info(ls: LanguageServer, params) -> dict | None:
    """Return the side-panel payload for ``(uri, position)``.

    See docs/design/panel-info.md for the data model. Stateless:
    reads from the last cached WorksetResult, computes the response
    on the fly.
    """
    return panel.resolve(ls, params)


@server.feature("dimfort/interactions")
def _interactions(ls: LanguageServer, params) -> dict | None:
    """Cross-site unit analysis for the symbol under the cursor.

    Resolves the identifier at ``(uri, position)`` (or an explicit
    ``symbol`` param), then runs :func:`collect_interactions` over the
    cached workset and returns the report. See
    docs/design/interaction-points.md.
    """
    return interactions.resolve(ls, params)


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
    # Tab-switch safety: re-publish if the URI isn't currently loaded.
    _ensure_uri_loaded(ls, params.text_document.uri)
    if not _features.inlay_hints:
        return None
    # See state.ts_handler_lock declaration: tree-sitter's C library isn't
    # thread-safe for concurrent traversal; serialise alongside hover
    # and definition.
    with state.ts_handler_lock:
        return inlay.resolve(params)


# ---------------------------------------------------------------------------
# Completion inside `@unit{…}`
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=["{", " ", "/", "*", "^"]),
)
def _completion(
    ls: LanguageServer, params: lsp.CompletionParams
) -> lsp.CompletionList | None:
    if not _features.completion:
        return None
    return completion.complete(ls, params)


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
    # Tab-switch safety: re-publish if the URI isn't currently loaded.
    _ensure_uri_loaded(ls, params.text_document.uri)
    if not _features.goto_definition:
        return None
    # See state.ts_handler_lock declaration: tree-sitter's C library isn't
    # thread-safe for concurrent traversal; Cmd-hover fires hover +
    # definition simultaneously and was triggering native-level
    # crashes. Serialise.
    with state.ts_handler_lock:
        return definition.resolve(params)


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
    return code_action.resolve(ls, params)


@server.command("dimfort.checkWorkspace")
def _cmd_check_workspace(ls: LanguageServer, *_args) -> None:
    """Run the active checker backend over every file in the workspace
    index, publishing diagnostics for each. Triggered from the client
    via ``workspace/executeCommand`` (palette command "DimFort: Check
    Whole Workspace").

    The work runs on a daemon thread so the LSP stays responsive. The
    server-wide ``state.check_lock`` is held for the duration to avoid
    racing with per-file didOpen/didSave/didChange checks.
    """
    threading.Thread(
        target=_check_whole_workspace,
        args=(ls,),
        daemon=True,
        name="dimfort-check-workspace",
    ).start()


def _check_whole_workspace(ls: LanguageServer) -> None:
    with state.workspace_index_lock:
        idx = state.workspace_index
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

    with state.check_lock:
        try:
            result = check_files(
                files,
                external_modules=state.external_modules,
                cpp_defines=state.project_config.cpp_defines,
                include_paths=state.project_config.include_paths,
                progress_cb=on_load_progress,
                cache=state.cache,
                cache_mode=state.cache_mode,
                units_file=state.project_config.units_file,
                diagnostic_severities=state.project_config.diagnostic_severities,
                scale_mode=state.scale_mode,
            )
        except Exception:
            log.exception("workspace check failed")
            if progress_started:
                with contextlib.suppress(Exception):
                    progress.end(
                        token, lsp.WorkDoneProgressEnd(message="failed")
                    )
            return

        with state.last_result_lock:
            state.last_result = result

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

    _refresh_inlay_hints(ls)

    h_count = sum(
        1 for diags in result.diagnostics.values() for d in diags
        if d.code.startswith("H")
    )
    u_count = sum(
        1 for diags in result.diagnostics.values() for d in diags
        if d.code.startswith("U")
    )
    total_seconds = result.phase_timings.get("total")
    timing = f" in {total_seconds:.1f} s" if total_seconds is not None else ""
    cache_note = ""
    if state.cache is not None and (
        result.cache_hits or result.cache_misses or result.cache_dirty
    ):
        cache_note = (
            f" [cache: {result.cache_hits} hit / "
            f"{result.cache_misses} miss / {result.cache_dirty} dirty]"
        )
    _notify(
        ls,
        f"DimFort workspace check complete: {len(files)} files, "
        f"{h_count} H-diags, {u_count} U-diags{timing}{cache_note}",
        toast=True,
    )


def run_stdio() -> None:
    # Raise DimFort's own log level so progress messages emitted via
    # ``log.info`` reach handlers. Pygls's root threshold is WARNING;
    # without this, namespace-scoped INFO logs would be silently
    # dropped before reaching the client's output channel.
    logging.getLogger("dimfort").setLevel(logging.INFO)
    _install_crash_trace_hook()
    server.start_io()


def _install_crash_trace_hook() -> None:
    """Wire ``sys.excepthook`` + ``threading.excepthook`` to a crash log.

    LSP stdio mode can lose stderr (the client doesn't forward it,
    pygls may not flush before process death, etc.). Writing
    tracebacks to a known file makes the next silent crash
    actionable. Default log path is ``/tmp/dimfort-lsp.crash``;
    override via ``DIMFORT_CRASH_LOG`` env var. Disable entirely
    with ``DIMFORT_CRASH_LOG=`` (empty value).

    Deliberately does NOT touch asyncio's loop policy — pygls owns
    that, and meddling with it broke server startup on Python 3.14.
    Most pygls feature handlers run on the asyncio loop, so an
    unhandled exception there typically dies via the loop's default
    handler which prints to stderr. If a future crash isn't caught
    by sys.excepthook + threading.excepthook, the next step is to
    wrap individual feature handlers in try/except locally rather
    than instrument the loop globally.
    """
    import os
    import sys
    import traceback

    env = os.environ.get("DIMFORT_CRASH_LOG")
    if env is None:
        path = "/tmp/dimfort-lsp.crash"
    elif env == "":
        return
    else:
        path = env

    def _write(header: str, body: str) -> None:
        try:
            with open(path, "a") as f:
                f.write(f"\n=== {header} ===\n{body}\n")
                f.flush()
        except Exception:  # noqa: BLE001 — diagnostic path; can't help if write fails
            pass

    def _hook(exc_type, exc_value, exc_tb) -> None:
        _write(
            "sys.excepthook",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

    sys.excepthook = _hook
    # Threads (pygls runs synchronous features in a thread pool).
    # Without this, exceptions from a worker thread don't reach
    # excepthook on older Pythons.
    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            _write(
                f"thread {args.thread.name}",
                "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback,
                )),
            )
        threading.excepthook = _thread_hook

    # Pygls wraps feature handler bodies in its own try/except and
    # converts exceptions into JSON-RPC error responses. Those
    # exceptions are logged through the ``pygls`` logger but never
    # reach sys.excepthook / threading.excepthook. Attach a stream
    # handler that mirrors ERROR-level logs into our crash file so
    # we capture them too.
    class _CrashFileHandler(logging.Handler):
        def emit(self, record):  # type: ignore[override]
            try:
                msg = self.format(record)
            except Exception:  # noqa: BLE001
                msg = record.getMessage()
            _write(f"pygls logger {record.name}/{record.levelname}", msg)

    crash_handler = _CrashFileHandler(level=logging.ERROR)
    crash_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s\n"
        )
    )
    # ``pygls`` covers handler-wrap errors; ``asyncio`` covers the
    # loop's default exception handler (which logs unhandled
    # task exceptions). Attach to both.
    logging.getLogger("pygls").addHandler(crash_handler)
    logging.getLogger("asyncio").addHandler(crash_handler)

    _write("startup", "crash hook installed; logging to this file")
