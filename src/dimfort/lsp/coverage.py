"""Coverage visualisation payloads (``dimfort/lineStatus`` and ``dimfort/coverageStats``).

Thin LSP wrappers over :mod:`dimfort.core.coverage`. The core module
holds the per-line projection logic; this module only translates
between the LSP wire format and the core dataclasses, and serialises
tree-sitter traversal under ``state.ts_handler_lock``.

See ``docs/design/future/coverage-visualization.md`` for the design
spec.
"""
from __future__ import annotations

from typing import Any

from pygls.lsp.server import LanguageServer

from dimfort.core.coverage import (
    aggregate_file,
    aggregate_workset,
    project_file,
)
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _uri_to_path


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

    per_file = []
    for p in paths:
        with state.ts_handler_lock:
            statuses = project_file(p, result)
        # Total source-file line count: pull from the cached source bytes
        # when available; fall back to the maximum status-key line + 1.
        tree_entry = result.trees.get(p)
        if tree_entry is not None:
            total_lines = tree_entry[1].count(b"\n") + 1
        elif statuses:
            total_lines = max(statuses)
        else:
            total_lines = 0
        per_file.append(aggregate_file(p, statuses, total_lines=total_lines))

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
