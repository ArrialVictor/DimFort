"""Tree-sitter unit checker.

Replacement for :mod:`dimfort.core.ast_checker`. Walks a tree-sitter
Fortran AST instead of LFortran's JSON tree and emits the same
H001-H004 diagnostic family from the same ``var_units`` / ``field_units``
tables produced by stage 1+2 (scan + attach).

Why a parallel module instead of an in-place rewrite: the AST node
shape is the only thing that changes between the two implementations,
so a side-by-side port keeps the diff reviewable and lets us run both
checkers against the same corpus before retiring the LFortran path
(Phase 5).

Layout mirrors ``ast_checker.py`` deliberately — every helper, emitter,
and collector has a 1:1 counterpart so a reader can navigate by feature
rather than by parser. The only structural difference is that
expression / statement dispatch lives on tree-sitter ``node.type``
strings instead of the LFortran ``node`` discriminator.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from tree_sitter import Node, Tree

from dimfort.core import ts_parser as _ts
from dimfort.core import units as _units_mod
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.symbols import (
    DIMENSIONLESS_INTRINSICS,
    FuncSig,
    ModuleExports,
    PRODUCT_INTRINSICS,
    REDUCTION_INTRINSICS,
    SAME_UNIT_ARG_INTRINSICS,
    TRANSFORMING_INTRINSICS,
    TRANSPARENT_INTRINSICS,
    apply_use_clauses,
)
from dimfort.core.units import Unit, UnitError, UnitTable, equal_dim, format_unit


_RATIONAL_EXPONENT_MAX_DENOMINATOR = 100


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Static context for one file's check pass."""

    file: str
    var_units: dict[str, Unit]
    table: UnitTable
    signatures: dict[str, FuncSig]
    var_types: dict[str, str]                       # varname → derived-type name
    type_field_types: dict[tuple[str, str], str]    # (type, field) → field's struct type
    field_units: dict[tuple[str, str], Unit]        # (type, field) → unit


# ---------------------------------------------------------------------------
# Node-shape helpers
# ---------------------------------------------------------------------------
#
# Tree-sitter exposes nodes as opaque objects with a ``.type`` string
# and an ordered children list. We never index children by named field
# in this module — instead we filter by type. That choice keeps the
# helpers small and the dispatch readable, at the cost of skipping
# tree-sitter's named-field accessor optimisation.

# Punctuation/keyword children that aren't semantically meaningful when
# we're trying to find operands. Centralised so a grammar update that
# adds new syntactic noise can be handled in one place.
_SYNTACTIC_TOKEN_TYPES = frozenset({
    "(", ")", ",", "::", "=", "%", "&", "[", "]",
    "call", "end", "return", "result",
    # Binary / unary operators: present as nodes inside ``math_expression``
    # and ``unary_expression`` alongside the operands. We resolve the
    # operator with :func:`_math_op` separately; ``_content_children``
    # must skip it so operand-position code stays simple.
    "+", "-", "*", "/", "**",
})


def _position(node: Node) -> Position:
    """Convert a tree-sitter node's start to a 1-based ``Position``."""
    sp = _ts.position_for(node)
    return Position(sp.line, sp.column)


def _text(node: Node, source: bytes) -> str:
    """Source text spanned by ``node`` as ``str`` (UTF-8 tolerant)."""
    return _ts.node_text(node, source)


def _content_children(node: Node) -> list[Node]:
    """Children that carry data, not punctuation/keywords."""
    return [c for c in node.children if c.type not in _SYNTACTIC_TOKEN_TYPES]


def _math_op(node: Node) -> str | None:
    """Operator symbol of a ``math_expression``: ``+``, ``-``, ``*``, ``/``, ``**``.

    The operator is exposed as its own child with the operator string
    as its ``type``; we just look for the first non-content child
    whose type is one of those symbols.
    """
    ops = {"+", "-", "*", "/", "**"}
    for c in node.children:
        if c.type in ops:
            return c.type
    return None


def _math_operands(node: Node) -> tuple[Node | None, Node | None]:
    """Return ``(lhs, rhs)`` of a binary ``math_expression``."""
    operands = _content_children(node)
    if len(operands) >= 2:
        return operands[0], operands[1]
    return None, None


