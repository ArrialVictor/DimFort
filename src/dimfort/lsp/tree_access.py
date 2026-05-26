"""Locating a parsed tree and checker context for a URI.

The bridge between an editor URI and the cached workset result: convert the
URI to a filesystem path, look up the parsed tree-sitter tree, and spin up a
``ts_checker.Ctx`` pre-loaded with the workset's unit tables. Every
tree-walking feature handler (hover, definition, inlay, panel, interactions)
starts here. Reads the shared ``state`` singleton but acquires no
``ts_handler_lock`` of its own — callers hold that around their traversal.
Extracted from ``server.py`` (the LSP-split refactor).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlparse

from tree_sitter import Tree

from dimfort.core import ts_checker
from dimfort.core import units as _units_mod
from dimfort.core.multifile import WorksetResult
from dimfort.core.units import UnitExpr
from dimfort.lsp.state import state


def _uri_for_path(path: Path) -> str:
    """Prefer the editor's original URI for a known-open file.

    Falls back to ``Path.as_uri()`` for files the editor hasn't opened
    yet (cross-file diagnostics on closed files).
    """
    with state.opened_uris_lock:
        known = state.opened_uris.get(path)
    if known is not None:
        return known
    return path.as_uri()


def _uri_to_path(uri: str) -> Path | None:
    if not uri.startswith("file:"):
        return None
    path = unquote(urlparse(uri).path)
    # On Windows, a URI like ``file:///C:/Users/...`` decodes to
    # ``/C:/Users/...`` — the leading slash is a URL-path artefact,
    # not part of the filesystem path. ``Path("/C:/Users/...")`` on
    # Windows doesn't equal ``Path("C:/Users/...")``, so a workset
    # keyed by the latter misses a lookup keyed by the former. Detect
    # the leading-slash-before-drive-letter pattern and strip it.
    # POSIX paths (no drive letter) are untouched.
    if len(path) >= 3 and path[0] == "/" and path[2] == ":" and path[1].isalpha():
        path = path[1:]
    return Path(path)


def _trees_for(uri: str) -> tuple[Path, Tree, bytes] | None:
    """Return ``(resolved_path, tree, source_bytes)`` for ``uri`` if loaded."""
    with state.last_result_lock:
        result = state.last_result
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


def _build_ts_ctx(
    result: WorksetResult, source: bytes, file: str,
    *, path: Path | None = None,
) -> ts_checker.Ctx:
    """Spin up a ts_checker ``Ctx`` pre-loaded with the workset's tables.

    Reused by hover / inlay so identifier-to-unit lookup goes through
    the same logic as the diagnostic pipeline — no second source of
    truth for derived-type / use-chain resolution.

    When ``path`` is provided we also splice in the per-file scoped
    annotation table and routine byte-ranges, so ``ctx.unit_for(name,
    byte_offset)`` honours the cursor's enclosing subroutine. Without
    ``path`` we degrade to flat ``merged_var_units`` (same behaviour
    as before scope-aware lookups existed).
    """
    table = _units_mod.DEFAULT_TABLE
    assert table is not None, (
        "DEFAULT_TABLE not initialised — import dimfort.core.unit_config"
    )
    var_units_by_scope: dict[tuple[str | None, str], UnitExpr] = {}
    routine_scopes: tuple[tuple[int, int, str], ...] = ()
    if path is not None:
        var_units_by_scope = result.var_units_by_scope.get(path, {})
        att = result.attachments.get(path)
        if att is not None:
            routine_scopes = att.routine_scopes
    return ts_checker.Ctx(
        file=file,
        var_units=result.merged_var_units,
        table=table,
        signatures=result.signatures,
        # var_types / type_field_types are collected per-tree on demand
        # by callers that need member-access resolution.
        var_types={},
        type_field_types={},
        field_units=result.merged_field_units,
        var_units_by_scope=var_units_by_scope,
        routine_scopes=routine_scopes,
        _scope_starts=tuple(r[0] for r in routine_scopes),
        # With a path we have the per-file scoped table (incl. use-imports
        # under the (None, name) layer): resolve scope-aware so a name
        # resolves to its OWN routine's unit, never a same-named symbol
        # from elsewhere (finding #018). Without a path, degrade to flat.
        scope_aware=path is not None,
        # Honour the project's opt-in scale mode so on-demand features
        # (hover / panel / re-check) reason consistently with the
        # diagnostic pipeline. Default off ⇒ dimension-only.
        scale_mode=state.scale_mode,
    )
