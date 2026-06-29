"""Pure tree-sitter navigation and node-inspection helpers.

Locating the identifier / scope / expression under a cursor, mapping node
extents to LSP ranges, and rendering one-line node previews — the read-only
tree queries every feature handler (hover, definition, inlay, panel,
interactions) needs. These depend only on tree-sitter and ``lsprotocol``
types; they hold no server state, take no checker ``Ctx``, and never mutate
anything. Extracted from ``server.py`` (the LSP-split refactor) so handler
modules can share one navigation definition.
"""
from __future__ import annotations

from lsprotocol import types as lsp
from tree_sitter import Node, Tree

from dimfort.core import ts_parser as _ts

# A 1-based ((start_line, start_col), (end_line, end_col)) extent, comparable
# to a Diagnostic's Position fields.
_Span = tuple[tuple[int, int], tuple[int, int]]

# Token types we never want to render as their own tree nodes — operators
# and punctuation that visually belong to their parent expression.
_SKIP_TOKEN_TYPES = frozenset({
    "+", "-", "*", "/", "**", "=", "(", ")", ",", "::", "%", "&",
    "[", "]",
})

# Block node types that introduce a name scope.
_SCOPE_NODE_TYPES = ("subroutine", "function", "module", "program")


def _node_lsp_range(node: Node) -> lsp.Range:
    """Convert a tree-sitter node's extent to an LSP 0-based ``Range``.

    Tree-sitter reports ``start_point``/``end_point`` as 0-based
    ``(row, column)`` tuples, which already matches the LSP wire
    format. This helper simply repackages them as the ``lsprotocol``
    ``Range`` dataclass the server hands back to clients.

    Args:
        node: Any tree-sitter ``Node`` whose ``start_point`` and
            ``end_point`` are populated (i.e. every node from a
            successfully parsed tree).

    Returns:
        An ``lsp.Range`` whose ``start`` and ``end`` positions are
        copied verbatim from the node's extent.
    """
    sr, sc = node.start_point
    er, ec = node.end_point
    return lsp.Range(
        start=lsp.Position(line=sr, character=sc),
        end=lsp.Position(line=er, character=ec),
    )


def _node_label(node: Node, source: bytes) -> str:
    """Render a one-line preview of a node's source text.

    Used by the detailed-hover tree to label intermediate expression
    nodes. Newlines and runs of whitespace are collapsed to single
    spaces, and long previews are truncated with an ellipsis so the
    hover stays within a reasonable column width.

    Args:
        node: Tree-sitter ``Node`` whose source byte range will be
            decoded.
        source: Raw source bytes of the file the tree was parsed
            from; sliced via ``node.start_byte`` / ``node.end_byte``.

    Returns:
        A whitespace-collapsed snippet of the node's source, capped
        at 52 characters with ``"..."`` appended when truncated.
    """
    text = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
    text = " ".join(text.split())  # collapse newlines / runs of spaces
    if len(text) > 52:
        text = text[:49] + "..."
    return text


def _node_span_lc(node: Node) -> _Span:
    """Return a node's extent in the 1-based span tuple form.

    The result mirrors DimFort's diagnostic ``Position`` fields
    (1-based line/column), so it can be compared directly against a
    diagnostic's range when correlating tree nodes with checker
    output.

    Args:
        node: Any tree-sitter ``Node`` whose extent is needed.

    Returns:
        A nested tuple ``((start_line, start_col), (end_line,
        end_col))`` with both coordinates 1-based.
    """
    sr, sc = node.start_point
    er, ec = node.end_point
    return (sr + 1, sc + 1), (er + 1, ec + 1)


