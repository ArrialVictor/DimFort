"""Coverage visualisation payloads (``dimfort/lineStatus`` and ``dimfort/coverageStats``).

Thin LSP wrappers over :mod:`dimfort.core.coverage`. The core module
holds the per-line projection logic; this module only translates
between the LSP wire format and the core dataclasses, and serialises
tree-sitter traversal under ``state.ts_handler_lock``.

See ``docs/design/future/coverage-visualization.md`` for the design
spec.
"""
from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any

from pygls.lsp.server import LanguageServer

from dimfort.core.cache_store import CacheStore
from dimfort.core.coverage import (
    FileCoverage,
    aggregate_file,
    aggregate_workset,
    project_file,
)
from dimfort.core.multifile import WorksetResult, check_files
from dimfort.core.unit_patterns import (
    compile_structured_patterns,
    compile_unit_patterns,
)
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _uri_to_path

log = logging.getLogger(__name__)

# Dedicated content-hash cache for the workspace coverage check.
# Independent of ``state.cache`` (which mirrors the user's
# ``cache_mode`` preference for their explicit CLI / LSP work):
# we always want write access here so subsequent workspace-scope
# requests benefit from content-hash hits — without it, every edit
# triggers a full re-check of every workspace file (tens of
# seconds on larger real-world Fortran codebases). Lifetime is the
# LSP session; lazily created on first request, lives in a tempdir
# that the OS reaps at boot.
_ws_cache: CacheStore | None = None
_ws_cache_lock = threading.Lock()


def _get_or_create_ws_cache() -> CacheStore:
    """Lazy-construct the session-scoped workspace coverage cache."""
    global _ws_cache
    with _ws_cache_lock:
        if _ws_cache is None:
            root = Path(tempfile.mkdtemp(prefix="dimfort-ws-coverage-"))
            _ws_cache = CacheStore(root=root)
        return _ws_cache

# Per-file coverage cache. Populated by ``_get_file_coverage`` and
# invalidated whenever ``state.last_result`` is replaced (identity
# comparison). The aggregation in ``stats()`` walks every workset
# file under ``state.ts_handler_lock``; on larger real-world
# Fortran codebases the un-cached cost is in the tens-to-hundreds
# of milliseconds. Caching the per-file ``FileCoverage`` records
# keyed by the current ``WorksetResult`` makes repeat hits from
# the same result O(1) — relevant when bar + report buffer both
# query, or when multiple companions are connected.
#
# Identity (``is``) rather than ``id()`` because Python may reuse a
# freed object's id; holding a strong ref to the cached result
# avoids that footgun. Memory cost: one extra WorksetResult ref.
_cache_lock = threading.Lock()
_cache_result: WorksetResult | None = None
_cache_files: dict[Path, FileCoverage] = {}


