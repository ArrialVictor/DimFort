"""LSP helpers built on tree-sitter trees.

Companion to :mod:`dimfort.lsp.server`. Pulls the parser-shape-specific
plumbing out of the handlers so the handler code stays readable.

Conventions (mirroring :mod:`dimfort.core.ts_parser`):
- Positions are 1-based ``(line, column)`` to match LSP / diagnostic
  reporting elsewhere in DimFort. Tree-sitter exposes 0-based; the
  conversion lives here.
- "Containment" is inclusive on both ends.
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from tree_sitter import Node, Tree

from dimfort.core import ts_parser as _ts

# ---------------------------------------------------------------------------
# Position containment


def node_contains(node: Node, line_1based: int, col_1based: int) -> bool:
    """``True`` if ``(line, col)`` (1-based, inclusive) sits inside ``node``."""
    sr, sc = node.start_point
    er, ec = node.end_point
    # Convert the click to 0-based for comparison with tree-sitter points.
    row = line_1based - 1
    col = col_1based - 1
    if row < sr or row > er:
        return False
    if row == sr and col < sc:
        return False
    # End-point is exclusive in tree-sitter, but we treat the last byte
    # inclusively so a click on the final char of a node still hovers.
    return not (row == er and col > ec)


def node_size(node: Node) -> int:
    """Cheap "size" used to compare overlapping matches; smaller = tighter fit."""
    sr, sc = node.start_point
    er, ec = node.end_point
    return (er - sr) * 1_000 + (ec - sc)


# ---------------------------------------------------------------------------
# Targeted walks
#
# Each function yields the named-node kinds the hover/inlay/goto handlers
# care about. Implementing them as small generators keeps the dispatch
# code declarative.


def walk_identifiers(tree: Tree) -> Iterator[Node]:
    """Yield every ``identifier`` node.

    Notably includes occurrences *inside* declarations, ``type_qualifier``
    expressions, and call argument lists. Callers that want only
    variable *uses* (excluding declaration sites) should filter on the
    parent type.
    """
    for n in _ts.walk(tree.root_node):
        if n.type == "identifier":
            yield n


def walk_decl_identifiers(tree: Tree) -> Iterator[tuple[Node, Node]]:
    """Yield ``(decl, name_node)`` for each declared variable.

    ``decl`` is the enclosing ``variable_declaration`` node (so callers
    can locate the whole statement). ``name_node`` is the identifier
    that gives the variable its name — accounting for the
    ``sized_declarator`` / ``init_declarator`` wrappers tree-sitter
    introduces when the decl carries an array dim or initializer.
    """
    for n in _ts.walk(tree.root_node):
        if n.type != "variable_declaration":
            continue
        for c in n.children:
            if c.type == "identifier":
                yield n, c
            elif c.type in ("sized_declarator", "init_declarator"):
                inner = _declarator_leading_identifier(c)
                if inner is not None:
                    yield n, inner


def _declarator_leading_identifier(node: Node) -> Node | None:
    for c in node.children:
        if c.type == "identifier":
            return c
        if c.type in ("sized_declarator", "init_declarator"):
            inner = _declarator_leading_identifier(c)
            if inner is not None:
                return inner
    return None


def walk_calls(tree: Tree) -> Iterator[Node]:
    """Yield ``call_expression`` and ``subroutine_call`` nodes."""
    for n in _ts.walk(tree.root_node):
        if n.type in ("call_expression", "subroutine_call"):
            yield n


def walk_member_exprs(tree: Tree) -> Iterator[Node]:
    """Yield ``derived_type_member_expression`` nodes (``a%b``)."""
    for n in _ts.walk(tree.root_node):
        if n.type == "derived_type_member_expression":
            yield n


def walk_function_definitions(tree: Tree) -> Iterator[Node]:
    """Yield ``function`` and ``subroutine`` definition nodes."""
    for n in _ts.walk(tree.root_node):
        if n.type in ("function", "subroutine"):
            yield n


# ---------------------------------------------------------------------------
# Smallest-enclosing lookup


@dataclass(frozen=True)
class HitResult:
    """The tightest-fitting node of a given type around a cursor."""

    node: Node
    size: int


def smallest_enclosing(
    nodes: Iterable[Node], line: int, col: int
) -> Node | None:
    """Return the smallest node in ``nodes`` containing ``(line, col)``."""
    best: HitResult | None = None
    for n in nodes:
        if not node_contains(n, line, col):
            continue
        size = node_size(n)
        if best is None or size < best.size:
            best = HitResult(n, size)
    return best.node if best is not None else None


# ---------------------------------------------------------------------------
# Identity helpers for handlers


def function_definition_name(func_or_sub: Node, source: bytes) -> tuple[str, Node] | None:
    """Return ``(name, name_node)`` for a function/subroutine definition.

    The ``name`` child sits inside ``function_statement`` /
    ``subroutine_statement``. Returns ``None`` if the structure is
    unexpected (e.g. parse error around the header).
    """
    stmt_type = (
        "subroutine_statement"
        if func_or_sub.type == "subroutine"
        else "function_statement"
    )
    stmt = next((c for c in func_or_sub.children if c.type == stmt_type), None)
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    if name_node is None:
        return None
    return _ts.node_text(name_node, source), name_node


def function_definition_header_line(func_or_sub: Node) -> int:
    """1-based line of the ``function``/``subroutine`` header statement."""
    sr, _ = func_or_sub.start_point
    return sr + 1


def call_name(node: Node, source: bytes) -> str | None:
    """Return the callee name of a ``call_expression`` / ``subroutine_call``."""
    for c in node.children:
        if c.type == "identifier":
            return _ts.node_text(c, source)
        if c.type == "call":
            continue
    return None


def member_expr_chain(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """Flatten ``a%b%c`` into ``("a", ["b", "c"])``.

    Identical algorithm to the one in :mod:`dimfort.core.ts_checker`
    (kept here as a tiny local copy to avoid pulling the checker into
    the LSP module's import graph for one helper).
    """
    fields: list[str] = []
    cur = node
    while cur.type == "derived_type_member_expression":
        member: Node | None = None
        left: Node | None = None
        for c in cur.children:
            if c.type == "type_member":
                member = c
            elif c.type in ("identifier", "derived_type_member_expression"):
                left = c
        if member is None or left is None:
            return None, []
        fields.insert(0, _ts.node_text(member, source))
        cur = left
    if cur.type != "identifier":
        return None, []
    return _ts.node_text(cur, source), fields


def is_inside_declaration(node: Node) -> bool:
    """``True`` if ``node`` sits inside a ``variable_declaration`` ancestor.

    Used to filter identifier hits when the LSP wants only *uses*, not
    declarations (e.g. go-to-definition).
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "variable_declaration":
            return True
        cur = cur.parent
    return False


def is_inside_type_qualifier(node: Node) -> bool:
    """``True`` if ``node`` sits inside a ``type_qualifier`` ancestor.

    Identifiers inside ``dimension(n)``, ``intent(in)``, etc. are not
    variable references — they live under ``type_qualifier``. Hover and
    inlay hints should skip them.
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "type_qualifier":
            return True
        cur = cur.parent
    return False


def is_call_callee(node: Node) -> bool:
    """``True`` if ``node`` is the callee identifier of a call expression.

    Tree-sitter exposes ``call_expression`` as ``[identifier, argument_list]``
    and ``subroutine_call`` as ``[call, identifier, argument_list]``. The
    callee is the *first* ``identifier`` child of either. Node identity
    is unreliable in tree-sitter (each access through ``.children``
    returns a fresh Python wrapper around the same underlying node), so
    we compare by start byte instead.
    """
    parent = node.parent
    if parent is None or parent.type not in ("call_expression", "subroutine_call"):
        return False
    for c in parent.children:
        if c.type == "identifier":
            return c.start_byte == node.start_byte
        if c.type == "call":
            continue
    return False