def _span_within(inner: _Span, outer: _Span) -> bool:
    """Check whether one 1-based span is fully contained in another.

    Containment is inclusive on both ends, matching the convention
    used throughout the LSP layer when correlating diagnostic
    ranges with tree-node extents.

    Args:
        inner: The candidate-contained span as returned by
            :func:`_node_span_lc`.
        outer: The enclosing-candidate span.

    Returns:
        ``True`` when every point of ``inner`` lies within ``outer``;
        ``False`` otherwise.
    """
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _interesting_children(node: Node) -> list[Node]:
    """Return the children worth rendering as sub-tree nodes.

    Filters a tree-sitter node's raw child list down to the subset
    the detailed-hover expression tree should descend into. Operator
    and punctuation tokens are dropped (they belong visually to the
    parent expression's label). For ``call_expression`` /
    ``subroutine_call`` nodes the callee identifier is dropped and
    the ``argument_list`` wrapper is flattened so each positional
    argument lands at the same indent level a binary operator's
    operands would. Keyword arguments are skipped: their unit
    contribution is reported on the call itself, not as a child row.

    Args:
        node: Tree-sitter ``Node`` whose children are about to be
            rendered in the hover tree. Typically an expression-level
            node such as ``math_expression``, ``call_expression``,
            ``subroutine_call``, or ``assignment_statement``.

    Returns:
        A list of child ``Node`` objects in source order, with
        operators / punctuation / callee identifiers / keyword
        arguments stripped.
    """
    is_call = node.type in ("call_expression", "subroutine_call")
    out = []
    seen_callee = False
    for c in node.children:
        if c.type in _SKIP_TOKEN_TYPES:
            continue
        if is_call and c.type == "call":
            # The leading ``call`` keyword on a subroutine_call —
            # structural, not an expression.
            continue
        if is_call and not seen_callee and c.type == "identifier":
            seen_callee = True
            continue
        if c.type == "argument_list":
            for ac in c.children:
                if ac.type in _SKIP_TOKEN_TYPES:
                    continue
                if ac.type == "keyword_argument":
                    continue
                out.append(ac)
            continue
        out.append(c)
    return out


def _identifier_at(tree: Tree, source: bytes, line_1based: int, col_1based: int) -> str | None:
    """Return the text of the smallest ``identifier`` node at the cursor.

    Walks every identifier node in the tree, keeps those whose
    1-based span encloses ``(line_1based, col_1based)``, and picks
    the one with the smallest byte length. The "smallest" heuristic
    matters when a cursor lands on a derived-type member access:
    both the outer ``a%b`` identifier and the inner ``b`` token may
    contain the cursor, and the inner one is the symbol the user
    actually clicked on.

    Args:
        tree: Parsed tree-sitter ``Tree`` for the current document.
        source: Raw source bytes (sliced to recover the identifier
            text once the best node is chosen).
        line_1based: 1-based line number of the cursor.
        col_1based: 1-based column number of the cursor.

    Returns:
        The identifier's decoded text, or ``None`` when the cursor
        isn't on any identifier (whitespace, comment, operator, …).
    """
    best = None
    best_size = None
    for n in _ts.walk(tree.root_node):
        if n.type != "identifier":
            continue
        sp = _ts.position_for(n)
        ep = _ts.end_position_for(n)
        if (sp.line, sp.column) <= (line_1based, col_1based) <= (ep.line, ep.column):
            size = n.end_byte - n.start_byte
            if best_size is None or size < best_size:
                best, best_size = n, size
    if best is None:
        return None
    return source[best.start_byte:best.end_byte].decode("utf-8", "replace")


def _scope_name(scope_node: Node | None, source: bytes) -> str | None:
    """Return the identifier name of a scope block node.

    The ``name`` token sits inside the ``*_statement`` header child
    (e.g. ``subroutine_statement``), not directly on the scope block
    node — true uniformly across subroutine / function / module /
    program. This helper walks one level into the header child and
    decodes the ``name`` token.

    Args:
        scope_node: A scope-introducing block node (``subroutine``,
            ``function``, ``module``, or ``program``) or ``None``.
        source: Raw source bytes used to decode the name token.

    Returns:
        The scope's name as a Python string, or ``None`` when
        ``scope_node`` is ``None`` or the expected header / name
        children aren't present (malformed parse).
    """
    if scope_node is None:
        return None
    stmt_types = tuple(f"{t}_statement" for t in _SCOPE_NODE_TYPES)
    stmt_child = next(
        (c for c in scope_node.children if c.type in stmt_types), None,
    )
    if stmt_child is None:
        return None
    name_node = next(
        (c for c in stmt_child.children if c.type == "name"), None,
    )
    if name_node is None:
        return None
    return _ts.node_text(name_node, source)