def _get_file_coverage(p: Path, result: WorksetResult) -> FileCoverage:
    """Return cached :class:`FileCoverage` for ``p``, computing on miss.

    Cache is keyed by the identity of ``result``; replacement of
    ``state.last_result`` invalidates the cache on the next call.
    The tree-sitter walk in :func:`project_file` runs under
    ``state.ts_handler_lock``; the cache lock is released for the
    walk so concurrent callers don't serialise on the cache.
    """
    global _cache_result, _cache_files
    with _cache_lock:
        if _cache_result is not result:
            _cache_result = result
            _cache_files = {}
        cached = _cache_files.get(p)
        if cached is not None:
            return cached

    with state.ts_handler_lock:
        statuses = project_file(p, result)
    tree_entry = result.trees.get(p)
    if tree_entry is not None:
        total_lines = tree_entry[1].count(b"\n") + 1
    elif statuses:
        total_lines = max(statuses)
    else:
        total_lines = 0
    fc = aggregate_file(p, statuses, total_lines=total_lines)

    with _cache_lock:
        # Only store if no concurrent caller swapped the result key.
        if _cache_result is result:
            _cache_files[p] = fc
    return fc


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` off ``obj`` as either an attribute or dict entry.

    Args:
        obj: The wrapped LSP params object (TypedDict-style with
            attribute access, plain ``dict``, or ``None``).
        key: Field name to look up.

    Returns:
        The field value when present, or ``None`` when ``obj`` is
        ``None``, the attribute is missing, or the dict has no entry
        for ``key``.
    """
    if hasattr(obj, key):
        return getattr(obj, key)
    if isinstance(obj, dict):
        return obj.get(key)
    return None


def resolve(ls: LanguageServer, params: Any) -> dict[str, Any] | None:
    """Resolve the ``dimfort/lineStatus`` payload for one file.

    Reads the per-line status projection from the cached
    :class:`~dimfort.core.multifile.WorksetResult`. Lines not present
    in the response are out-of-scope (no decoration); the companion
    paints nothing for them.

    Args:
        ls: pygls :class:`LanguageServer` instance (unused for the
            lookup, but kept for signature parity with the other
            ``dimfort/*`` handlers).
        params: Raw LSP custom-method params object. Expected to
            carry ``uri``.

    Returns:
        A dict matching the wire format documented in §7 of the
        coverage spec, or ``None`` when ``uri`` is missing / doesn't
        map to a known path / the workset cache is empty.

    Note:
        The core projection walks the cached tree-sitter tree to
        identify green-eligible lines; the walk is serialised under
        :attr:`state.ts_handler_lock` to match the documented
        concurrency contract.
    """
    del ls  # unused; LanguageServer not needed for cache-only handler

    uri = _get(params, "uri")
    if uri is None:
        return None
    path = _uri_to_path(uri)
    if path is None:
        return None
    resolved = path.resolve()

    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return {"uri": uri, "lines": []}

    with state.ts_handler_lock:
        statuses = project_file(resolved, result)

    lines = [
        {"line": line, "status": status}
        for line, status in sorted(statuses.items())
    ]
    return {"uri": uri, "lines": lines}


def _collect_open_overrides(ls: LanguageServer) -> dict[Path, str]:
    """Snapshot the current in-memory text of every open document.

    Used so the workspace check sees unsaved buffer edits rather than
    only what's on disk — matching the per-active-file path, which
    passes the active file's buffer text as an override. Without
    this, the workspace coverage aggregate would lag every keystroke
    until the user pressed Save.

    Args:
        ls: Active language server. Reads from
            ``ls.workspace.text_documents``.

    Returns:
        Mapping from resolved absolute path to the document's
        current source text. Paths whose URI can't be resolved are
        silently skipped. Returns an empty dict on any pygls API
        change that breaks the lookup (defensive — the workspace
        check still runs, just against on-disk state).
    """
    overrides: dict[Path, str] = {}
    try:
        documents = ls.workspace.text_documents
    except Exception:
        return overrides
    for uri, doc in documents.items():
        path = _uri_to_path(uri)
        if path is None:
            continue
        try:
            overrides[path.resolve()] = doc.source
        except Exception:
            continue
    return overrides


def _run_workspace_check(ls: LanguageServer) -> WorksetResult | None:
    """Run ``check_files`` over every file in the workspace index.

    Used by the workspace-scope branch of :func:`stats` to compute a
    project-level coverage aggregate that doesn't depend on which
    file the user happens to be editing. Distinct from the
    per-active-file workset path that backs ``state.last_result``
    — calling this does not replace ``state.last_result``, so the
    per-file diagnostic flow continues to use whichever workset was
    last published.

    Args:
        ls: Active language server. Used to snapshot open-document
            text via :func:`_collect_open_overrides` so unsaved
            buffer edits are reflected in the WS aggregate.

    Returns:
        A :class:`WorksetResult` covering every indexed Fortran file
        in the workspace. ``None`` when the workspace index isn't
        built yet, the index has no files, or the check raised.

    Note:
        Synchronous. Holds ``state.check_lock`` for the duration to
        avoid racing with per-file ``didChange`` / ``didSave`` checks
        on the same shared cache + config. On larger real-world
        Fortran codebases this can take seconds; the companion is
        expected to debounce its workspace-scope requests, and the
        per-file ``lineStatus`` handler remains cheap because it
        reads from the pre-existing ``state.last_result``.
    """
    with state.workspace_index_lock:
        idx = state.workspace_index
    if idx is None:
        return None
    files = sorted(idx.uses_by_file.keys())
    if not files:
        return None

    overrides = _collect_open_overrides(ls)

    ws_cache = _get_or_create_ws_cache()

    with state.check_lock:
        try:
            return check_files(
                files,
                overrides=overrides,
                external_modules=state.external_modules,
                cpp_defines=state.project_config.cpp_defines,
                include_paths=state.project_config.include_paths,
                cache=ws_cache,
                cache_mode="read-write",
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
            log.exception("workspace coverage stats check failed")
            return None


def _project_and_aggregate(
    paths: list[Path], result: WorksetResult,
) -> Any:
    """Project per-file coverage over ``paths`` and aggregate into a workset.

    Used by both stats branches. Walks each path's tree under
    ``state.ts_handler_lock`` (matching the documented concurrency
    contract), computes the per-line status projection, and tallies
    into per-file + workset totals.

    Args:
        paths: File paths to project. Must be present in ``result``
            (either with diagnostics, attachments, or a cached tree
            entry); paths absent from ``result`` produce empty
            projections.
        result: The :class:`WorksetResult` to read from. May be
            ``state.last_result`` (per-active-file scope) or a fresh
            whole-workspace result from :func:`_run_workspace_check`.

    Returns:
        A :class:`WorksetCoverage` with per-file rows and the
        aggregated totals.
    """
    per_file = []
    for p in paths:
        with state.ts_handler_lock:
            statuses = project_file(p, result)
        tree_entry = result.trees.get(p)
        if tree_entry is not None:
            total_lines = tree_entry[1].count(b"\n") + 1
        elif statuses:
            total_lines = max(statuses)
        else:
            total_lines = 0
        per_file.append(aggregate_file(p, statuses, total_lines=total_lines))
    return aggregate_workset(per_file)


def stats(ls: LanguageServer, params: Any) -> dict[str, Any] | None:
    """Resolve the ``dimfort/coverageStats`` payload.

    Aggregates per-line statuses into per-file tier counts and a
    workset total. With ``uri`` in ``params``, returns the breakdown
    for that file only; with ``uri`` omitted, returns the workspace
    breakdown.

    Args:
        ls: pygls :class:`LanguageServer` instance (unused).
        params: Raw LSP custom-method params object. May carry an
            optional ``uri`` to scope the response to a single file.

    Returns:
        A dict with keys ``scope`` (``"file"`` or ``"workspace"``),
        optionally ``uri`` (when scoped to a single file), ``files``
        (per-file rows), and ``total`` (sum across the rows in
        ``files``). ``None`` only when ``uri`` was supplied but
        didn't map to a known path.
    """
    uri = _get(params, "uri")
    zero_total = {"ok": 0, "warn": 0, "fire": 0, "unparsed": 0, "out": 0, "coverage_pct": 0.0}

    if uri is None:
        # Workspace-scope: run check_files over EVERY file in the
        # workspace index, independent of which file the user is
        # editing. The returned aggregate is therefore a stable
        # project-level number that doesn't shift when the active
        # editor changes. Distinct from state.last_result, which is
        # always scoped to the active-file workset. ``ls`` is used
        # to snapshot open buffer text so unsaved edits flow into
        # the WS aggregate.
        ws_result = _run_workspace_check(ls)
        if ws_result is None:
            return {"scope": "workspace", "files": [], "total": zero_total}
        paths = sorted(ws_result.diagnostics.keys() | ws_result.attachments.keys())
        workset = _project_and_aggregate(paths, ws_result)
        scope = "workspace"
    else:
        # File-scope: serve from the per-active-file ``last_result``
        # via the per-file cache. Cheap (one file's projection); the
        # cache makes repeated requests from the same result O(1).
        with state.last_result_lock:
            result = state.last_result
        if result is None:
            return {"scope": "file", "files": [], "total": zero_total}
        path = _uri_to_path(uri)
        if path is None:
            return None
        fc = _get_file_coverage(path.resolve(), result)
        workset = aggregate_workset([fc])
        scope = "file"

    payload: dict[str, Any] = {
        "scope": scope,
        "files": [
            {
                "uri": f"file://{f.path}",
                "ok": f.ok,
                "warn": f.warn,
                "fire": f.fire,
                "unparsed": f.unparsed,
                "out": f.out,
                "coverage_pct": f.coverage_pct,
            }
            for f in workset.files
        ],
        "total": {
            "ok": workset.ok,
            "warn": workset.warn,
            "fire": workset.fire,
            "unparsed": workset.unparsed,
            "out": workset.out,
            "coverage_pct": workset.coverage_pct,
        },
    }
    if uri is not None:
        payload["uri"] = uri
    return payload
