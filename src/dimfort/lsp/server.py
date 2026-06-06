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
- ``textDocument/definition`` — go-to-definition for declared symbols.
- ``textDocument/inlayHint`` — inferred-unit inlay hints.
- ``textDocument/completion`` — unit-name completion inside
  ``@unit{…}`` (and configured equivalents) on bare-``!`` comments.
- ``textDocument/codeAction`` — quick-fixes (add ``@unit{}``,
  extract literal to PARAMETER, U002 replace-with-suggestion).
- ``dimfort/panelInfo`` — side-panel rendering data.
- ``dimfort/interactions`` — read/write/contributor sites for a symbol.
- ``dimfort.checkWorkspace`` — re-run the pipeline over the workset.

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
from dimfort.core.unit_patterns import (
    compile_structured_patterns,
    compile_unit_patterns,
)
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
    """Surface a progress message to the Python logger and the LSP client.

    Mirrors a single line of operational text into two channels: the
    project's ``dimfort.lsp`` logger (so it lands wherever Python
    logging is configured) and the client's output panel via
    ``window/logMessage``. Pygls filters ``log.info`` below WARNING by
    default, so user-relevant events would otherwise be invisible in
    VSCode's "DimFort Language Server" output channel.

    Failures from the client-side calls are swallowed: a stuck or
    half-initialised client must never crash a background worker.

    Args:
        ls: Active language server instance, or ``None`` when the
            caller has no handle (e.g. early lifecycle paths). In the
            ``None`` case only the Python logger sees the message.
        message: Human-readable text. Should already be prefixed with
            ``DimFort:`` for output-channel consistency.
        toast: When ``True``, additionally emit ``window/showMessage``
            so a status-bar popup appears for unblock signals worth
            interrupting the user (e.g. "workspace index ready").

    Returns:
        None. All side effects are out-of-band notifications.

    Note:
        Both ``window_log_message`` and ``window_show_message`` are
        wrapped in a broad ``try/except`` because pygls raises on a
        disconnected client and we never want a notification path to
        propagate.
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
    """Per-feature on/off (and hover verbosity) flags resolved at initialize-time.

    The ``initialize`` handler reads ``initializationOptions`` and
    flips these fields once; every feature handler reads them on the
    hot path to decide whether to do any work. Defaults are
    on-for-everything so a client that ignores ``initializationOptions``
    still gets the full feature set.

    Attributes:
        inlay_hints: When ``False``, ``textDocument/inlayHint`` returns
            ``None`` without touching the tree.
        completion: When ``False``, ``textDocument/completion`` returns
            ``None`` (no unit-name suggestions inside ``@unit{...}``).
        code_actions: When ``False``, ``textDocument/codeAction``
            returns ``None`` (no quick-fixes).
        goto_definition: When ``False``, ``textDocument/definition``
            returns ``None``.
        hover: Tri-state verbosity (``"disabled"`` / ``"short"`` /
            ``"detailed"``) applied uniformly to every hover surface
            (call pairing, expression, variable). The side panel is
            unaffected — it is always detailed, governed only by its
            own open/closed state.

    Note:
        This is a class-level state holder, not a dataclass; mutation
        happens via the module-level ``_features`` singleton.
    """

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
    """Trim a workset to ``limit`` entries while pinning load-bearing files.

    Topo order alone isn't enough on real codebases: a direct callee
    that DimFort pulled in via the procedure index can sit early in
    the topo sort (its own deps are shallow) and get dropped by a
    naive ``paths[-limit:]`` cap, even though it's semantically
    central to the active file. ``must_keep`` lets the caller pin
    those entries.

    Algorithm: keep the active file plus every ``must_keep`` entry,
    then fill the remaining budget from the topo-last entries that
    aren't already pinned. The returned list preserves the input
    topo order so downstream consumers (``multifile.check_files``)
    process dependencies before their users.

    Args:
        paths: Candidate workset in dependency-first topo order.
        active: The file currently focused in the editor; always
            retained.
        limit: Maximum number of paths allowed in the returned list;
            must be a positive integer.
        must_keep: Optional frozenset of additional paths that must
            survive the cap (e.g. directly-used modules, direct
            callees). Entries outside ``paths`` are ignored.

    Returns:
        A list of paths, length at most ``limit`` (or larger if the
        pinned set alone already exceeds ``limit``), preserving the
        original topo ordering.

    Note:
        When the pinned set already exceeds ``limit``, every pin is
        kept and the cap is effectively widened — soundness trumps
        the soft budget.
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
    """Record that ``uri`` is currently open in the editor.

    Maintains ``state.opened_uris`` (resolved-path → original-URI
    mapping) under ``state.opened_uris_lock``. The map is used by the
    post-index-build refresh to know which buffers to re-check, and
    by close-handling to forget URIs cleanly.

    No-ops silently when the URI cannot be turned into a real
    filesystem path or when ``Path.resolve`` raises ``OSError``
    (e.g. a network share that just went away).

    Args:
        uri: The ``textDocument.uri`` from a didOpen/didChange/didSave
            notification.

    Returns:
        None.
    """
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
    """Remove ``uri`` from the opened-URI map.

    The inverse of :func:`_remember_uri`. Called from the
    ``textDocument/didClose`` handler. Silently no-ops when the URI
    isn't a real path or when ``Path.resolve`` raises.

    Args:
        uri: The ``textDocument.uri`` from a didClose notification.

    Returns:
        None.
    """
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
    """Convert a DimFort core diagnostic into an LSP wire-format diagnostic.

    Bridges the two coordinate systems: DimFort cores are 1-based
    (line/column from tree-sitter), LSP is 0-based. Also ensures a
    minimum one-character range so the squiggle is visible even when
    the source span is degenerate (start == end).

    The ``suggested_rewrite`` payload, when present, is forwarded via
    the LSP ``data`` field. The code-action provider reads it back
    out of ``params.context.diagnostics`` to materialise a
    "Replace with ..." quick-fix (spec §12 + task #9).

    Args:
        d: A DimFort core :class:`Diagnostic` instance carrying
            source position, severity, code, message, and an optional
            ``suggested_rewrite`` payload.

    Returns:
        An :class:`lsprotocol.types.Diagnostic` ready for inclusion
        in a ``textDocument/publishDiagnostics`` payload.

    Note:
        Unknown severities fall back to ``Error`` rather than being
        dropped — silent dropping would mask checker bugs.
    """
    start_line = max(d.start.line - 1, 0)
    start_col = max(d.start.column - 1, 0)
    end_line = max(d.end.line - 1, 0)
    end_col = max(d.end.column - 1, 0)
    if (end_line, end_col) <= (start_line, start_col):
        end_col = start_col + 1
    data: dict[str, str] | None = None
    if d.suggested_rewrite is not None:
        # Carried into the code-action provider via `params.context.
        # diagnostics`; spec §12 + task #9 turn it into a "Replace
        # with …" quick-fix.
        data = {"suggested_rewrite": d.suggested_rewrite}
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=start_line, character=start_col),
            end=lsp.Position(line=end_line, character=end_col),
        ),
        severity=_SEVERITY_TO_LSP.get(d.severity, lsp.DiagnosticSeverity.Error),
        code=d.code,
        source="DimFort",
        message=d.message,
        data=data,
    )