def _scope_header(scope_node: Node | None, source: bytes) -> dict[str, str] | None:
    """Build the ``{name, kind}`` header dict for a scope panel section.

    The side panel renders one section per enclosing scope; each
    section's heading uses ``name`` (identifier) and ``kind``
    (block-node type — ``subroutine`` / ``function`` / ``module`` /
    ``program``).

    Args:
        scope_node: Innermost (or any) enclosing scope block node, or
            ``None`` when the cursor sits in bare file-level code.
        source: Raw source bytes used to decode the scope's name.

    Returns:
        A ``{"name": str, "kind": str}`` mapping, or ``None`` when
        ``scope_node`` is ``None`` or its name cannot be recovered.
    """
    if scope_node is None:
        return None
    name = _scope_name(scope_node, source)
    if name is None:
        return None
    return {
        "name": name,
        "kind": scope_node.type,  # subroutine / function / module / program
    }


def _enclosing_scopes(tree: Tree, line_1based: int, col_1based: int) -> list[Node]:
    """Return every scope node enclosing a cursor position.

    Walks the whole tree, keeps every ``_SCOPE_NODE_TYPES`` block
    whose 1-based span encloses the cursor, and orders the results
    outermost-first. For a cursor inside a module-contained
    subroutine the result is ``[module, subroutine]``; for bare
    file-level code (outside any block) the result is empty.

    The side panel stacks one section per scope so the user sees the
    whole environment chain — every visible name source — and not
    just the innermost frame.

    Args:
        tree: Parsed tree-sitter ``Tree`` for the current document.
        line_1based: 1-based line number of the cursor.
        col_1based: 1-based column number of the cursor.

    Returns:
        A list of scope ``Node`` objects ordered outermost-first.
        Empty when no scope encloses the cursor.
    """
    matches = []
    for n in _ts.walk(tree.root_node):
        if n.type not in _SCOPE_NODE_TYPES:
            continue
        sp = _ts.position_for(n)
        ep = _ts.end_position_for(n)
        if (sp.line, sp.column) <= (line_1based, col_1based) <= (ep.line, ep.column):
            matches.append(n)
    # Larger byte-span = more enclosing = outer. Sort outer → inner.
    matches.sort(key=lambda n: n.end_byte - n.start_byte, reverse=True)
    return matches


def _smallest_enclosing_scope(tree: Tree, line_1based: int, col_1based: int) -> Node | None:
    """Return the innermost scope node enclosing a cursor position.

    Convenience wrapper over :func:`_enclosing_scopes`: picks the
    last element of the outermost-first list (the tightest match).
    Used by hover/inlay when only the symbol-lookup scope matters
    and the full chain isn't needed.

    Args:
        tree: Parsed tree-sitter ``Tree`` for the current document.
        line_1based: 1-based line number of the cursor.
        col_1based: 1-based column number of the cursor.

    Returns:
        The innermost enclosing ``subroutine`` / ``function`` /
        ``module`` / ``program`` node, or ``None`` for bare
        file-level code outside any of them.
    """
    scopes = _enclosing_scopes(tree, line_1based, col_1based)
    return scopes[-1] if scopes else None