def _unary_operand(node: Node) -> Node | None:
    """Return the operand of a ``unary_expression``."""
    for c in _content_children(node):
        return c
    return None


def _assignment_sides(node: Node) -> tuple[Node | None, Node | None]:
    """Return ``(lhs, rhs)`` of an ``assignment_statement``."""
    parts = _content_children(node)
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def _call_callee_name(node: Node, source: bytes) -> str | None:
    """Return the name part of a ``call_expression`` or ``subroutine_call``.

    The callee is always the first ``identifier`` child of the call.
    Subroutine call nodes additionally carry a leading ``call`` keyword
    that we filter out via ``_content_children``.
    """
    for c in _content_children(node):
        if c.type == "identifier":
            return _text(c, source)
        return None  # the first content child wasn't an identifier → unknown shape
    return None


def _call_args(node: Node, source: bytes) -> list[Node]:
    """Return the positional argument expressions of a call.

    Skips keyword arguments and punctuation. Keyword args are emitted
    as ``keyword_argument`` nodes which we deliberately ignore: H004
    matches by *positional index*, and DimFort's signatures don't
    currently model keyword binding.
    """
    arglist = next((c for c in node.children if c.type == "argument_list"), None)
    if arglist is None:
        return []
    out: list[Node] = []
    for c in arglist.children:
        if c.type in _SYNTACTIC_TOKEN_TYPES:
            continue
        if c.type == "keyword_argument":
            continue
        out.append(c)
    return out


def _unwrap_parens(node: Node) -> Node:
    """Strip outer ``parenthesized_expression`` layers."""
    while node.type == "parenthesized_expression":
        inner = _content_children(node)
        if not inner:
            return node
        node = inner[0]
    return node


