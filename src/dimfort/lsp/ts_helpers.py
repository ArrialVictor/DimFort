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
    """Check whether a 1-based cursor position sits inside a node.

    Converts the 1-based ``(line, col)`` to the 0-based form
    tree-sitter uses internally and tests containment against the
    node's ``start_point`` / ``end_point``. Tree-sitter treats
    ``end_point`` as exclusive; this helper treats the final
    character inclusively so a click on the last byte of a node
    still hovers.

    Args:
        node: Any tree-sitter ``Node`` whose extent populated.
        line_1based: 1-based line number of the cursor.
        col_1based: 1-based column number of the cursor.

    Returns:
        ``True`` when the cursor lies within ``node``'s extent on
        the inclusive convention described above; ``False``
        otherwise.
    """
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
    """Compute a comparable "size" for tightness-of-fit ranking.

    Used by :func:`smallest_enclosing` and other smallest-match
    walks to break ties between overlapping candidate nodes. The
    formula ``(end_row - start_row) * 1000 + (end_col - start_col)``
    gives row-spans dominant weight, so a single-line match always
    ranks tighter than a multi-line one even when the column delta
    is larger.

    Args:
        node: Any tree-sitter ``Node`` whose extent is populated.

    Returns:
        A non-negative integer; smaller means a tighter fit.
    """
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
    """Yield every ``identifier`` node in a tree.

    The walk is unfiltered: it includes identifier occurrences
    inside ``variable_declaration`` headers, ``type_qualifier``
    expressions (``dimension(n)``, ``intent(in)``, …), call argument
    lists, and ordinary expression positions. Callers that want
    only variable *uses* — excluding declaration sites — should
    pair this with :func:`is_inside_declaration` /
    :func:`is_inside_type_qualifier` filters.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``identifier`` ``Node`` encountered, in tree-walk
        order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type == "identifier":
            yield n


def walk_decl_identifiers(tree: Tree) -> Iterator[tuple[Node, Node]]:
    """Yield ``(declaration, name_node)`` pairs for declared variables.

    For every ``variable_declaration`` node in the tree the helper
    yields one pair per declared name. The first element is the
    enclosing declaration (so callers can locate the type prefix,
    the whole statement, the trailing annotation comment, etc.).
    The second element is the identifier ``Node`` that gives the
    variable its name.

    Tree-sitter wraps the name in a ``sized_declarator`` whenever
    the declaration carries an array dimension (``x(10)``) and in
    an ``init_declarator`` whenever it carries an initializer
    (``x = 0``); :func:`_declarator_leading_identifier` peels those
    wrappers so the leading identifier always surfaces directly.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Tuples ``(decl, name_node)`` where ``decl`` is the
        ``variable_declaration`` node and ``name_node`` is the
        identifier ``Node`` for one declared variable.
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
    """Recursively unwrap declarator wrappers to find the leading name.

    Tree-sitter nests ``sized_declarator`` and ``init_declarator``
    nodes when a declaration carries an array dimension or
    initializer. Both wrappers always front-load their first child
    with the variable's identifier (or with another wrapper that
    eventually does). This helper descends through the wrappers and
    returns that identifier.

    Args:
        node: A ``sized_declarator``, ``init_declarator``, or
            similar declarator-shaped ``Node`` to descend into.

    Returns:
        The leading identifier ``Node``, or ``None`` when the
        children deviate from the expected shape.
    """
    for c in node.children:
        if c.type == "identifier":
            return c
        if c.type in ("sized_declarator", "init_declarator"):
            inner = _declarator_leading_identifier(c)
            if inner is not None:
                return inner
    return None


def walk_calls(tree: Tree) -> Iterator[Node]:
    """Yield every call-site node in a tree.

    Surfaces both Fortran call shapes — function calls
    (``call_expression``) and subroutine calls (``subroutine_call``)
    — through a single walk so the inlay / hover / interactions
    handlers can iterate call sites uniformly.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``call_expression`` or ``subroutine_call`` ``Node`` in
        tree-walk order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type in ("call_expression", "subroutine_call"):
            yield n


def walk_member_exprs(tree: Tree) -> Iterator[Node]:
    """Yield every derived-type member access node in a tree.

    Member-access nodes correspond to source like ``a%b`` and chain
    leftward — ``a%b%c`` is one outer
    ``derived_type_member_expression`` whose ``identifier`` left
    child is itself a ``derived_type_member_expression`` for
    ``a%b``. Callers that want the full chain pass each result to
    :func:`member_expr_chain`.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``derived_type_member_expression`` ``Node`` in
        tree-walk order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type == "derived_type_member_expression":
            yield n