def _find_expression_root(tree: Tree, line_1based: int, col_1based: int) -> Node | None:
    """Find the smallest expression-bearing node containing the cursor.

    Walks the tree and picks the smallest-byte-span node whose type
    indicates it carries (or computes) a unit — assignments, math
    expressions, relational expressions, calls, identifiers, number
    or complex literals, parenthesised expressions, unary
    expressions, and derived-type member accesses.

    Two post-walk adjustments make the result more useful for
    hover/panel rendering:

    1. If the cursor lands on the *callee identifier* of a call
       (``foo`` in ``foo(x, y)`` or ``call foo(x, y)``), we promote
       the result to the surrounding call node. The bare callee
       identifier has no unit of its own (it would render as a lone
       leaf); the call resolves to the function's return unit plus a
       well-formed argument tree. A cursor on an argument identifier
       is left alone.
    2. If the smallest match has ``has_error`` set, the cursor is
       inside an unparsed region. Tree-sitter's error recovery
       yields a malformed node whose extent bleeds into adjacent
       lines, so the "tree" is nonsense; P001 already flags the
       region, and we return ``None`` rather than render
       confident-looking but wrong output.

    Args:
        tree: Parsed tree-sitter ``Tree`` for the current document.
        line_1based: 1-based line number of the cursor.
        col_1based: 1-based column number of the cursor.

    Returns:
        The expression-root ``Node`` for the cursor, or ``None``
        when the cursor is on a blank line / comment / structural
        keyword, or sits inside a parse-error region.
    """
    expression_types = {
        "assignment_statement",
        "math_expression",
        "relational_expression",
        "call_expression",
        "identifier",
        "number_literal",
        "complex_literal",
        "parenthesized_expression",
        "unary_expression",
        "derived_type_member_expression",
    }
    best = None
    best_size = None
    for n in _ts.walk(tree.root_node):
        if n.type not in expression_types:
            continue
        sp = _ts.position_for(n)
        ep = _ts.end_position_for(n)
        if (sp.line, sp.column) <= (line_1based, col_1based) <= (ep.line, ep.column):
            size = n.end_byte - n.start_byte
            if best_size is None or size < best_size:
                best, best_size = n, size
    # If the cursor landed on the callee *name* of a call, show the whole
    # call — which resolves to the function's return unit and a proper
    # argument tree — rather than the bare callee identifier, which has no
    # unit of its own and renders as a lone 🟡 leaf. A cursor on an
    # argument identifier is left untouched.
    if best is not None and best.type == "identifier":
        parent = best.parent
        if parent is not None:
            if (parent.type == "call_expression"
                    and best.start_byte == parent.start_byte):
                # Function call: the callee is the call's first token.
                best = parent
            elif parent.type == "subroutine_call":
                # ``call foo(...)``: the callee is the first identifier
                # child (the leading ``call`` keyword precedes it, so its
                # start does not coincide with the statement's). Compare
                # by byte offset — py-tree-sitter hands back fresh node
                # wrappers, so identity (``is``) never holds.
                first_id = next(
                    (c for c in parent.children if c.type == "identifier"),
                    None,
                )
                if first_id is not None and first_id.start_byte == best.start_byte:
                    best = parent
    # Don't surface an expression tree for an unparsed region: tree-sitter's
    # error recovery yields a malformed node that bleeds in adjacent lines
    # (the next statement's tokens), so the "tree" is nonsense. The cursor is
    # already flagged by P001 — return None so the panel/hover shows no
    # expression rather than a confident-looking but wrong one.
    if best is not None and best.has_error:
        return None
    return best


def _normalized_unit(unit_text: str, *, scale_mode: bool = False) -> str | None:
    """Render the base-SI normalized form of an annotation.

    The side panel shows the *input* unit as written by the author
    (``hPa``). This helper computes the normalized base-SI expansion
    (``hPa`` → ``kg·m⁻¹·s⁻²``) shown alongside it, so the user can
    see what dimension their annotation actually carries.

    With ``scale_mode`` on, the multiplicative scale factor is
    included (``hPa`` → ``100×kg·m⁻¹·s⁻²``); that mirrors what the
    checker is reasoning about when scale checking is enabled. With
    scale mode off the factor is hidden so the rendered units match
    what the checker considers significant — a linter should not
    display information it is actively ignoring.

    Args:
        unit_text: Author-written unit string, exactly as it would
            appear inside ``@unit{...}``.
        scale_mode: When ``True``, include the multiplicative scale
            factor in the rendered string. Defaults to ``False``.

    Returns:
        The normalized form as a Python string, or ``None`` when
        ``unit_text`` cannot be parsed by the configured unit table.

    Note:
        Uses the installed default unit table (project units are
        already loaded at LSP ``initialize``), so prefixes and
        derived units resolve exactly as during checking.
    """
    from dimfort.core.units import format_unit
    from dimfort.core.units import parse as parse_unit
    try:
        return format_unit(parse_unit(unit_text), show_factor=scale_mode)
    except Exception:
        # audited(0.2.7): silent-OK — returning None on parse failure is
        # the documented contract of this normalisation helper (see the
        # `Returns` docstring entry above). Callers branch on None to
        # render the raw unit_text; no diagnostic is appropriate here.
        return None
