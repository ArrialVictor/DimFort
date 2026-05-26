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

from dimfort.core import ts_parser as _ts

# Token types we never want to render as their own tree nodes — operators
# and punctuation that visually belong to their parent expression.
_SKIP_TOKEN_TYPES = frozenset({
    "+", "-", "*", "/", "**", "=", "(", ")", ",", "::", "%", "&",
    "[", "]",
})

# Block node types that introduce a name scope.
_SCOPE_NODE_TYPES = ("subroutine", "function", "module", "program")


def _node_lsp_range(node) -> lsp.Range:
    """Convert a tree-sitter node's extent to an LSP 0-based ``Range``."""
    sr, sc = node.start_point
    er, ec = node.end_point
    return lsp.Range(
        start=lsp.Position(line=sr, character=sc),
        end=lsp.Position(line=er, character=ec),
    )


def _node_label(node, source: bytes) -> str:
    """One-line preview of a node's source text, truncated for hover width."""
    text = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
    text = " ".join(text.split())  # collapse newlines / runs of spaces
    if len(text) > 52:
        text = text[:49] + "..."
    return text


def _node_span_lc(node) -> tuple[tuple[int, int], tuple[int, int]]:
    """Node extent as 1-based ((start_line, start_col), (end_line, end_col)),
    comparable to a Diagnostic's Position fields."""
    sr, sc = node.start_point
    er, ec = node.end_point
    return (sr + 1, sc + 1), (er + 1, ec + 1)


def _span_within(inner, outer) -> bool:
    """True iff the ``inner`` span sits inside ``outer`` (inclusive)."""
    return outer[0] <= inner[0] and inner[1] <= outer[1]


def _interesting_children(node) -> list:
    """Return the children worth rendering as sub-tree nodes.

    Skips punctuation/operator tokens. For ``call_expression`` /
    ``subroutine_call``, drops the callee identifier and expands the
    argument list inline so each argument shows up at the same indent
    level as a binary operator's operands would.
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


def _identifier_at(tree, source: bytes, line_1based: int, col_1based: int) -> str | None:
    """Return the text of the smallest ``identifier`` node at the cursor."""
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


def _scope_name(scope_node, source: bytes) -> str | None:
    """Return the scope's identifier name. The ``name`` token sits
    inside the ``*_statement`` header child, not directly on the
    scope block node — true for subroutine / function / module /
    program alike."""
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


def _scope_header(scope_node, source: bytes) -> dict | None:
    """``{name, kind}`` header for the panel's scope section, or
    ``None`` when there's no enclosing scope (bare file-level code)."""
    if scope_node is None:
        return None
    name = _scope_name(scope_node, source)
    if name is None:
        return None
    return {
        "name": name,
        "kind": scope_node.type,  # subroutine / function / module / program
    }


def _enclosing_scopes(tree, line_1based: int, col_1based: int):
    """Return *all* scope nodes enclosing the position, **outermost
    first** (e.g. ``[module, subroutine]`` for a cursor inside a
    module-contained subroutine). Empty for bare file-level code.

    The panel stacks one section per scope so the user sees the whole
    environment chain, not just the innermost frame.
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


def _smallest_enclosing_scope(tree, line_1based: int, col_1based: int):
    """Return the innermost scope node (subroutine / function / module /
    program) enclosing the position, or ``None`` for bare file-level
    code outside any of them."""
    scopes = _enclosing_scopes(tree, line_1based, col_1based)
    return scopes[-1] if scopes else None


def _find_expression_root(tree, line_1based: int, col_1based: int):
    """Find the smallest expression-bearing node containing the cursor.

    Walks the tree and picks the deepest node whose type indicates it
    carries a unit (assignment, math op, call, identifier, literal,
    etc.). Returns ``None`` if the cursor is in a region with no such
    node (blank line, comment, structural keyword).
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
    return best


def _normalized_unit(unit_text: str) -> str | None:
    """Render the base-SI normalized form of an annotation, factor included.

    The panel shows the *input* unit as written (``hPa``); the normalized
    form makes the otherwise-invisible scale factor visible (``hPa`` →
    ``100×kg/(m×s²)``, ``g/kg`` → ``1/1000``). ``None`` if it doesn't parse.
    Uses the installed default unit table (project units already loaded at
    initialize), so prefixes/derived units resolve as in checking.
    """
    from dimfort.core.units import format_unit
    from dimfort.core.units import parse as parse_unit
    try:
        return format_unit(parse_unit(unit_text), show_factor=True)
    except Exception:
        return None