def _flatten_member_chain(
    node: Node, source: bytes
) -> tuple[str | None, list[str]]:
    """Flatten a ``derived_type_member_expression`` into ``(base, path)``.

    Tree-sitter nests left-leaning: ``o%inner%x`` is
    ``member(member(o, inner), x)``. We unroll into the source-order
    sequence ``("o", ["inner", "x"])`` so the rest of the resolver can
    treat all chain depths uniformly.

    Returns ``(None, [])`` if the structure isn't a clean variable-
    rooted chain (e.g. it bottoms out in a call or array index).
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
        fields.insert(0, _text(member, source))
        cur = left
    if cur.type != "identifier":
        return None, []
    return _text(cur, source), fields


def _is_real_literal(node: Node, source: bytes) -> bool:
    """``number_literal`` is real if its text has ``.`` or scientific notation."""
    text = _text(node, source)
    return "." in text or "e" in text.lower() or "d" in text.lower()


def _constant_exponent(node: Node, source: bytes) -> int | Fraction | None:
    """Decode an expression used as a power exponent into ``int`` or ``Fraction``.

    Accepts ``number_literal`` directly, or a ``unary_expression`` /
    ``parenthesized_expression`` wrapping a number literal (so
    ``b ** -2`` and ``b ** (-2)`` both work). Anything else → ``None``,
    meaning "we don't know the exponent" so the caller stops resolving.
    """
    node = _unwrap_parens(node)
    sign = 1
    if node.type == "unary_expression":
        for c in node.children:
            if c.type == "-":
                sign = -sign
                break
            if c.type == "+":
                break
        inner = _unary_operand(node)
        if inner is None:
            return None
        node = _unwrap_parens(inner)
    if node.type != "number_literal":
        return None
    text = _text(node, source)
    if _is_real_literal(node, source):
        try:
            value = float(text.replace("d", "e").replace("D", "e"))
        except ValueError:
            return None
        try:
            exact = Fraction(value).limit_denominator(
                _RATIONAL_EXPONENT_MAX_DENOMINATOR
            )
        except (TypeError, ValueError, OverflowError):
            return None
        if abs(float(exact) - value) > 1e-6:
            return None
        return sign * exact
    try:
        return sign * int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _resolve(node: Node | None, ctx: _Ctx, source: bytes) -> Unit | None:
    """Return the unit of ``node``, or ``None`` if we don't know.

    "Unknown" is a first-class outcome — many expression shapes
    (intrinsics outside our six categories, casts, complex chains)
    don't yield a useful answer, and returning ``None`` lets the caller
    skip the check rather than risk a false positive.
    """
    if node is None:
        return None

    node = _unwrap_parens(node)
    kind = node.type

    if kind == "identifier":
        name = _text(node, source)
        return ctx.var_units.get(name)

    if kind == "number_literal":
        # Numeric literals are dimensionless; model as the unit-algebra
        # neutral element so multiplications/divisions behave correctly.
        return _units_mod.parse("1", ctx.table)

    if kind == "unary_expression":
        return _resolve(_unary_operand(node), ctx, source)

    if kind == "math_expression":
        op = _math_op(node)
        left, right = _math_operands(node)
        if op == "**":
            base = _resolve(left, ctx, source)
            if base is None or right is None:
                return None
            exponent = _constant_exponent(right, source)
            if exponent is None:
                return None
            try:
                return base.pow(exponent)
            except Exception:
                return None
        left_u = _resolve(left, ctx, source)
        right_u = _resolve(right, ctx, source)
        if left_u is None or right_u is None:
            return None
        if op in ("+", "-"):
            # The walker will already have emitted H002 if these
            # disagree dimensionally. The result unit is the LHS.
            return left_u
        if op == "*":
            return left_u * right_u
        if op == "/":
            return left_u / right_u
        return None

    if kind == "call_expression":
        return _resolve_call(node, ctx, source)

    if kind == "derived_type_member_expression":
        return _resolve_member_chain(node, ctx, source)

    # Unsupported node kind → unknown unit.
    return None


def _resolve_member_chain(
    node: Node, ctx: _Ctx, source: bytes
) -> Unit | None:
    """Resolve a ``derived_type_member_expression`` chain to its unit.

    For ``o%inner%x``: look up ``var_types["o"]`` → T1, step
    ``type_field_types[(T1, "inner")]`` → T2, then return
    ``field_units[(T2, "x")]``. Any unknown step short-circuits.
    """
    base, path = _flatten_member_chain(node, source)
    if base is None or not path:
        return None
    current_type = ctx.var_types.get(base.lower())
    if current_type is None:
        return None
    # All but the last entry are intermediate fields whose type we follow.
    for step in path[:-1]:
        current_type = ctx.type_field_types.get((current_type, step.lower()))
        if current_type is None:
            return None
    final = path[-1]
    return ctx.field_units.get((current_type, final.lower()))


def _resolve_call(node: Node, ctx: _Ctx, source: bytes) -> Unit | None:
    """Resolve a ``call_expression``'s result unit.

    Dispatches in order: intrinsic categories first, then the user-
    defined signature table, then a fallback that treats the name as
    an array index (``arr(i)`` and ``f(x)`` are syntactically
    identical in Fortran).
    """
    name = _call_callee_name(node, source)
    if name is None:
        return None
    name_lc = name.lower()
    arg_exprs = _call_args(node, source)

    if name_lc in DIMENSIONLESS_INTRINSICS:
        return _units_mod.parse("1", ctx.table)

    if name_lc in TRANSFORMING_INTRINSICS:
        if not arg_exprs:
            return None
        base = _resolve(arg_exprs[0], ctx, source)
        if base is None:
            return None
        try:
            return base.pow(TRANSFORMING_INTRINSICS[name_lc])
        except Exception:
            return None

    if name_lc in TRANSPARENT_INTRINSICS:
        if not arg_exprs:
            return None
        return _resolve(arg_exprs[0], ctx, source)

    if name_lc in SAME_UNIT_ARG_INTRINSICS:
        if not arg_exprs:
            return None
        return _resolve(arg_exprs[0], ctx, source)

    if name_lc in PRODUCT_INTRINSICS:
        if len(arg_exprs) < 2:
            return None
        a = _resolve(arg_exprs[0], ctx, source)
        b = _resolve(arg_exprs[1], ctx, source)
        if a is None or b is None:
            return None
        return a * b

    if name_lc in REDUCTION_INTRINSICS:
        if not arg_exprs:
            return None
        return _resolve(arg_exprs[0], ctx, source)

    # User-defined function: look up by lower-cased name.
    sig = ctx.signatures.get(name_lc)
    if sig is not None and not sig.is_subroutine:
        return sig.return_unit

    # Array indexing: ``arr(i)`` shares its node type with ``f(x)``.
    # If the callee resolves to a known variable, the result unit is
    # the variable's own unit (element access, slice, range all carry
    # the same unit as the array).
    if name in ctx.var_units:
        return ctx.var_units[name]
    for k, v in ctx.var_units.items():
        if k.lower() == name_lc:
            return v
    return None


# ---------------------------------------------------------------------------
# Diagnostic emitters (identical text/format to ast_checker.py)
# ---------------------------------------------------------------------------


def _node_span(node: Node) -> tuple[Position, Position]:
    """Return ``(start, end)`` positions in DimFort 1-based coordinates.

    Using the full extent (not just the start) gives VSCode a real
    range to draw the squiggle over, instead of widening a zero-length
    point to a single character.
    """
    sr, sc = node.start_point
    er, ec = node.end_point
    return Position(sr + 1, sc + 1), Position(er + 1, ec + 1)


def _emit_u005_for_unannotated(
    tree, ctx: _Ctx, source: bytes,
) -> list[Diagnostic]:
    """Emit U005 on declarations whose names are used in a checked context but unannotated.

    "Checked context" = the identifier appears as an operand of an
    assignment, a binary expression, a unary expression, or as a call
    argument. We deliberately skip identifiers that only appear inside
    type qualifiers (``dimension(n)``), as the callee of a call (not an
    operand), or solely as a declaration site — none of those would
    cause a check to fail.

    Reports once per (declaration line, name) pair so a variable used
    in many expressions yields a single squiggle on its declaration.
    """
    # First pass: collect names that are *queried* in a checked context.
    queried: set[str] = set()

    def _mark_identifier(ident: Node) -> None:
        name = _ts.node_text(ident, source)
        if not name:
            return
        # Already annotated (any case-fold) → no warning needed.
        if name in ctx.var_units:
            return
        if any(k.lower() == name.lower() for k in ctx.var_units):
            return
        queried.add(name.lower())

    def _walk_operands(expr: Node | None) -> None:
        if expr is None:
            return
        # Identifier directly used as an operand → mark.
        if expr.type == "identifier":
            _mark_identifier(expr)
            return
        # Otherwise descend into expression-shaped children.
        for c in expr.children:
            if c.type in (
                "identifier",
                "math_expression",
                "unary_expression",
                "parenthesized_expression",
                "derived_type_member_expression",
                "call_expression",
            ):
                _walk_operands(c)

    for node in _ts.walk(tree.root_node):
        if node.type == "assignment_statement":
            lhs, rhs = _assignment_sides(node)
            _walk_operands(lhs)
            _walk_operands(rhs)
        elif node.type == "math_expression":
            left, right = _math_operands(node)
            _walk_operands(left)
            _walk_operands(right)
        elif node.type in ("call_expression", "subroutine_call"):
            for arg in _call_args(node, source):
                _walk_operands(arg)

    if not queried:
        return []

    # Second pass: find each queried name's declaration site. Only
    # declarations *in this file* qualify — cross-file usages where the
    # var is imported and annotated elsewhere shouldn't fire here.
    out: list[Diagnostic] = []
    seen: set[tuple[str, int]] = set()
    for decl in _ts.walk(tree.root_node):
        if decl.type != "variable_declaration":
            continue
        for name_node in _decl_name_nodes(decl):
            name_lc = _ts.node_text(name_node, source).lower()
            if name_lc not in queried:
                continue
            sr, sc = name_node.start_point
            er, ec = name_node.end_point
            key = (name_lc, sr)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Diagnostic(
                    file=ctx.file,
                    start=Position(sr + 1, sc + 1),
                    end=Position(er + 1, ec + 1),
                    severity=Severity.WARNING,
                    code="U005",
                    message=(
                        f"{_ts.node_text(name_node, source)!r} is used in a "
                        f"unit-checked expression but has no @unit{{}} annotation"
                    ),
                )
            )
    return out


def _decl_name_nodes(decl: Node):
    """Yield identifier nodes that name a declared entity in ``decl``.

    Same logic as :func:`_collect_decl_names`, but yields the nodes so
    the caller can read their positions.
    """
    for c in decl.children:
        if c.type == "identifier":
            yield c
        elif c.type in _DECLARATOR_WRAPPERS:
            inner = _declarator_leading_node(c)
            if inner is not None:
                yield inner


def _declarator_leading_node(node: Node) -> Node | None:
    for c in node.children:
        if c.type == "identifier":
            return c
        if c.type in _DECLARATOR_WRAPPERS:
            inner = _declarator_leading_node(c)
            if inner is not None:
                return inner
    return None


def _emit_h001(loc: Node, lhs: Unit, rhs: Unit, ctx: _Ctx) -> Diagnostic:
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H001",
        message=(
            f"Assignment unit mismatch: "
            f"{format_unit(lhs)} ≠ {format_unit(rhs)}"
        ),
    )


def _emit_h002(loc: Node, left: Unit, right: Unit, ctx: _Ctx) -> Diagnostic:
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H002",
        message=(
            f"Operand unit mismatch in '+'/'-': "
            f"{format_unit(left)} ≠ {format_unit(right)}"
        ),
    )


def _emit_h003(loc: Node, intrinsic: str, arg_unit: Unit, ctx: _Ctx) -> Diagnostic:
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H003",
        message=(
            f"Intrinsic '{intrinsic}' requires a dimensionless argument; "
            f"got {format_unit(arg_unit)}"
        ),
    )


def _emit_h004(
    loc: Node, func: str, arg_index: int, expected: Unit, actual: Unit, ctx: _Ctx
) -> Diagnostic:
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H004",
        message=(
            f"Call to '{func}': argument {arg_index + 1} unit mismatch: "
            f"expected {format_unit(expected)}, got {format_unit(actual)}"
        ),
    )


# ---------------------------------------------------------------------------
# Expression walker (emits H002/H003/H004)
# ---------------------------------------------------------------------------


def _walk_expressions(
    node: Node | None, ctx: _Ctx, source: bytes
) -> Iterable[Diagnostic]:
    """Recurse over an expression tree, yielding H002/H003/H004."""
    if node is None:
        return
    kind = node.type

    if kind == "math_expression":
        left, right = _math_operands(node)
        yield from _walk_expressions(left, ctx, source)
        yield from _walk_expressions(right, ctx, source)
        if _math_op(node) in ("+", "-"):
            lu = _resolve(left, ctx, source)
            ru = _resolve(right, ctx, source)
            if lu is not None and ru is not None and not equal_dim(lu, ru):
                yield _emit_h002(node, lu, ru, ctx)
        return

    if kind == "call_expression":
        yield from _check_call(node, ctx, source)
        for a in _call_args(node, source):
            yield from _walk_expressions(a, ctx, source)
        return

    # Default recursion: every other expression node may contain
    # nested calls/math we still want to check.
    for c in node.children:
        yield from _walk_expressions(c, ctx, source)


def _check_call(
    node: Node, ctx: _Ctx, source: bytes
) -> Iterable[Diagnostic]:
    """Emit H003/H004 for one ``call_expression``."""
    name = _call_callee_name(node, source)
    if name is None:
        return
    name_lc = name.lower()
    arg_exprs = _call_args(node, source)

    if name_lc in DIMENSIONLESS_INTRINSICS:
        if not arg_exprs:
            return
        u = _resolve(arg_exprs[0], ctx, source)
        if u is None:
            return
        try:
            one = _units_mod.parse("1", ctx.table)
        except UnitError:
            return
        if not equal_dim(u, one):
            yield _emit_h003(node, name_lc, u, ctx)
        return

    sig = ctx.signatures.get(name_lc)
    if sig is None or sig.is_subroutine:
        return
    yield from _check_call_args_against_sig(
        sig, name_lc, arg_exprs, node, ctx, source
    )


def _check_call_args_against_sig(
    sig: FuncSig,
    func_name: str,
    arg_exprs: list[Node],
    call_node: Node,
    ctx: _Ctx,
    source: bytes,
) -> Iterable[Diagnostic]:
    for i, (expected, actual_node) in enumerate(zip(sig.arg_units, arg_exprs)):
        if expected is None or actual_node is None:
            continue
        actual = _resolve(actual_node, ctx, source)
        if actual is None:
            continue
        if not equal_dim(actual, expected):
            yield _emit_h004(call_node, func_name, i, expected, actual, ctx)


# ---------------------------------------------------------------------------
# Collectors: type info + signatures
# ---------------------------------------------------------------------------


def _decl_type_name(decl: Node, source: bytes) -> str | None:
    """Pull the type name from a ``variable_declaration``.

    Tree-sitter's Fortran grammar wraps the declared type differently
    depending on whether it's intrinsic or derived:

    - ``real :: x``         → ``intrinsic_type`` child, no ``type_name``.
    - ``type(particle) :: p`` → ``derived_type`` child containing a
      ``type_name`` child. The ``derived_type`` wrapper is what trips
      up a direct-children scan for ``type_name``.

    Returns the type name (case preserved) when the declaration uses
    a derived type, or ``None`` for intrinsic types.
    """
    for c in decl.children:
        if c.type == "derived_type":
            inner = next((cc for cc in c.children if cc.type == "type_name"), None)
            if inner is not None:
                return _text(inner, source)
        elif c.type == "type_name":
            # Grammar variants occasionally surface type_name directly;
            # keep the fallback so a future grammar tweak doesn't break us.
            return _text(c, source)
    return None


def collect_var_types(tree: Tree, source: bytes) -> dict[str, str]:
    """Return ``{varname_lc: type_name_lc}`` for every ``type(NAME) :: …`` decl."""
    out: dict[str, str] = {}
    for n in _ts.walk(tree.root_node):
        if n.type != "variable_declaration":
            continue
        tn = _decl_type_name(n, source)
        if tn is None:
            continue
        tn_lc = tn.lower()
        for vn in _collect_decl_names(n, source):
            out[vn.lower()] = tn_lc
    return out


def collect_type_field_types(
    tree: Tree, source: bytes
) -> dict[tuple[str, str], str]:
    """Return ``{(struct_lc, field_lc): field_struct_lc}`` for fields of derived type.

    Only fields whose declared type is itself a derived type appear in
    the map; fields of intrinsic type (``real :: m``) are not — the
    resolver uses ``field_units`` for those instead.
    """
    out: dict[tuple[str, str], str] = {}
    for n in _ts.walk(tree.root_node):
        if n.type != "derived_type_definition":
            continue
        stmt = next(
            (c for c in n.children if c.type == "derived_type_statement"), None
        )
        if stmt is None:
            continue
        struct = next((c for c in stmt.children if c.type == "type_name"), None)
        if struct is None:
            continue
        struct_lc = _text(struct, source).lower()
        for decl in n.children:
            if decl.type != "variable_declaration":
                continue
            field_type = _decl_type_name(decl, source)
            if field_type is None:
                continue
            for vn in _collect_decl_names(decl, source):
                out[(struct_lc, vn.lower())] = field_type.lower()
    return out


def collect_function_signatures(
    tree: Tree,
    var_units: dict[str, Unit],
    source: bytes,
) -> dict[str, FuncSig]:
    """Return ``{name_lc: FuncSig}`` for every ``function`` and ``subroutine``."""
    out: dict[str, FuncSig] = {}
    for n in _ts.walk(tree.root_node):
        if n.type not in ("function", "subroutine"):
            continue
        is_subroutine = n.type == "subroutine"
        stmt_type = "subroutine_statement" if is_subroutine else "function_statement"
        stmt = next((c for c in n.children if c.type == stmt_type), None)
        if stmt is None:
            continue
        name_node = next((c for c in stmt.children if c.type == "name"), None)
        if name_node is None:
            continue
        func_name = _text(name_node, source)

        # Argument names from the ``parameters`` block.
        params = next((c for c in stmt.children if c.type == "parameters"), None)
        arg_names: list[str] = []
        if params is not None:
            for c in params.children:
                if c.type == "identifier":
                    arg_names.append(_text(c, source))
        arg_units = tuple(var_units.get(a) for a in arg_names)

        return_unit: Unit | None = None
        if not is_subroutine:
            # ``result(y)`` clause renames the return variable; without
            # it, F90 reuses the function name as the return var.
            result = next(
                (c for c in stmt.children if c.type == "function_result"), None
            )
            if result is not None:
                ret_id = next(
                    (c for c in result.children if c.type == "identifier"), None
                )
                if ret_id is not None:
                    return_unit = var_units.get(_text(ret_id, source))
            if return_unit is None:
                return_unit = var_units.get(func_name)

        out[func_name.lower()] = FuncSig(
            arg_names=tuple(arg_names),
            arg_units=arg_units,
            return_unit=return_unit,
            is_subroutine=is_subroutine,
        )
    return out


def collect_module_exports(
    tree: Tree,
    var_units: dict[str, Unit],
    source: bytes,
) -> dict[str, ModuleExports]:
    """Return ``{module_name_lc: ModuleExports}`` for every ``module`` node.

    Treats every module-level declaration as exported (no ``private``
    honouring — matches the LFortran-side Phase 2 behaviour). Contained
    procedures' signatures are collected by re-running the signature
    collector against the module's children.
    """
    out: dict[str, ModuleExports] = {}
    for n in _ts.walk(tree.root_node):
        if n.type != "module":
            continue
        stmt = next((c for c in n.children if c.type == "module_statement"), None)
        if stmt is None:
            continue
        name_node = next((c for c in stmt.children if c.type == "name"), None)
        if name_node is None:
            continue
        name = _text(name_node, source)

        # Module-level variable names: every variable_declaration that
        # is a *direct* child of the module (not inside a contained
        # function/subroutine or a derived-type block).
        export_var_units: dict[str, Unit] = {}
        for decl in n.children:
            if decl.type != "variable_declaration":
                continue
            for vn in _collect_decl_names(decl, source):
                if vn in var_units:
                    export_var_units[vn] = var_units[vn]

        # Contained procedures: walk only the children to scope correctly.
        signatures: dict[str, FuncSig] = {}
        for child in n.children:
            if child.type in ("function", "subroutine"):
                # Sub-walk: collect_function_signatures iterates the
                # whole tree by default, so build a mini-context.
                signatures.update(
                    _signatures_for_subtree(child, var_units, source)
                )
            elif child.type == "internal_procedures":
                # `contains` block wrapping function/subroutine defs
                for grandchild in child.children:
                    if grandchild.type in ("function", "subroutine"):
                        signatures.update(
                            _signatures_for_subtree(grandchild, var_units, source)
                        )

        out[name.lower()] = ModuleExports(
            name=name,
            var_units=export_var_units,
            signatures=signatures,
        )
    return out


def _signatures_for_subtree(
    node: Node,
    var_units: dict[str, Unit],
    source: bytes,
) -> dict[str, FuncSig]:
    """Run :func:`collect_function_signatures` over a sub-tree only.

    The main collector walks from the root; we need to scope to a single
    module's children. Reuse the inner loop by manually iterating.
    """
    out: dict[str, FuncSig] = {}
    if node.type not in ("function", "subroutine"):
        return out
    is_subroutine = node.type == "subroutine"
    stmt_type = "subroutine_statement" if is_subroutine else "function_statement"
    stmt = next((c for c in node.children if c.type == stmt_type), None)
    if stmt is None:
        return out
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    if name_node is None:
        return out
    func_name = _text(name_node, source)
    params = next((c for c in stmt.children if c.type == "parameters"), None)
    arg_names: list[str] = []
    if params is not None:
        for c in params.children:
            if c.type == "identifier":
                arg_names.append(_text(c, source))
    arg_units = tuple(var_units.get(a) for a in arg_names)
    return_unit: Unit | None = None
    if not is_subroutine:
        result = next(
            (c for c in stmt.children if c.type == "function_result"), None
        )
        if result is not None:
            ret_id = next(
                (c for c in result.children if c.type == "identifier"), None
            )
            if ret_id is not None:
                return_unit = var_units.get(_text(ret_id, source))
        if return_unit is None:
            return_unit = var_units.get(func_name)
    out[func_name.lower()] = FuncSig(
        arg_names=tuple(arg_names),
        arg_units=arg_units,
        return_unit=return_unit,
        is_subroutine=is_subroutine,
    )
    return out


# ---------------------------------------------------------------------------
# Local declarator-name helper
# ---------------------------------------------------------------------------
#
# The scanner in core/annotations.py owns the canonical names-from-
# declaration logic; we reproduce the leading-identifier extraction
# here in a tiny form because the checker only needs the names, not
# the line-range bookkeeping.

_DECLARATOR_WRAPPERS = {"sized_declarator", "init_declarator"}


def _collect_decl_names(decl: Node, source: bytes) -> list[str]:
    names: list[str] = []
    for c in decl.children:
        if c.type == "identifier":
            names.append(_text(c, source))
        elif c.type in _DECLARATOR_WRAPPERS:
            inner = _declarator_leading_identifier(c, source)
            if inner is not None:
                names.append(inner)
    return names


def _declarator_leading_identifier(node: Node, source: bytes) -> str | None:
    for c in node.children:
        if c.type == "identifier":
            return _text(c, source)
        if c.type in _DECLARATOR_WRAPPERS:
            inner = _declarator_leading_identifier(c, source)
            if inner is not None:
                return inner
    return None


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def check(
    tree: Tree,
    var_units: dict[str, str | Unit],
    *,
    source: bytes,
    file: str | Path,
    table: UnitTable | None = None,
    signatures: dict[str, FuncSig] | None = None,
    field_units: dict[tuple[str, str], str | Unit] | None = None,
) -> list[Diagnostic]:
    """Run the checker over a tree-sitter-parsed file.

    Signature parallels :func:`ast_checker.check` so the same callers
    can swap implementations. Tree-sitter requires the original
    ``source`` bytes alongside the tree to extract identifier text.
    """
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )

    # Parse the var_units / field_units tables ahead of the walk so
    # the resolver doesn't pay a parse cost per lookup.
    parsed_vars: dict[str, Unit] = {}
    for name, value in var_units.items():
        if isinstance(value, Unit):
            parsed_vars[name] = value
        else:
            try:
                parsed_vars[name] = _units_mod.parse(value, active_table)
            except UnitError:
                continue
    parsed_fields: dict[tuple[str, str], Unit] = {}
    for (tn, fn), value in (field_units or {}).items():
        if isinstance(value, Unit):
            parsed_fields[(tn.lower(), fn.lower())] = value
        else:
            try:
                parsed_fields[(tn.lower(), fn.lower())] = _units_mod.parse(
                    value, active_table
                )
            except UnitError:
                continue

    if signatures is None:
        signatures = collect_function_signatures(tree, parsed_vars, source)

    ctx = _Ctx(
        file=str(file),
        var_units=parsed_vars,
        table=active_table,
        signatures=signatures,
        var_types=collect_var_types(tree, source),
        type_field_types=collect_type_field_types(tree, source),
        field_units=parsed_fields,
    )
    out: list[Diagnostic] = []
    out.extend(_emit_u005_for_unannotated(tree, ctx, source))

    for node in _ts.walk(tree.root_node):
        kind = node.type

        if kind == "assignment_statement":
            target, value = _assignment_sides(node)
            out.extend(_walk_expressions(value, ctx, source))
            tu = _resolve(target, ctx, source)
            ru = _resolve(value, ctx, source)
            if tu is None or ru is None:
                continue
            if not equal_dim(tu, ru):
                # Position points at the LHS (parity with ast_checker).
                out.append(_emit_h001(target if target is not None else node, tu, ru, ctx))
            continue

        if kind == "subroutine_call":
            name = _call_callee_name(node, source)
            if name is None:
                continue
            name_lc = name.lower()
            arg_exprs = _call_args(node, source)
            for a in arg_exprs:
                out.extend(_walk_expressions(a, ctx, source))
            sig = ctx.signatures.get(name_lc)
            if sig is None or not sig.is_subroutine:
                continue
            out.extend(
                _check_call_args_against_sig(
                    sig, name_lc, arg_exprs, node, ctx, source
                )
            )

    return out


__all__ = [
    "ModuleExports",
    "apply_use_clauses",
    "check",
    "collect_function_signatures",
    "collect_module_exports",
    "collect_type_field_types",
    "collect_var_types",
]