# ---------------------------------------------------------------------------
# Workspace traversal
# ---------------------------------------------------------------------------


def _discover_fortran_files(roots: list[Path]) -> list[Path]:
    """Walk every workspace folder and collect Fortran source files.

    Recursively descends each root and gathers files whose suffix
    appears in ``_FORTRAN_EXTS`` (the canonical Fortran-extension
    set from ``core._source_io``). Deduplicates by resolved path so
    a folder that is itself a symlink target doesn't double-count.

    Non-directory roots are silently skipped — callers may pass a
    mixed list without filtering up-front.

    Args:
        roots: Absolute or relative directories to scan. Entries
            that aren't directories are ignored.

    Returns:
        A list of resolved, deduplicated Fortran source paths in
        first-seen order across roots.

    Note:
        This is the fallback discovery path; the primary discovery
        flow uses ``scan_workspace`` to also build the use/module
        index. Kept for handlers that just need the file list.
    """
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
    """Return the workset of paths plus the active path for ``active_uri``.

    Uses the workspace index to follow ``use``-statement dependencies
    from the active file when the index is ready. Falls back to the
    single-file workset (active alone) when the index hasn't been
    built yet — that's strictly better than feeding every file in
    the workspace to the checker, which would SIGKILL the LSP via
    macOS jetsam on ~2400-file workspaces.

    The active file is always present in the returned workset, even
    when it lives outside any indexed workspace root (e.g. the user
    opened a loose ``.f90``). The function also pins directly-used
    modules and called procedures so the workset cap can't drop
    them mid-topo.

    Args:
        ls: Active language server, passed through to
            :func:`_notify` for the optional "workset capped"
            heads-up.
        active_uri: ``textDocument.uri`` for the file the user is
            focused on.

    Returns:
        A ``(paths, active)`` tuple. ``paths`` is the topo-ordered
        workset (possibly capped). ``active`` is the resolved
        :class:`Path` for ``active_uri``, or ``None`` when the URI
        doesn't map to a file on disk.

    Note:
        Reads ``state.workspace_index`` under
        ``state.workspace_index_lock``; the lock is released before
        the resolve call so we don't hold it across the
        :func:`resolve_workset` traversal.
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
    """Run the pipeline for ``uri``'s workset and publish per-file diagnostics.

    Builds the workset via :func:`_workset_for`, runs
    :func:`check_files` with all current project settings (CPP
    defines, include paths, cache, units file, severity overrides,
    scale mode, comment-delimiter patterns), stores the result in
    ``state.last_result`` for later cached reads (didClose, panel,
    interactions), then publishes one ``publishDiagnostics`` payload
    per file in the workset.

    Files with no diagnostics still receive an empty publish so
    stale squiggles clear immediately.

    Args:
        ls: Active language server (used for the publish and for
            ``_workset_for``'s notification path).
        uri: URI of the currently-active document driving the
            workset computation.
        override_text: When non-``None``, supplies the in-memory
            buffer text for the active file (used by didChange so
            unsaved edits flow through the pipeline). Other files in
            the workset are read from disk.

    Returns:
        None. All output goes through ``publishDiagnostics``.

    Note:
        Swallows pipeline exceptions after logging them; a single
        checker crash must not take down the LSP. The caller is
        expected to hold ``state.check_lock`` so concurrent
        publishes don't race on shared state.
    """
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
            unit_patterns=compile_unit_patterns(
                state.project_config.unit_comment_delimiters
            ),
            assume_patterns=compile_structured_patterns(
                state.project_config.unit_assume_comment_delimiters
            ),
            affine_patterns=compile_structured_patterns(
                state.project_config.unit_affine_comment_delimiters
            ),
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
    method is opt-in via the LSP spec
    (``workspace.inlayHint.refreshSupport``), so we fire it
    unconditionally and let the framework drop it when the client
    didn't advertise support.

    Args:
        ls: Active language server instance.

    Returns:
        None.

    Note:
        Implements ``workspace/inlayHint/refresh``. Errors are
        swallowed via ``contextlib.suppress`` because not every
        client supports the request, and a refusal must not
        propagate.
    """
    with contextlib.suppress(Exception):
        ls.workspace_inlay_hint_refresh(None)


def _bump_version(uri: str) -> int:
    """Increment and return the per-URI document version counter.

    Used by the debounced didChange handler to detect superseded
    keystrokes: each call returns a strictly-increasing version
    number, and :func:`_is_current` checks whether a previously
    captured version is still the latest.

    Args:
        uri: ``textDocument.uri`` of the buffer being edited.

    Returns:
        The new (post-increment) version number, starting at 1.

    Note:
        Holds ``state.doc_versions_lock`` for the read-modify-write
        cycle so concurrent didChange notifications can't collide.
    """
    with state.doc_versions_lock:
        state.doc_versions[uri] = state.doc_versions.get(uri, 0) + 1
        return state.doc_versions[uri]


def _is_current(uri: str, version: int) -> bool:
    """Return whether ``version`` is still the latest seen for ``uri``.

    The debounced didChange path captures the version returned by
    :func:`_bump_version`, sleeps for the debounce interval, then
    calls this to decide whether to fire a check or bail out as
    superseded by a later keystroke. Checked twice (before and
    inside the ``state.check_lock`` critical section) so a keystroke
    that arrives while the worker is waiting on the lock doesn't get
    a stale publish.

    Args:
        uri: ``textDocument.uri`` of the buffer in question.
        version: A version number previously returned by
            :func:`_bump_version`.

    Returns:
        ``True`` when ``version`` equals the current stored version
        for ``uri``, ``False`` otherwise (including when the URI was
        never recorded).

    Note:
        Holds ``state.doc_versions_lock`` for the duration of the
        read.
    """
    with state.doc_versions_lock:
        return state.doc_versions.get(uri) == version


# ---------------------------------------------------------------------------
# Tree access for handlers (workset-cache lookup + ctx builder)
# ---------------------------------------------------------------------------


def _ensure_uri_loaded(ls: LanguageServer, uri: str) -> None:
    """Re-publish for ``uri`` if its tree isn't in ``state.last_result``.

    The LSP keeps a single global ``state.last_result``, updated on
    every didOpen / didSave / didChange. When the user navigates
    between open tabs, VSCode doesn't fire any LSP event — but the
    last publish may have been for a *different* active file whose
    workset doesn't include the now-active one (typical when the
    user jumped from a caller to a callee via goto-def: the callee's
    workset is downward-only and doesn't loop back).

    Detect that by asking :func:`_trees_for` and, if it returns
    ``None`` for what's actually a known Fortran file, fire a
    synchronous publish for the URI so the next hover / goto-def /
    inlay request sees fresh trees.

    Args:
        ls: Active language server, forwarded to
            :func:`_publish_for_uri`.
        uri: ``textDocument.uri`` for which fresh trees are needed.

    Returns:
        None. Either no-ops or performs a synchronous workspace
        check.

    Note:
        Acquires ``state.check_lock`` around the publish to serialise
        with the debounced didChange path. Skips entirely when the
        URI does not resolve to an on-disk Fortran source (non-file
        URIs, non-Fortran extensions).
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
    """Implements ``initialize``: capture workspace + apply client settings.

    Reads the workspace folders (or legacy ``root_uri``) into
    ``state.workspace_folders``, loads ``.dimfort.toml`` from the
    first folder, then layers ``initializationOptions`` on top per
    the documented precedence (config < init-options < CLI).
    Installs project-specific unit tables, the process-wide
    severity overrides, the feature toggles, the external-modules
    allowlist, the workset cap, the scale-mode flag, and (when
    requested) the content-hash cache.

    Args:
        ls: Active language server.
        params: LSP ``InitializeParams`` carrying workspace folders
            and ``initializationOptions``.

    Returns:
        None. All effects mutate module-level ``state`` /
        ``_features`` singletons.

    Note:
        Accepts legacy hover keys (``traceHoverEnabled``,
        ``hoverFunctionCalls`` etc.) defensively for pre-1.x
        clients. The modern key is a single ``hover`` tri-state.
    """
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
    """Implements ``initialized``: kick off the background workspace scan.

    The workspace scan needs to send server-to-client requests
    (``window/workDoneProgress/create``); these are only valid after
    the client has sent the ``initialized`` notification. Spawning
    earlier — e.g. from inside the ``initialize`` handler — races
    against the client's readiness and produces
    ``JsonRpcMethodNotFound`` responses.

    Picks scan roots from ``.dimfort.toml``'s ``[project].src_paths``
    when configured (useful on large monorepos where only a few
    subtrees concern DimFort); otherwise falls back to every
    workspace folder.

    Args:
        ls: Active language server.
        params: LSP ``InitializedParams`` (no payload of interest).

    Returns:
        None. Spawns the ``dimfort-workspace-scan`` daemon thread.

    Note:
        No-ops when there are no workspace folders (single-file
        mode).
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
    """Background workspace scan; populates ``state.workspace_index``.

    Walks ``roots`` via :func:`scan_workspace` to build the
    ``WorkspaceIndex`` (modules, procedures, uses-by-file,
    calls-by-file). Emits ``$/progress`` notifications so VSCode
    shows a status-bar spinner with per-file detail; reports are
    throttled to ~10/sec so a 2435-file scan doesn't flood the
    wire. After the index lands, every currently-open buffer is
    re-checked so files that opened during the scan get their
    cross-file deps surfaced (otherwise their use-deps would stay
    as bogus U007s until the next keystroke).

    Args:
        ls: Active language server, used for progress + notify.
        roots: Workspace roots (resolved earlier from
            ``state.project_config.src_paths`` or the workspace
            folders).

    Returns:
        None. Mutates ``state.workspace_index`` under
        ``state.workspace_index_lock``.

    Note:
        Pipeline crashes are logged but do not propagate — a broken
        index is bad, a crashed LSP process is worse.
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
        """Forward a per-file scan tick to the LSP progress channel.

        Args:
            scanned: Number of files processed so far (1-based at
                the first tick).
            total: Total file count for the scan.
            path: The file currently being processed.

        Returns:
            None.

        Note:
            Throttles to roughly 10 reports per second; always
            emits the final tick (``scanned == total``) so the
            spinner closes cleanly.
        """
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
    """Incrementally re-scan one file into the workspace index.

    No-ops when the initial scan hasn't completed (the file will be
    covered when the full build finishes).

    The lock is held **across** :func:`update_index` (not just across
    the reference read) because that call mutates the underlying
    ``modules`` / ``uses_by_file`` / ``procedures`` dicts in place.
    Concurrent readers (:func:`resolve_workset`,
    :func:`_check_whole_workspace`) iterate those same dicts under
    the same lock; without holding it through the mutation the
    readers would race on a partial-state window or trip "dict
    changed size during iteration" mid-traverse.

    Args:
        path: Absolute path of the file to re-scan.
        new_text: When non-``None``, supplies in-memory buffer text
            (used by didChange so a freshly-added ``use M`` is
            picked up on the same keystroke). When ``None``, the
            file is re-read from disk.

    Returns:
        None.

    Note:
        Failures inside :func:`update_index` are logged but
        swallowed — a partial-state index is preferable to a
        crashed LSP.
    """
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
    """Implements ``textDocument/didOpen``: record + check the new buffer.

    Remembers the URI in ``state.opened_uris`` (so the post-index
    refresh knows to re-check it) and spawns a daemon worker that
    runs the pipeline under ``state.check_lock`` so it serialises
    with other checks.

    Args:
        ls: Active language server.
        params: LSP ``DidOpenTextDocumentParams``.

    Returns:
        None.

    Note:
        The publish happens off the main asyncio loop so the LSP
        stays responsive on first-open of a deep workspace.
    """
    uri = params.text_document.uri
    _remember_uri(uri)

    def worker() -> None:
        """Run the pipeline for ``uri`` under ``state.check_lock``.

        Returns:
            None.

        Note:
            Logs and swallows pipeline crashes so they don't take
            down the daemon thread (and, on Python 3.14, the
            process).
        """
        with state.check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didOpen check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-open").start()


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def _did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    """Implements ``textDocument/didSave``: re-index + re-check on save.

    Updates the workspace index for the saved file (so a new
    ``use`` clause becomes resolvable from siblings) and spawns a
    daemon worker that re-runs the pipeline for the saved file's
    workset.

    Args:
        ls: Active language server.
        params: LSP ``DidSaveTextDocumentParams``.

    Returns:
        None.

    Note:
        The index update reads from disk; the buffer text isn't
        consulted because save semantics imply the disk state is
        canonical.
    """
    uri = params.text_document.uri
    _remember_uri(uri)
    saved = _uri_to_path(uri)
    if saved is not None:
        _update_index_for(saved.resolve())

    def worker() -> None:
        """Run the pipeline for ``uri`` under ``state.check_lock``.

        Returns:
            None.

        Note:
            Mirrors the didOpen worker; failures are logged and
            swallowed.
        """
        with state.check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didSave check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-save").start()


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def _did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    """Implements ``textDocument/didClose``: republish cached diagnostics.

    DimFort is a workspace-wide checker, so a file's diagnostics
    remain true after the user closes it — the bugs are still
    there. Rather than clear the Problems panel (the LSP default),
    republish the cached workspace-check diagnostics so the entries
    stay visible. Clears only if we have nothing on file (e.g. a
    single-file workset whose entry no longer applies).

    Args:
        ls: Active language server.
        params: LSP ``DidCloseTextDocumentParams``.

    Returns:
        None.

    Note:
        Reads ``state.last_result`` under ``state.last_result_lock``.
    """
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
    """Implements ``textDocument/didChange``: debounced live check.

    Bumps the per-URI version counter, captures the current buffer
    text, and spawns a daemon worker that sleeps for
    ``_DEBOUNCE_SECONDS`` before re-checking under
    ``state.check_lock``. The version check inside the worker
    discards stale keystrokes so only the latest debounced edit
    fires a publish.

    Args:
        ls: Active language server.
        params: LSP ``DidChangeTextDocumentParams``.

    Returns:
        None.

    Note:
        The in-memory buffer text is also fed to
        :func:`_update_index_for` so a freshly-added ``use M`` is
        picked up before the publish runs.
    """
    uri = params.text_document.uri
    _remember_uri(uri)
    version = _bump_version(uri)

    # Pygls keeps a TextDocument with the up-to-date buffer source.
    doc = ls.workspace.get_text_document(uri)
    text = doc.source

    def delayed() -> None:
        """Sleep through the debounce window, then re-check if still current.

        Returns:
            None.

        Note:
            Version is checked twice — once after the sleep, once
            inside ``state.check_lock`` — because a later keystroke
            may arrive while the worker is waiting for the lock.
        """
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
    """Implements ``textDocument/hover``: resolved unit at the cursor.

    Returns ``None`` when hover is disabled, when the requested
    position has no unit-bearing token, or when the file isn't
    loaded yet (after a synchronous re-publish attempt via
    :func:`_ensure_uri_loaded`).

    Args:
        ls: Active language server.
        params: LSP ``HoverParams`` carrying ``textDocument.uri``
            and ``position``.

    Returns:
        An :class:`lsprotocol.types.Hover` with Markdown contents
        and a range, or ``None`` when nothing is reportable.

    Note:
        Acquires ``state.ts_handler_lock`` because the underlying
        tree-sitter traversal is not thread-safe; serialises with
        definition + inlay handlers.
    """
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
# Side-panel info — thin delegations for the dimfort/panelInfo and
# dimfort/interactions requests. The two handlers below forward to
# ``panel.resolve`` / ``interactions.resolve`` in the extracted feature
# modules; this file owns only the @server.feature registration and
# the cached-state plumbing. See docs/design/shipped/panel-info.md.
# ---------------------------------------------------------------------------




@server.feature("dimfort/panelInfo")
def _panel_info(ls: LanguageServer, params: Any) -> dict[str, Any] | None:
    """Implements ``dimfort/panelInfo``: side-panel payload at the cursor.

    Stateless from the server's perspective: reads from the last
    cached ``WorksetResult`` (``state.last_result``) and computes
    the response on the fly. See ``docs/design/shipped/panel-info.md``
    for the data model and wire spec.

    Args:
        ls: Active language server.
        params: Custom request params; an object with at least
            ``uri`` and ``position`` (forwarded to
            :func:`panel.resolve`).

    Returns:
        A JSON-serialisable dict describing the panel sections
        (scope, imports, hover, interactions), or ``None`` when no
        payload applies.

    Note:
        No tree-sitter lock needed: :mod:`panel` parses fresh trees
        rather than reading the cached one.
    """
    return panel.resolve(ls, params)


@server.feature("dimfort/interactions")
def _interactions(ls: LanguageServer, params: Any) -> dict[str, Any] | None:
    """Implements ``dimfort/interactions``: cross-site unit analysis for a symbol.

    Resolves the identifier at ``(uri, position)`` (or an explicit
    ``symbol`` param), then runs :func:`collect_interactions` over
    the cached workset and returns the report. See
    ``docs/design/interaction-points.md`` for the data model.

    Args:
        ls: Active language server.
        params: Custom request params; an object with ``uri`` plus
            either ``position`` or an explicit ``symbol``.

    Returns:
        A JSON-serialisable dict listing every read/write/contributor
        site for the resolved symbol, tagged with the unit each site
        requires or contributes, or ``None`` when no symbol could be
        identified.

    Note:
        Delegates to :func:`interactions.resolve`; no tree-sitter
        lock needed because that path parses fresh trees.
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
    """Implements ``textDocument/inlayHint``: ghost-text units in the buffer.

    Renders ``[unit]`` ghost text at variable uses, calls, and
    member accesses. Walks the visible range only — VSCode requests
    inlays in the currently-on-screen range — and pulls each
    candidate node through the ts_checker resolver so the unit text
    matches what the diagnostic pipeline computes.

    Args:
        ls: Active language server.
        params: LSP ``InlayHintParams`` carrying ``textDocument.uri``
            and the visible ``range``.

    Returns:
        A list of :class:`lsprotocol.types.InlayHint`, or ``None``
        when inlays are disabled.

    Note:
        Acquires ``state.ts_handler_lock`` because tree-sitter's C
        traversal is not thread-safe. Calls
        :func:`_ensure_uri_loaded` first to handle the tab-switch
        case where the workset hasn't been published yet.
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
    """Implements ``textDocument/completion``: unit-name completions.

    Fires inside ``@unit{...}`` (and configured equivalents) on
    bare-``!`` comments. Trigger characters cover the unit-algebra
    punctuation (``{ space / * ^``) so the list refreshes as the
    user types compound expressions like ``kg/m^3``.

    Args:
        ls: Active language server.
        params: LSP ``CompletionParams`` carrying ``textDocument.uri``
            and ``position``.

    Returns:
        A :class:`lsprotocol.types.CompletionList` of candidates, or
        ``None`` when completion is disabled or the cursor isn't in
        a unit comment.

    Note:
        Delegates entirely to :func:`completion.complete`; this
        wrapper only handles the feature-flag short-circuit.
    """
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
    """Implements ``textDocument/definition``: jump to a symbol's declaration.

    Resolves identifiers and call-callees to their declaration site,
    searching every loaded file's tree-sitter tree. Returns the
    first match — F90's case-insensitive name resolution is
    implemented by a lower-cased compare on both ends.

    Args:
        ls: Active language server.
        params: LSP ``DefinitionParams`` carrying ``textDocument.uri``
            and ``position``.

    Returns:
        A list with a single :class:`lsprotocol.types.Location` when
        the declaration is found, or ``None`` when goto-definition
        is disabled or the symbol can't be resolved.

    Note:
        Acquires ``state.ts_handler_lock`` — Cmd-hover fires hover
        and definition simultaneously, and the tree-sitter C
        library is not thread-safe for concurrent traversal
        (history: this combination was triggering native-level
        crashes before the lock was introduced).
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
    """Implements ``textDocument/codeAction``: quick-fixes for DimFort diagnostics.

    Surfaces three quick-fix families: insert a ``!< @unit{}``
    skeleton on annotation-less declarations, extract a bare
    literal to a typed PARAMETER (for H001/H002/U007 lifts), and
    apply the U002 "Replace with ..." suggestion carried via the
    diagnostic's ``data.suggested_rewrite`` payload.

    Args:
        ls: Active language server.
        params: LSP ``CodeActionParams`` carrying the editor
            selection range and the diagnostics in context.

    Returns:
        A list of :class:`lsprotocol.types.CodeAction`, or ``None``
        when code actions are disabled.

    Note:
        Code action kinds are restricted to ``QuickFix``.
        Delegates to :func:`code_action.resolve` for the actual
        quick-fix synthesis.
    """
    if not _features.code_actions:
        return None
    return code_action.resolve(ls, params)


@server.command("dimfort.checkWorkspace")
def _cmd_check_workspace(ls: LanguageServer, *_args: Any) -> None:
    """Implements ``workspace/executeCommand dimfort.checkWorkspace``.

    Runs the checker over every file in the workspace index,
    publishing diagnostics for each. Triggered from the client via
    the palette command "DimFort: Check Whole Workspace".

    The work runs on a daemon thread so the LSP stays responsive.
    The server-wide ``state.check_lock`` is held for the duration to
    avoid racing with per-file didOpen/didSave/didChange checks.

    Args:
        ls: Active language server.
        *_args: Forwarded command arguments (none expected).

    Returns:
        None. The actual work + notifications happen on the
        ``dimfort-check-workspace`` daemon thread.

    Note:
        No-ops gracefully when the index hasn't been built yet
        (see :func:`_check_whole_workspace`).
    """
    threading.Thread(
        target=_check_whole_workspace,
        args=(ls,),
        daemon=True,
        name="dimfort-check-workspace",
    ).start()


def _check_whole_workspace(ls: LanguageServer) -> None:
    """Worker for ``dimfort.checkWorkspace``: run the pipeline over every indexed file.

    Pulls the file list from ``state.workspace_index.uses_by_file``,
    fires a ``$/progress`` notification, runs
    :func:`check_files` with the full configured environment, then
    publishes per-file diagnostics. Reports a final toast summarising
    H/U counts, total time, and (when caching is on) cache hit/miss
    stats.

    Args:
        ls: Active language server.

    Returns:
        None.

    Note:
        Holds ``state.check_lock`` across the whole run so per-file
        didOpen/didSave/didChange checks can't interleave. When the
        index isn't ready, surfaces a heads-up via :func:`_notify`
        and returns without running the pipeline.
    """
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
        """Forward a per-phase pipeline tick to the LSP progress channel.

        Args:
            phase: Pipeline phase identifier (``"load"`` /
                ``"index"`` / ``"check"``); used to pick a
                human-readable label.
            scanned: Files completed for the phase so far.
            total: Total files in the phase.
            path: File currently being processed.

        Returns:
            None.

        Note:
            Throttled to ~10/sec; the final tick of each phase
            always emits so the spinner advances visibly between
            phases on a 2400-file workspace.
        """
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
                unit_patterns=compile_unit_patterns(
                    state.project_config.unit_comment_delimiters
                ),
                assume_patterns=compile_structured_patterns(
                    state.project_config.unit_assume_comment_delimiters
                ),
                affine_patterns=compile_structured_patterns(
                    state.project_config.unit_affine_comment_delimiters
                ),
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
    """Entry point: start the DimFort LSP on stdio.

    Raises DimFort's own log level to INFO so progress messages
    emitted via ``log.info`` reach handlers (pygls's root threshold
    is WARNING; without this, namespace-scoped INFO logs would be
    silently dropped before reaching the client's output channel),
    installs the crash-trace hook so silent stdio deaths leave a
    file behind for the next debugging pass, and hands control to
    pygls's ``start_io`` event loop.

    Returns:
        None. Blocks until the LSP transport closes.

    Note:
        Used by the console script declared in ``pyproject.toml``
        (``dimfort-lsp``). Should be the only public callable in
        this module.
    """
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
    by ``sys.excepthook`` + ``threading.excepthook``, the next step
    is to wrap individual feature handlers in try/except locally
    rather than instrument the loop globally.

    Also attaches an ERROR-level handler to the ``pygls`` and
    ``asyncio`` loggers so feature-handler exceptions (which pygls
    catches and converts to JSON-RPC errors) get mirrored to the
    crash file.

    Returns:
        None. Side effects only.

    Note:
        Env-var contract: unset → default path; empty string →
        feature disabled; any other value → use as the path.
    """
    import os
    import sys
    import traceback
    from types import TracebackType

    env = os.environ.get("DIMFORT_CRASH_LOG")
    if env is None:
        path = "/tmp/dimfort-lsp.crash"
    elif env == "":
        return
    else:
        path = env

    def _write(header: str, body: str) -> None:
        """Append a header + body section to the crash log file.

        Args:
            header: Short label identifying the source of the
                traceback (e.g. ``"sys.excepthook"``, a thread
                name, or a pygls logger label).
            body: Formatted traceback text.

        Returns:
            None.

        Note:
            Silently swallows IO errors: this is the diagnostic
            path of last resort, so a write failure can't be
            allowed to raise further.
        """
        try:
            with open(path, "a") as f:
                f.write(f"\n=== {header} ===\n{body}\n")
                f.flush()
        except Exception:  # noqa: BLE001 — diagnostic path; can't help if write fails
            pass

    def _hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        """Format and persist an uncaught exception from ``sys.excepthook``.

        Args:
            exc_type: Exception class.
            exc_value: Exception instance.
            exc_tb: Traceback object (may be ``None``).

        Returns:
            None.
        """
        _write(
            "sys.excepthook",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

    sys.excepthook = _hook
    # Threads (pygls runs synchronous features in a thread pool).
    # Without this, exceptions from a worker thread don't reach
    # excepthook on older Pythons.
    if hasattr(threading, "excepthook"):
        def _thread_hook(args: threading.ExceptHookArgs) -> None:
            """Format and persist an uncaught exception from a daemon thread.

            Args:
                args: The standard library's bundle of
                    ``(exc_type, exc_value, exc_traceback, thread)``.

            Returns:
                None.

            Note:
                Older Pythons don't route worker-thread exceptions
                to ``sys.excepthook``; this closes that gap.
            """
            _write(
                f"thread {args.thread.name if args.thread else '?'}",
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
        """Logging handler that mirrors records into the crash log file.

        Attached to the ``pygls`` and ``asyncio`` loggers so feature
        handler exceptions — which pygls intercepts and converts to
        JSON-RPC error responses, bypassing ``sys.excepthook`` —
        still leave a trace on disk.

        Note:
            Inherits :class:`logging.Handler`; only :meth:`emit` is
            overridden.
        """

        def emit(self, record: logging.LogRecord) -> None:
            """Format and append one log record to the crash file.

            Args:
                record: Standard ``LogRecord`` instance.

            Returns:
                None.

            Note:
                Falls back to ``record.getMessage()`` if formatting
                raises (e.g. a misformed extra-args payload).
            """
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
