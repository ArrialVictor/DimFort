"""Unit tests for the LSP tree-sitter helpers.

These pin position-containment behaviour and the targeted walks that
the hover / inlay / definition handlers depend on.
"""
from __future__ import annotations

from dimfort.core import ts_parser as ts
from dimfort.lsp import ts_helpers as ts_h


def _tree(src: str):
    return ts.parse_text(src.encode()), src.encode()


def test_node_contains_inclusive_at_start():
    """The first character of a node is inside its containment range."""
    tree, src = _tree("module foo\nend module\n")
    module_node = next(n for n in ts.walk(tree.root_node) if n.type == "module")
    # 'module foo' begins at line 1, col 1 (1-based).
    assert ts_h.node_contains(module_node, 1, 1)


def test_node_contains_outside():
    """A point past the node's last column is not contained."""
    tree, src = _tree("real :: x\n")
    decl = next(n for n in ts.walk(tree.root_node) if n.type == "variable_declaration")
    # `real :: x` ends at col 9 (1-based). Col 99 must be outside.
    assert not ts_h.node_contains(decl, 1, 99)


def test_smallest_enclosing_picks_inner_member():
    """In ``o%inner%x`` the innermost expression wins on hover."""
    tree, src = _tree("a = o%inner%x\n")
    members = list(ts_h.walk_member_exprs(tree))
    # Cursor on the 'inner' token — should hit the inner ``o%inner``
    # expression, not the outer.
    # 'inner' lives roughly at columns 7-12 in the assignment.
    hit = ts_h.smallest_enclosing(members, 1, 8)
    assert hit is not None
    base, path = ts_h.member_expr_chain(hit, src)
    assert base == "o"
    assert path == ["inner"]


def test_walk_decl_identifiers_handles_init_declarator():
    """A decl like ``real :: g = 9.81`` yields ``g`` even with the initializer."""
    tree, src = _tree("real :: g = 9.81\n")
    names = [
        ts.node_text(name_node, src)
        for _, name_node in ts_h.walk_decl_identifiers(tree)
    ]
    assert names == ["g"]


def test_walk_decl_identifiers_handles_sized_declarator():
    """A decl like ``real :: v(3)`` yields ``v`` despite the array spec."""
    tree, src = _tree("real :: v(3)\n")
    names = [
        ts.node_text(name_node, src)
        for _, name_node in ts_h.walk_decl_identifiers(tree)
    ]
    assert names == ["v"]


def test_is_call_callee_true_on_function_name():
    """The identifier ``foo`` in ``foo(x)`` is recognised as the callee position."""
    tree, src = _tree("a = foo(x)\n")
    foo_ident = next(
        n for n in ts.walk(tree.root_node)
        if n.type == "identifier" and ts.node_text(n, src) == "foo"
    )
    assert ts_h.is_call_callee(foo_ident)


def test_is_call_callee_false_on_argument():
    """The argument identifier in ``foo(x)`` is NOT the callee."""
    tree, src = _tree("a = foo(x)\n")
    x_ident = next(
        n for n in ts.walk(tree.root_node)
        if n.type == "identifier" and ts.node_text(n, src) == "x"
    )
    assert not ts_h.is_call_callee(x_ident)


def test_is_inside_type_qualifier_filters_attribute_identifiers():
    """The ``n`` in ``real, dimension(n) :: arr`` is inside a type_qualifier."""
    tree, src = _tree("real, dimension(n) :: arr\n")
    n_ident = next(
        i for i in ts.walk(tree.root_node)
        if i.type == "identifier" and ts.node_text(i, src) == "n"
    )
    arr_ident = next(
        i for i in ts.walk(tree.root_node)
        if i.type == "identifier" and ts.node_text(i, src) == "arr"
    )
    assert ts_h.is_inside_type_qualifier(n_ident)
    assert not ts_h.is_inside_type_qualifier(arr_ident)


def test_function_definition_name_extracts_name_and_header_line():
    """The function/subroutine name lives in ``function_statement > name``."""
    src = (
        "real function f(x)\n"
        "  real :: x\n"
        "  f = x\n"
        "end function\n"
    )
    tree, src_b = _tree(src)
    func = next(n for n in ts.walk(tree.root_node) if n.type == "function")
    nm = ts_h.function_definition_name(func, src_b)
    assert nm is not None
    name, name_node = nm
    assert name == "f"
    assert ts_h.function_definition_header_line(func) == 1


def test_member_expr_chain_unrolls_left_leaning_nest():
    """``o%inner%x`` unrolls to ``("o", ["inner", "x"])``."""
    tree, src = _tree("a = o%inner%x\n")
    outer = max(
        ts_h.walk_member_exprs(tree),
        key=lambda n: n.end_byte - n.start_byte,
    )
    base, path = ts_h.member_expr_chain(outer, src)
    assert base == "o"
    assert path == ["inner", "x"]
