"""Inlay-hint resolution for the LSP server.

Emits ``[unit]`` ghost text at variable uses, calls, and derived-type member
accesses across the editor's visible range. Each candidate node is pushed
through the ts_checker resolver so the rendered unit matches what the
diagnostic pipeline computes. ``server.py`` registers the LSP feature and —
holding ``state.ts_handler_lock`` — delegates the traversal here.

Per-URI table cache
-------------------
``_tables_cache``: ``uri → (version, var_types, parameter_values,
type_field_types)``. Replaces the three full tree walks
(``collect_var_types``, ``collect_parameter_values``,
``collect_type_field_types``) on every cursor-scroll
``textDocument/inlayHint`` request — ~30-150 ms saved per scroll on a
5000-line file under ``state.ts_handler_lock``.

Invalidation
~~~~~~~~~~~~
Entry replaced in place when the cached ``version`` differs from the
URI's current ``state.doc_versions`` entry. Every ``didChange``
notification bumps that counter, so a stale entry never survives an
edit — the next ``inlayHint`` after an edit rebuilds.

Bound
~~~~~
One entry per open buffer (``O(open buffers)``). Eviction on
``didClose`` via :func:`forget_uri`, called from
``server._forget_uri`` — without that hook closed buffers would
persist for the LSP session. No explicit numerical cap; the bound is
the editor's open-file count, which is the natural ceiling.

Thread safety
~~~~~~~~~~~~~
``_tables_cache_lock`` guards every read and write — ``inlayHint``
fires concurrently with other handlers that don't hold
``state.ts_handler_lock``, and ``forget_uri`` runs on the
``didClose`` path. Lock is held only for the dict op, not the
underlying tree walk.
"""
from __future__ import annotations

import threading
from fractions import Fraction

from lsprotocol import types as lsp
from tree_sitter import Node

from dimfort.core import ts_checker
from dimfort.core.units import UnitExpr
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.hover_render import _unit_pretty
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _build_ts_ctx, _trees_for

# uri → (version, var_types, parameter_values, type_field_types)
_tables_cache: dict[
    str,
    tuple[
        int,
        dict[str, str],
        dict[str, Fraction | int],
        dict[tuple[str, str], str],
    ],
] = {}
_tables_cache_lock = threading.Lock()


def forget_uri(uri: str) -> None:
    """Evict the cached inlay tables for ``uri``.

    Called from the LSP ``textDocument/didClose`` handler so closed
    buffers don't accumulate in the per-URI cache.
    """
    with _tables_cache_lock:
        _tables_cache.pop(uri, None)


def resolve(params: lsp.InlayHintParams) -> list[lsp.InlayHint] | None:
    """Compute inlay hints for the editor's visible range.

    Walks the cached tree-sitter tree for the URI in ``params``,
    resolving each member access, call, and identifier use through
    ``ts_checker`` and emitting a ``[unit]`` hint anchored at the
    node's end column whenever a unit is known. The same resolver
    powers diagnostics, so the rendered unit matches what ``check``
    would report at that site.

    Args:
        params: LSP ``InlayHintParams`` carrying the document URI and
            the zero-based visible-line range.

    Returns:
        List of :class:`lsp.InlayHint` records for sites that fall
        inside the visible range; empty list when no workset result
        or no tree is loaded for the URI.

    Note:
        The caller in ``server.py`` holds ``state.ts_handler_lock``
        around this call — the tree-sitter traversal is not safe
        across concurrent readers (the documented concurrency gotcha).
    """
    uri = params.text_document.uri
    found = _trees_for(uri)
    if found is None:
        return []
    resolved_path, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return []

    visible_start_line = params.range.start.line + 1   # 1-based
    visible_end_line = params.range.end.line + 1

    # Audit #4: cache the three table walks by (uri, doc_version).
    # The collectors return fresh dicts; ``ctx.var_types.update(...)``
    # reads from the cached dict without mutating it, so handing the
    # same reference back on a hit is safe even though the cache
    # outlives one request.
    with state.doc_versions_lock:
        version = state.doc_versions.get(uri, 0)
    with _tables_cache_lock:
        cached = _tables_cache.get(uri)
    if cached is not None and cached[0] == version:
        _, var_types, parameter_values, type_field_types = cached
    else:
        var_types = ts_checker.collect_var_types(tree, source)
        parameter_values = ts_checker.collect_parameter_values(tree, source)
        type_field_types = ts_checker.collect_type_field_types(tree, source)
        with _tables_cache_lock:
            _tables_cache[uri] = (
                version, var_types, parameter_values, type_field_types,
            )

    ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
    ctx.var_types.update(var_types)
    ctx.parameter_values.update(parameter_values)
    ctx.type_field_types.update(type_field_types)

    seen: set[tuple[int, int]] = set()
    hints: list[lsp.InlayHint] = []

    def _emit(node: Node, unit: UnitExpr | None) -> None:
        """Append one ``[unit]`` hint anchored at the node's end column.

        Args:
            node: Tree-sitter node whose end position anchors the hint.
                Typically an ``identifier``, ``call_expression``, or
                ``derived_type_member_expression``.
            unit: Resolved unit for the node, or ``None`` when the
                resolver could not assign one. ``None`` is a silent
                no-op — no hint is emitted.

        Note:
            Deduplicates against ``seen`` so the same ``(line, end_col)``
            anchor never receives two hints (matters when identifier
            walks overlap with call walks). Skips nodes whose end line
            falls outside ``visible_start_line..visible_end_line``.
        """
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
        _emit(member, ts_checker.resolve_member_chain(member, ctx, source))

    # Calls — emit on the full call expression so the [unit] sits past
    # the closing paren.
    for call in _ts_h.walk_calls(tree):
        if call.type == "subroutine_call":
            continue  # subroutines have no return unit
        _emit(call, ts_checker.resolve_unit(call, ctx, source))

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
        _emit(ident, ts_checker.resolve_unit(ident, ctx, source))
    return hints
