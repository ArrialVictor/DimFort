"""Coverage visualisation payloads (``dimfort/lineStatus`` and ``dimfort/coverageStats``).

Thin LSP wrappers over :mod:`dimfort.core.coverage`. The core module
holds the per-line projection logic; this module only translates
between the LSP wire format and the core dataclasses, and serialises
tree-sitter traversal under ``state.ts_handler_lock``.

See ``docs/design/future/coverage-visualization.md`` for the design
spec.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from pygls.lsp.server import LanguageServer

from dimfort.core.coverage import (
    FileCoverage,
    aggregate_file,
    aggregate_workset,
    project_file,
)
from dimfort.core.multifile import WorksetResult
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _uri_to_path

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
    del ls

    uri = _get(params, "uri")
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return {
            "scope": "workspace" if uri is None else "file",
            "files": [],
            "total": {"ok": 0, "warn": 0, "fire": 0, "unparsed": 0, "out": 0, "coverage_pct": 0.0},
        }

    if uri is not None:
        path = _uri_to_path(uri)
        if path is None:
            return None
        paths = [path.resolve()]
        scope = "file"
    else:
        paths = sorted(result.diagnostics.keys() | result.attachments.keys())
        scope = "workspace"

    per_file = [_get_file_coverage(p, result) for p in paths]

    workset = aggregate_workset(per_file)
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