def walk_assignments(tree: Tree) -> Iterator[Node]:
    """Yield every assignment statement node in a tree.

    Assignment statements are the primary site of dimensional
    checking: each one binds an LHS variable to an RHS expression
    whose unit must match. Handlers that drive D1.x rule fires walk
    over these nodes to discover the assignments in scope.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``assignment_statement`` ``Node`` in tree-walk order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type == "assignment_statement":
            yield n


def walk_function_definitions(tree: Tree) -> Iterator[Node]:
    """Yield every routine-definition node in a tree.

    Surfaces both ``function`` and ``subroutine`` definitions via a
    single walk. Used by handlers that need to enumerate routines
    (panel scope discovery, header-line lookup, name resolution).

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``function`` or ``subroutine`` ``Node`` in tree-walk
        order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type in ("function", "subroutine"):
            yield n


def walk_use_statements(tree: Tree) -> Iterator[Node]:
    """Yield every ``use_statement`` node in a tree.

    A ``use_statement`` carries the module name (via
    :func:`use_statement_module_name`) and any ``only:`` list. The
    imports panel and the transitive-imports walker rely on this
    enumeration.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``use_statement`` ``Node`` in tree-walk order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type == "use_statement":
            yield n


def walk_module_definitions(tree: Tree) -> Iterator[Node]:
    """Yield every top-level ``module`` definition node in a tree.

    The walk surfaces top-level module blocks; nested modules are
    not produced by the Fortran grammar, so the result is
    effectively the list of modules defined in the file.

    Args:
        tree: Parsed tree-sitter ``Tree`` to walk.

    Yields:
        Each ``module`` definition ``Node`` in tree-walk order.
    """
    for n in _ts.walk(tree.root_node):
        if n.type == "module":
            yield n


def use_statement_module_name(use_node: Node, source: bytes) -> tuple[str, Node] | None:
    """Return ``(name, name_node)`` for the module referenced by a ``use``.

    The grammar exposes the target as a ``module_name`` child of
    the ``use_statement``. This helper finds that child and
    decodes its source text in one step.

    Args:
        use_node: A ``use_statement`` ``Node``.
        source: Raw source bytes used to decode the module name.

    Returns:
        A ``(name, name_node)`` tuple, or ``None`` on a malformed
        ``use`` (no ``module_name`` child) so the caller can skip
        silently.
    """
    name_node = next((c for c in use_node.children if c.type == "module_name"), None)
    if name_node is None:
        return None
    return _ts.node_text(name_node, source), name_node


def module_definition_name(module_node: Node, source: bytes) -> tuple[str, Node] | None:
    """Return ``(name, name_node)`` for a ``module`` definition's header.

    The grammar puts the module's name token inside the
    ``module_statement`` header child; this helper drills one level
    in and decodes it. Same shape as
    :func:`function_definition_name` for symmetry.

    Args:
        module_node: A ``module`` definition ``Node``.
        source: Raw source bytes used to decode the name token.

    Returns:
        A ``(name, name_node)`` tuple, or ``None`` when the header
        or name child isn't present (malformed parse).
    """
    stmt = next((c for c in module_node.children if c.type == "module_statement"), None)
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    if name_node is None:
        return None
    return _ts.node_text(name_node, source), name_node


# ---------------------------------------------------------------------------
# Smallest-enclosing lookup


@dataclass(frozen=True)
class HitResult:
    """A node-plus-tightness record for smallest-enclosing lookups.

    Used internally by :func:`smallest_enclosing` to track the
    best (smallest) candidate seen so far while iterating over a
    set of nodes that contain the cursor.

    Attributes:
        node: The candidate ``Node``.
        size: The candidate's tightness metric, as computed by
            :func:`node_size`. Smaller means a tighter fit.
    """

    node: Node
    size: int


def smallest_enclosing(
    nodes: Iterable[Node], line: int, col: int
) -> Node | None:
    """Return the smallest node in an iterable that contains a position.

    "Smallest" is judged by :func:`node_size`, so multi-line nodes
    rank larger than single-line nodes even when their column
    delta is shorter. Used by hover/inlay/goto handlers to pick the
    most specific match when several candidate nodes overlap the
    cursor.

    Args:
        nodes: Iterable of candidate ``Node`` objects (typically
            the output of one of the targeted walks above).
        line: 1-based line number of the cursor.
        col: 1-based column number of the cursor.

    Returns:
        The tightest-fitting ``Node`` containing the cursor, or
        ``None`` when no node in ``nodes`` contains it.
    """
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

    The grammar puts the routine's name token inside the header
    statement child — ``function_statement`` for functions,
    ``subroutine_statement`` for subroutines. This helper picks the
    right header type based on ``func_or_sub.type`` and drills in.

    Args:
        func_or_sub: A ``function`` or ``subroutine`` definition
            ``Node``.
        source: Raw source bytes used to decode the name token.

    Returns:
        A ``(name, name_node)`` tuple, or ``None`` when the header
        or name child is missing (e.g. a parse error around the
        header).
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
    """Return the 1-based line of a routine's header statement.

    Converts the routine's ``start_point`` from tree-sitter's
    0-based row to the 1-based line numbers used throughout the
    LSP layer. Useful for "go to definition" / panel section
    anchors that target the header line.

    Args:
        func_or_sub: A ``function`` or ``subroutine`` definition
            ``Node``.

    Returns:
        The 1-based line number where the routine's header begins.
    """
    sr, _ = func_or_sub.start_point
    return sr + 1


def call_name(node: Node, source: bytes) -> str | None:
    """Return the callee name of a call-site node.

    For ``call_expression`` the children are
    ``[identifier, argument_list]``; for ``subroutine_call`` they
    are ``[call, identifier, argument_list]``. In both cases the
    callee is the *first* ``identifier`` child, so the helper
    scans children in order and returns the first match.

    Args:
        node: A ``call_expression`` or ``subroutine_call`` ``Node``.
        source: Raw source bytes used to decode the identifier
            text.

    Returns:
        The callee identifier's decoded text, or ``None`` when no
        identifier child is present (malformed parse).
    """
    for c in node.children:
        if c.type == "identifier":
            return _ts.node_text(c, source)
        if c.type == "call":
            continue
    return None


def member_expr_chain(node: Node, source: bytes) -> tuple[str | None, list[str]]:
    """Flatten a derived-type member chain into base + field list.

    Walks a left-recursive chain of
    ``derived_type_member_expression`` nodes — e.g. ``a%b%c`` is a
    chain whose innermost left identifier is ``a`` and whose
    ``type_member`` rights are ``b`` then ``c`` — and returns the
    base identifier plus an ordered list of accessed fields.

    Identical algorithm to the one in
    :mod:`dimfort.core.ts_checker`; kept as a tiny local copy here
    to avoid pulling the checker into the LSP module's import
    graph for one helper.

    Args:
        node: A ``derived_type_member_expression`` ``Node`` at the
            top of the chain.
        source: Raw source bytes used to decode identifier text.

    Returns:
        A tuple ``(base, fields)``. ``base`` is the leftmost
        identifier's text (e.g. ``"a"`` for ``a%b%c``); ``fields``
        is the ordered list of accessed members (``["b", "c"]``).
        On a malformed chain (missing ``type_member`` or non-
        identifier base) the helper returns ``(None, [])``.
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
    """Check whether a node lies inside a variable declaration.

    Walks parent links from ``node`` upward looking for a
    ``variable_declaration`` ancestor. Used by handlers (notably
    go-to-definition and inlay-hint placement) that want to ignore
    identifier occurrences that *introduce* a name and keep only
    the ones that *use* it.

    Args:
        node: Any tree-sitter ``Node`` whose ancestry should be
            inspected.

    Returns:
        ``True`` when a ``variable_declaration`` ancestor exists,
        ``False`` otherwise (including for the root node).
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "variable_declaration":
            return True
        cur = cur.parent
    return False


def is_inside_type_qualifier(node: Node) -> bool:
    """Check whether a node lies inside a type qualifier expression.

    Identifiers inside ``dimension(n)`` / ``intent(in)`` / similar
    type-qualifier expressions are syntactic markers, not
    variable references; hover and inlay hints should skip them.
    This helper walks parent links to detect that situation.

    Args:
        node: Any tree-sitter ``Node`` whose ancestry should be
            inspected.

    Returns:
        ``True`` when a ``type_qualifier`` ancestor exists,
        ``False`` otherwise.
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "type_qualifier":
            return True
        cur = cur.parent
    return False


def is_call_callee(node: Node) -> bool:
    """Check whether a node is the callee identifier of a call site.

    Tree-sitter exposes ``call_expression`` as
    ``[identifier, argument_list]`` and ``subroutine_call`` as
    ``[call, identifier, argument_list]``; the callee is the
    *first* ``identifier`` child of either shape. The helper
    inspects ``node.parent`` to confirm we are inside a call site
    and then verifies that ``node`` is that first identifier.

    Node identity is unreliable in py-tree-sitter — each access
    through ``.children`` returns a fresh Python wrapper around
    the same underlying node, so ``is`` comparisons never hold; we
    compare by ``start_byte`` instead, which uniquely identifies
    the underlying node within a tree.

    Args:
        node: An identifier ``Node`` whose role in its parent we
            want to classify.

    Returns:
        ``True`` when ``node`` is the callee identifier of its
        parent call site, ``False`` for argument identifiers and
        for identifiers whose parent isn't a call site at all.
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
