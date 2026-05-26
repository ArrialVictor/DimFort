"""Inlay-hint resolution for the LSP server.

Emits ``[unit]`` ghost text at variable uses, calls, and derived-type member
accesses across the editor's visible range. Each candidate node is pushed
through the ts_checker resolver so the rendered unit matches what the
diagnostic pipeline computes. ``server.py`` registers the LSP feature and —
holding ``state.ts_handler_lock`` — delegates the traversal here.
"""
from __future__ import annotations

from lsprotocol import types as lsp

from dimfort.core import ts_checker
from dimfort.core.units import Unit
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.hover_render import _unit_pretty
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _build_ts_ctx, _trees_for


def resolve(params: lsp.InlayHintParams) -> list[lsp.InlayHint] | None:
    found = _trees_for(params.text_document.uri)
    if found is None:
        return []
    resolved_path, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return []

    visible_start_line = params.range.start.line + 1   # 1-based
    visible_end_line = params.range.end.line + 1

    ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
    ctx.var_types.update(ts_checker.collect_var_types(tree, source))
    ctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
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
