"""Go-to-definition resolution for the LSP server.

Resolves the identifier, call-callee, or ``use`` module under the cursor to
its declaration site, searching every loaded tree-sitter tree in the cached
workset. F90's case-insensitive name resolution is a lower-cased compare on
both ends. ``server.py`` registers the LSP feature and — holding
``state.ts_handler_lock`` — delegates the traversal here.
"""
from __future__ import annotations

from pathlib import Path

from lsprotocol import types as lsp

from dimfort.core import ts_parser as _ts
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _trees_for, _uri_for_path


def resolve(params: lsp.DefinitionParams) -> list[lsp.Location] | None:
    found = _trees_for(params.text_document.uri)
    if found is None:
        return None
    _, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None

    line = params.position.line + 1
    col = params.position.character + 1

    # Identify the target. Order matters: the most specific node
    # type wins. ``use foo`` first (its module_name token isn't an
    # identifier or call-callee), then call-callees, then plain
    # identifiers.
    target_name: str | None = None
    target_kind: str | None = None  # "module", "var", or "callable"
    for use_node in _ts_h.walk_use_statements(tree):
        nm = _ts_h.use_statement_module_name(use_node, source)
        if nm is None:
            continue
        mod_name, mod_name_node = nm
        if _ts_h.node_contains(mod_name_node, line, col):
            target_name = mod_name
            target_kind = "module"
            break
    if target_name is None:
        for call in _ts_h.walk_calls(tree):
            name = _ts_h.call_name(call, source)
            if name is None:
                continue
            # Match only if the cursor is on the callee identifier
            # (not on an argument inside the call).
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

    def _name_node_location(tree_path: Path, name_node) -> lsp.Location:
        sr, sc = name_node.start_point
        er, ec = name_node.end_point
        return lsp.Location(
            uri=_uri_for_path(tree_path),
            range=lsp.Range(
                start=lsp.Position(line=sr, character=sc),
                end=lsp.Position(line=er, character=ec),
            ),
        )

    # Walk every loaded tree for the matching declaration / function.
    # When the cursor was on a "callable", we try function/subroutine
    # definitions first but fall through to variable declarations —
    # ``a(1)`` in Fortran could be either an array index or a function
    # call, and tree-sitter can't distinguish them syntactically.
    for tree_path, (other_tree, other_source) in result.trees.items():
        if target_kind == "module":
            for mod in _ts_h.walk_module_definitions(other_tree):
                nm = _ts_h.module_definition_name(mod, other_source)
                if nm is None:
                    continue
                name, name_node = nm
                if name.lower() == target_lc:
                    return [_name_node_location(tree_path, name_node)]
            continue
        if target_kind == "callable":
            for func in _ts_h.walk_function_definitions(other_tree):
                nm = _ts_h.function_definition_name(func, other_source)
                if nm is None:
                    continue
                name, name_node = nm
                if name.lower() == target_lc:
                    return [_name_node_location(tree_path, name_node)]
        for _decl, name_node in _ts_h.walk_decl_identifiers(other_tree):
            if _ts.node_text(name_node, other_source).lower() != target_lc:
                continue
            return [_name_node_location(tree_path, name_node)]
    return None
