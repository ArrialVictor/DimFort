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

import bisect
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from tree_sitter import Node, Tree

from dimfort.core import ts_parser as _ts
from dimfort.core import units as _units_mod
from dimfort.core.diagnostics import AutocastEvent, Diagnostic, Position, Severity
from dimfort.core.polymorphism import (
    Conflict,
    SlotEquation,
    UnsupportedPolymorphism,
    free_tyvars_of_sig,
    unify,
)
from dimfort.core.symbols import (
    DIMENSIONLESS_INTRINSICS,
    EXP_INTRINSICS,
    LOG_INTRINSICS,
    PRODUCT_INTRINSICS,
    REDUCTION_INTRINSICS,
    SAME_UNIT_ARG_INTRINSICS,
    TRANSFORMING_INTRINSICS,
    TRANSPARENT_INTRINSICS,
    FuncSig,
    ModuleExports,
    apply_use_clauses,
)
from dimfort.core.trace import Trace, current_trace, with_trace
from dimfort.core.units import (
    Exponent,
    ExpWrap,
    LogWrap,
    Unit,
    UnitError,
    UnitExpr,
    UnitTable,
    combine,
    compare,
    equal_dim,
    format_unit,
    format_unit_source,
    power,
    wrap_exp,
    wrap_log,
)


def _is_wrapper(u: UnitExpr | None) -> bool:
    return isinstance(u, (LogWrap, ExpWrap))


def _outer_unary_sign(node: Node) -> int:
    """Walk up the AST from ``node``, counting enclosing unary minuses.

    Peels ``unary_expression(-)`` and ``parenthesized_expression``
    layers; stops at the first other parent. Returns +1 or -1. Used
    by the math_expression resolver to propagate an outer ``-`` sign
    to the literal coefficient of an inner ``*`` / ``/`` so R5.4
    receives the correct ``k``.
    """
    sign = 1
    parent = node.parent
    while parent is not None:
        if parent.type == "unary_expression":
            for c in parent.children:
                if c.type == "-":
                    sign = -sign
                    break
                if c.type == "+":
                    break
            parent = parent.parent
        elif parent.type == "parenthesized_expression":
            parent = parent.parent
        else:
            break
    return sign

_RATIONAL_EXPONENT_MAX_DENOMINATOR = 100


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Static context for one file's check pass."""

    file: str
    var_units: dict[str, UnitExpr]
    table: UnitTable
    signatures: dict[str, FuncSig]
    var_types: dict[str, str]                       # varname → derived-type name
    type_field_types: dict[tuple[str, str], str]    # (type, field) → field's struct type
    field_units: dict[tuple[str, str], UnitExpr]    # (type, field) → unit
    # OQ4: PARAMETER literal values, keyed by lowercased name. Populated
    # from ``collect_parameter_values``. Used by ``_resolve_constant_value``
    # so ``p ** kappa`` (where ``kappa`` is a PARAMETER with a literal
    # initialiser) resolves the exponent as a rational rather than firing
    # D1.4. Empty dict ⇒ behaves identically to "no PARAMETER awareness".
    parameter_values: dict[str, Fraction | int] = field(default_factory=dict)
    # ``@unit_assume`` escape hatch, keyed by 1-based source line. Value is
    # ``(assumed_unit, reason, column, end_column)`` — the column pair spans
    # the full ``@unit_assume{...}`` directive so the U020 squiggle covers
    # the whole thing. When an assignment statement's line span covers one
    # of these, the checker skips deriving the RHS (suppressing D1.4 +
    # interior fires), treats the result as ``assumed_unit`` for the LHS
    # consistency check, and emits a U020 INFO.
    assumes: dict[int, tuple[UnitExpr, str, int, int]] = field(default_factory=dict)
    # ``@unit_affine_conversion`` directives (Phase 2c), keyed by 1-based
    # source line. Value is ``(src_text, tgt_text, column)`` — the raw unit
    # names are resolved at verify time (a bad name surfaces as S003 with
    # the statement, not a scan error). When an assignment's line span
    # covers one of these, the checker *verifies* the conversion arithmetic
    # against the known offsets: a valid conversion suppresses the S002 the
    # statement would otherwise raise; an invalid one emits S003. See
    # docs/design/scale.md §11.
    affine_conversions: dict[int, tuple[str, str, int]] = field(
        default_factory=dict
    )
    # True when the caller supplied a by-scope table (even an empty one),
    # i.e. scope-aware mode is active. In that mode ``unit_for`` resolves
    # ONLY through the scoped table (incl. the ``(None, name)`` module /
    # use-import layer) and never falls back to the flat first-seen
    # ``var_units`` map — that flat fallback let an unannotated parameter
    # absorb a same-named symbol from an unrelated routine (finding #018).
    scope_aware: bool = False
    # Scope-aware annotation table. ``var_units`` above remains the
    # flat first-seen view (compat). When ``var_units_by_scope`` is
    # populated, ``unit_for(name, byte_offset)`` honours the enclosing
    # subroutine/function so same-named params across routines don't
    # alias. Empty dict ⇒ behaves identically to flat lookup.
    var_units_by_scope: dict[tuple[str | None, str], UnitExpr] = field(
        default_factory=dict
    )
    # Byte-range cover of every subroutine/function (sorted by
    # ``start_byte``). Used to map a node's byte offset to its
    # enclosing scope name for scope-aware lookups.
    routine_scopes: tuple[tuple[int, int, str], ...] = ()
    # Cached parallel arrays for bisect: starts[i] == routine_scopes[i][0].
    _scope_starts: tuple[int, ...] = ()
    # Case-insensitive mirrors of the two unit tables. Fortran identifiers
    # are case-insensitive, but ``var_units`` / ``var_units_by_scope`` are
    # keyed in *declaration* case. Cross-file ``use`` imports key entries in
    # the module's declaration case (e.g. ``RHOH2O``); a consumer that
    # references the symbol in another case (e.g. ``rhoh2o = ratm/100.``)
    # would miss a case-sensitive lookup and silently lose its unit. Built
    # once per file in ``__post_init__``; ``unit_for`` queries these.
    _var_units_lc: dict[str, UnitExpr] = field(default_factory=dict)
    _by_scope_lc: dict[tuple[str | None, str], UnitExpr] = field(
        default_factory=dict
    )
    # Case-insensitive mirror of ``field_units`` (derived-type ``%`` fields).
    # ``_resolve_member_chain`` lowercases the type + field at lookup, but
    # ``field_units`` is keyed in declaration case, so without this mirror
    # any type/field with an uppercase letter would never resolve.
    _field_units_lc: dict[tuple[str, str], UnitExpr] = field(default_factory=dict)
    # Opt-in scale checking (Phase 1: multiplicative). When False (default)
    # the checker is dimension-only — ``factor`` differences are ignored,
    # exactly as before. When True, dim-equal-but-factor-differing operands
    # fire S001. Dimension-only must stay first-class; see docs/design/scale.md.
    scale_mode: bool = False

    def __post_init__(self) -> None:
        if self.var_units and not self._var_units_lc:
            for k, v in self.var_units.items():
                self._var_units_lc.setdefault(k.lower(), v)
        if self.var_units_by_scope and not self._by_scope_lc:
            for (s, n), v in self.var_units_by_scope.items():
                self._by_scope_lc.setdefault(
                    (s.lower() if s is not None else None, n.lower()), v
                )
        if self.field_units and not self._field_units_lc:
            for (t, fld), v in self.field_units.items():
                self._field_units_lc.setdefault((t.lower(), fld.lower()), v)

    def scope_at(self, byte_offset: int) -> str | None:
        """Innermost enclosing routine scope name (lower-cased) for ``byte_offset``.

        ``None`` if the offset isn't inside any routine (module-level
        or file-level). The byte ranges are nested-tolerant: a
        CONTAINS-nested procedure's range is fully contained in its
        parent's, so the innermost match wins.
        """
        if not self.routine_scopes:
            return None
        # Bisect to the rightmost range whose start <= byte_offset, then
        # walk backward through any earlier ranges that also contain it
        # (handles nesting where an outer range starts earlier).
        idx = bisect.bisect_right(self._scope_starts, byte_offset) - 1
        best: tuple[int, int, str] | None = None
        while idx >= 0:
            lo, hi, name = self.routine_scopes[idx]
            if lo <= byte_offset < hi and (
                best is None or (hi - lo) < (best[1] - best[0])
            ):
                best = (lo, hi, name)
            idx -= 1
        return best[2] if best else None

    def unit_for(self, name: str, byte_offset: int) -> UnitExpr | None:
        """Resolve ``name`` at ``byte_offset`` honouring subroutine scope.

        Order: enclosing routine's scope → file/module-level scope →
        flat fallback (so callers that didn't populate the scoped
        table keep working).
        """
        name_lc = name.lower()
        if self.scope_aware:
            # Scope-aware: (scope, name) then the (None, name) layer
            # (module-level decls + use-imports). NO flat fallback — a
            # name absent here is genuinely unannotated in this scope, and
            # falling back to the flat first-seen map would let it absorb
            # a same-named symbol from an unrelated routine (finding #018).
            scope = self.scope_at(byte_offset)
            if scope is not None:
                u = self._by_scope_lc.get((scope, name_lc))
                if u is not None:
                    return u
            return self._by_scope_lc.get((None, name_lc))
        return self._var_units_lc.get(name_lc)


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


# Verdict tokens returned by :func:`_assignment_homogeneity`. Every
# consumer (checker / LSP renderer / future audit tooling) reads the
# verdict and decides on its own action:
#
# - ``"homogeneous"``    — LHS and RHS units match. No diagnostic; 🟢.
# - ``"autocast"``       — pure-numeric-constant RHS took on LHS unit
#                          (R4.4). No diagnostic, but emits an
#                          :class:`AutocastEvent` for audit. 🟢.
# - ``"wrapper_untag"``  — implicit ``LogWrap`` / ``ExpWrap`` untag
#                          (D1.6). Emits H010. 🟡.
# - ``"mismatch"``       — LHS and RHS resolved to different dims.
#                          Emits H001. 🔴.
# - ``"unresolved"``     — at least one side unresolved (no LHS
#                          annotation, RHS resolution failed, …). No
#                          diagnostic from this rule; 🟡.
AssignmentVerdict = str  # one of the literals above

# A scope-aware unit lookup: ``(name, scope_lc) -> unit-or-None``.
_ScopedLookup = Callable[[str, str | None], UnitExpr | None]


def _assignment_homogeneity(
    target: Node | None,
    value: Node | None,
    ctx: _Ctx,
    source: bytes,
) -> tuple[AssignmentVerdict, UnitExpr | None, UnitExpr | None]:
    """Decide what an assignment's homogeneity status is — and what
    units it has after applying the initialization-autocast rule R4.4.

    Returns ``(verdict, lhs_unit, effective_rhs_unit)``. ``effective_
    rhs_unit`` equals ``lhs_unit`` when the verdict is ``"autocast"``
    or ``"homogeneous"``; otherwise it's whatever ``_resolve`` returned
    for the RHS.

    This function is the *single source of truth* for what an
    assignment looks like to any consumer:

    - The checker calls it to drive diagnostic emission and to record
      :class:`AutocastEvent`s.
    - The LSP renderers (panel, hover) call it to decide the marker
      and the units they display.

    Keep the autocast detection here only. Renderers must never
    detect autocast locally — that's how the marker disagreed with
    the diagnostic stream before this refactor.
    """
    if target is None or value is None:
        return "unresolved", None, None
    tu = _resolve(target, ctx, source)
    ru = _resolve(value, ctx, source)
    if tu is None or ru is None:
        return "unresolved", tu, ru
    if equal_dim(tu, ru):
        return "homogeneous", tu, ru
    # Dims differ — three sub-cases.
    if _is_pure_numeric_constant(value):
        # R4.4: literal initialization. The RHS effectively takes on
        # the LHS unit; no diagnostic.
        from dimfort.core.trace import trace_step
        trace_step("R4.4", (tu, ru), tu)
        return "autocast", tu, tu
    if (
        isinstance(tu, Unit)
        and isinstance(ru, (LogWrap, ExpWrap))
        and isinstance(ru.inner, Unit)
        and equal_dim(tu, ru.inner)
    ):
        return "wrapper_untag", tu, ru
    return "mismatch", tu, ru


def _assume_for_node(
    node: Node, ctx: _Ctx
) -> tuple[UnitExpr, str, int, int, int] | None:
    """Return the ``@unit_assume`` covering ``node``, or ``None``.

    The directive is written as a trailing ``!< @unit_assume{...}`` on
    the statement (single-line: same line; continued: the last physical
    line). We scan only the node's own line span so a trailing assume on
    statement N never bleeds onto statement N+1. Returns
    ``(unit, reason, line, column, end_column)``.
    """
    if not ctx.assumes:
        return None
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    for ln in range(start_line, end_line + 1):
        hit = ctx.assumes.get(ln)
        if hit is not None:
            unit, reason, col, end_col = hit
            return unit, reason, ln, col, end_col
    return None


def _affine_conv_for_node(
    node: Node, ctx: _Ctx
) -> tuple[str, str, int, int] | None:
    """Return the ``@unit_affine_conversion`` directive covering ``node``,
    or ``None``. Returns ``(src_text, tgt_text, line, column)``.

    Same line-span scan as :func:`_assume_for_node` so a trailing directive
    on statement N never bleeds onto N+1.
    """
    if not ctx.affine_conversions:
        return None
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    for ln in range(start_line, end_line + 1):
        hit = ctx.affine_conversions.get(ln)
        if hit is not None:
            src, tgt, col = hit
            return src, tgt, ln, col
    return None


def _build_autocast_event(
    value_node: Node, lhs_unit: UnitExpr, file: str, source: bytes,
) -> AutocastEvent:
    """Construct an :class:`AutocastEvent` for an R4.4 fire at ``value_node``."""
    start, end = _node_span(value_node)
    return AutocastEvent(
        file=file,
        start=start,
        end=end,
        literal_text=_text(value_node, source),
        inferred_unit=format_unit(lhs_unit),
        context="assignment_rhs",
    )


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


def _is_number_literal_node(node: Node) -> bool:
    """True if ``node`` is a bare numeric literal (parens/unary +/- ok).

    Detects literal-ness structurally rather than via
    ``_resolve_constant_value`` — the latter returns ``None`` for values
    it can't rationalise (e.g. E-notation like ``2.546E-5``), which must
    still be treated as a numeric literal for implicit-cast purposes.
    """
    n = _unwrap_parens(node)
    if n.type == "number_literal":
        return True
    if n.type == "unary_expression":
        inner = _unary_operand(n)
        return inner is not None and _unwrap_parens(inner).type == "number_literal"
    return False


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


def _resolve_constant_value(
    node: Node | None, ctx: _Ctx | None, source: bytes,
) -> int | Fraction | None:
    """Resolve a node to a constant rational value.

    Handles:
    - A bare ``number_literal`` (possibly wrapped in unary ``-``/``+`` /
      parens) — same as the legacy ``_constant_exponent``.
    - A reference to a PARAMETER whose initialiser collapses to a rational
      (via ``ctx.parameter_values``). Enables ``p ** kappa`` patterns
      (Exner, etc.) to recover a literal-rational exponent.
    - Simple constant-folded arithmetic over the above: ``2./7.``,
      ``RD/RCPD`` (where both are PARAMETERs), ``-kappa`` etc.

    Returns ``None`` when any sub-expression isn't known. Safe to call
    with ``ctx=None``; PARAMETER lookup is then skipped.
    """
    if node is None:
        return None
    node = _unwrap_parens(node)
    if node.type == "number_literal":
        return _constant_exponent(node, source)
    if node.type == "unary_expression":
        sign = 1
        for c in node.children:
            if c.type == "-":
                sign = -sign
                break
            if c.type == "+":
                break
        inner = _unary_operand(node)
        inner_val = _resolve_constant_value(inner, ctx, source)
        if inner_val is None:
            return None
        return sign * inner_val
    if node.type == "identifier" and ctx is not None and ctx.parameter_values:
        name = _text(node, source).lower()
        return ctx.parameter_values.get(name)
    if node.type == "math_expression":
        op = _math_op(node)
        if op not in ("+", "-", "*", "/"):
            return None
        left, right = _math_operands(node)
        l_val = _resolve_constant_value(left, ctx, source)
        r_val = _resolve_constant_value(right, ctx, source)
        if l_val is None or r_val is None:
            return None
        if op == "+":
            return l_val + r_val
        if op == "-":
            return l_val - r_val
        if op == "*":
            return l_val * r_val
        # op == "/"
        if r_val == 0:
            return None
        return Fraction(l_val) / Fraction(r_val)
    return None


def _resolve_symbolic_exponent(
    node: Node | None, ctx: _Ctx | None, source: bytes,
) -> Exponent | None:
    """Resolve a node to a symbolic Exponent (linear form over named
    dim'less generators + a rational constant).

    Used by the ``**`` resolver as a fallback when the exponent is
    *not* a literal rational. Returns ``None`` if the expression isn't
    representable as a linear Exponent — the caller then falls back to
    the existing D1.4 path.

    Allowed shapes (the linear-form fragment):

    - Literal rational / PARAMETER reference (delegates to
      ``_resolve_constant_value``, then promotes to a constant
      Exponent).
    - Bare identifier whose annotated unit is dim'less (becomes an
      opaque symbol named after the identifier).
    - Unary ``-``/``+``.
    - ``+`` / ``-`` of two sub-Exponents (sum or difference).
    - ``*`` of two sub-Exponents where at least one side is
      pure-constant (scalar multiplication; symbol×symbol is
      non-linear and surfaces ``None``).
    - ``/`` of any sub-Exponent by a *constant* sub-Exponent
      (scalar division). Symbol-in-denominator is non-linear.
    """
    if node is None:
        return None
    node = _unwrap_parens(node)
    # 1. Pure constant (literal or PARAMETER) → promote.
    c = _resolve_constant_value(node, ctx, source)
    if c is not None:
        return Exponent.from_value(c)
    # 2. Bare identifier of dim'less type → opaque symbol.
    if node.type == "identifier":
        if ctx is None:
            return None
        name = _text(node, source)
        u = ctx.unit_for(name, node.start_byte)
        if u is not None and _is_dimensionless(u):
            return Exponent.from_symbol(name)
        return None
    # 3. Unary +/-.
    if node.type == "unary_expression":
        sign = 1
        for c2 in node.children:
            if c2.type == "-":
                sign = -sign
                break
            if c2.type == "+":
                break
        inner = _unary_operand(node)
        if inner is None:
            return None
        inner_e = _resolve_symbolic_exponent(inner, ctx, source)
        if inner_e is None:
            return None
        return inner_e * sign
    # 4. Linear math.
    if node.type == "math_expression":
        op_ = _math_op(node)
        if op_ not in ("+", "-", "*", "/"):
            return None
        left, right = _math_operands(node)
        if left is None or right is None:
            return None
        l_e = _resolve_symbolic_exponent(left, ctx, source)
        r_e = _resolve_symbolic_exponent(right, ctx, source)
        if l_e is None or r_e is None:
            return None
        try:
            if op_ == "+":
                return l_e + r_e
            if op_ == "-":
                return l_e - r_e
            if op_ == "*":
                return l_e * r_e   # raises UnitError if non-linear
            # op_ == "/"
            r_const = r_e.as_fraction()
            if r_const is None or r_const == 0:
                return None
            return l_e * (Fraction(1) / r_const)
        except UnitError:
            return None
    return None


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


def _resolve(node: Node | None, ctx: _Ctx, source: bytes) -> UnitExpr | None:
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
        return ctx.unit_for(name, node.start_byte)

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
            exponent_value: int | Fraction | Exponent | None = (
                _resolve_constant_value(right, ctx, source)
            )
            exponent_unit = _resolve(right, ctx, source)
            # Symbolic fallback: when the exponent isn't a literal
            # rational, try to express it as a linear Exponent over
            # named dim'less generators. If successful, power() will
            # produce a Unit with symbolic dimensions (Pa^kappa-style).
            if exponent_value is None:
                exponent_value = _resolve_symbolic_exponent(
                    right, ctx, source,
                )
            result, _ = power(base, exponent_unit, exponent_value)
            return result
        left_u = _resolve(left, ctx, source)
        right_u = _resolve(right, ctx, source)
        if left_u is None or right_u is None:
            return None
        if op not in ("+", "-", "*", "/"):
            return None
        left_lit: int | Fraction | Exponent | None = (
            _resolve_constant_value(left, ctx, source) if left is not None else None
        )
        right_lit: int | Fraction | Exponent | None = (
            _resolve_constant_value(right, ctx, source) if right is not None else None
        )
        # Symbolic fallback: if the operand isn't a literal rational,
        # try to express it as a linear Exponent over dim'less
        # generators. This is what closes the Tetens-family D1.4s by
        # letting `combine`'s R5.4 (log-power identity) accept symbolic
        # multipliers.
        if left_lit is None and left is not None:
            left_lit = _resolve_symbolic_exponent(left, ctx, source)
        if right_lit is None and right is not None:
            right_lit = _resolve_symbolic_exponent(right, ctx, source)
        # Outer-unary-minus sign propagation. Tree-sitter parses
        # ``-1.0 * LOG(p)`` as ``-(1.0 * LOG(p))`` — the literal child
        # of the inner math_expression is positive, so R5.4 sees
        # ``1 × LOG(Pa) → LOG(Pa)`` instead of ``-1 × LOG(Pa) →
        # LOG(1/Pa)``. Peel any enclosing unary_expression layers and
        # flip the single literal operand's sign accordingly.
        sign = _outer_unary_sign(node)
        if sign == -1 and op in ("*", "/"):
            if left_lit is not None and right_lit is None:
                left_lit = -left_lit
            elif right_lit is not None and left_lit is None:
                right_lit = -right_lit
        result, _diag = combine(
            op, left_u, right_u,
            a_literal=left_lit, b_literal=right_lit,
        )
        return result

    if kind == "call_expression":
        return _resolve_call(node, ctx, source)

    if kind == "derived_type_member_expression":
        return _resolve_member_chain(node, ctx, source)

    # Unsupported node kind → unknown unit.
    return None


def _resolve_member_chain(
    node: Node, ctx: _Ctx, source: bytes
) -> UnitExpr | None:
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
    return ctx._field_units_lc.get((current_type, final.lower()))


def _resolve_call(node: Node, ctx: _Ctx, source: bytes) -> UnitExpr | None:
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

    if name_lc in LOG_INTRINSICS:
        if not arg_exprs:
            return None
        arg = _resolve(arg_exprs[0], ctx, source)
        if arg is None:
            return None
        return wrap_log(arg)

    if name_lc in EXP_INTRINSICS:
        if not arg_exprs:
            return None
        arg = _resolve(arg_exprs[0], ctx, source)
        if arg is None:
            return None
        return wrap_exp(arg)

    if name_lc in TRANSFORMING_INTRINSICS:
        if not arg_exprs:
            return None
        base = _resolve(arg_exprs[0], ctx, source)
        if base is None:
            return None
        if not isinstance(base, Unit):
            return None  # SQRT/ABS on a wrapper: sub-step 4
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
        # MAX/MIN require all args to share a unit. Return the first
        # "carrying" operand's unit — i.e. skip over dimensionless numeric
        # literals (e.g. the 0. in ``max(0., qq)``), which adopt the
        # dimensioned sibling's unit rather than forcing the result to {1}.
        fallback = _resolve(arg_exprs[0], ctx, source)
        for a in arg_exprs:
            u = _resolve(a, ctx, source)
            if u is None:
                continue
            if isinstance(u, Unit) and _is_dimensionless(u) and _is_number_literal_node(a):
                continue
            return u
        return fallback

    if name_lc in PRODUCT_INTRINSICS:
        if len(arg_exprs) < 2:
            return None
        ua = _resolve(arg_exprs[0], ctx, source)
        ub = _resolve(arg_exprs[1], ctx, source)
        if ua is None or ub is None:
            return None
        if not (isinstance(ua, Unit) and isinstance(ub, Unit)):
            return None  # wrapper product is sub-step 3
        return ua * ub

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
    u = ctx.unit_for(name, node.start_byte)
    if u is not None:
        return u
    if not ctx.scope_aware:
        # Legacy flat fallback only outside scope-aware mode; in
        # scope-aware mode ``unit_for`` is authoritative (no cross-scope
        # bleed — finding #018).
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


def _emit_unparsed_regions(tree: Tree, ctx: _Ctx) -> list[Diagnostic]:
    """Emit one P001 (INFO) per contiguous region tree-sitter couldn't parse.

    The honesty marker: where the parser left ``ERROR`` / ``missing`` nodes the
    checker resolves nothing, so we say so rather than implying the lines are
    clean. Nested error nodes for one bad construct are coalesced by line span
    into a single region. See docs/design/unparsed-regions.md.

    Only the *innermost* error nodes are reported: tree-sitter often wraps a
    single bad statement in an outer ``ERROR`` node spanning the whole enclosing
    construct (e.g. the entire subroutine), so an error node that contains
    another error node is dropped — otherwise one stray line would blue-underline
    a whole routine.

    Each surviving ERROR is then widened to its smallest ``*_statement`` (or
    ``subroutine_call``) ancestor with ``has_error=True``: tree-sitter's error
    recovery commonly swallows the immediately-following clean statement into
    the bad statement's parse node (the parent assignment_statement spans both
    lines with ``has_error=True``), so the panel produces degraded results on
    the swallowed line too. Widening the P001 to that ancestor honestly marks
    the full untrustworthy range; otherwise users see a single blue line plus
    a silently-empty Expression panel one line below.
    """
    def _statement_ancestor_with_error(node: Node) -> Node | None:
        cur = node.parent
        while cur is not None:
            if cur.has_error and (
                cur.type.endswith("_statement") or cur.type == "subroutine_call"
            ):
                return cur
            cur = cur.parent
        return None

    errs = list(_ts.error_nodes(tree))
    if not errs:
        return []
    bspans = [(n.start_byte, n.end_byte) for n in errs]
    spans: list[tuple[int, int, int, int]] = []
    for i, n in enumerate(errs):
        si, ei = bspans[i]
        # Drop this node if it strictly contains another error node (it's just
        # tree-sitter's outer wrapper, not the precise unparsed spot).
        contains_other = any(
            j != i and si <= sj and ej <= ei and (sj, ej) != (si, ei)
            for j, (sj, ej) in enumerate(bspans)
        )
        if contains_other:
            continue
        # Widen to the smallest statement-level ancestor with has_error=True,
        # which captures any clean statement swallowed by error recovery.
        anchor = _statement_ancestor_with_error(n) or n
        start, end = _node_span(anchor)
        spans.append((start.line, start.column, end.line, end.column))
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for sl, sc, el, ec in spans[1:]:
        prev = merged[-1]
        if sl <= prev[2] + 1:  # overlapping or adjacent lines → one region
            if (el, ec) > (prev[2], prev[3]):
                prev[2], prev[3] = el, ec
        else:
            merged.append([sl, sc, el, ec])
    return [
        Diagnostic(
            file=str(ctx.file),
            start=Position(sl, sc),
            end=Position(el, ec),
            severity=Severity.INFO,
            code="P001",
            message=(
                "could not parse this region — DimFort makes no unit "
                "guarantee here"
            ),
        )
        for sl, sc, el, ec in merged
    ]


def _emit_u005_for_unannotated(
    tree: Tree, ctx: _Ctx, source: bytes,
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

    Single-pass implementation: while walking the tree we
    simultaneously (a) collect names appearing in checked expressions
    and (b) record every variable_declaration we see. After the walk,
    we cross-reference queried names against recorded declarations.
    Avoids the two-pass tree walk an earlier revision had — the
    second pass alone was visible on profiles of large workspaces.
    """
    queried: set[str] = set()
    first_use: dict[str, tuple[int, int]] = {}
    # name_lc -> list of (start_row, start_col, end_row, end_col, raw_name)
    decls_by_name: dict[str, list[tuple[int, int, int, int, str]]] = {}
    # Skip already-annotated names cheaply — precompute a lowercased set.
    annotated_lc = {k.lower() for k in ctx.var_units}

    def _mark_identifier(ident: Node) -> None:
        name = _ts.node_text(ident, source)
        if not name:
            return
        key = name.lower()
        if key in annotated_lc:
            return
        queried.add(key)
        sr, sc = ident.start_point
        prior = first_use.get(key)
        if prior is None or (sr, sc) < prior:
            first_use[key] = (sr, sc)

    def _walk_operands(expr: Node | None) -> None:
        if expr is None:
            return
        if expr.type == "identifier":
            _mark_identifier(expr)
            return
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
        ntype = node.type
        if ntype == "assignment_statement":
            lhs, rhs = _assignment_sides(node)
            _walk_operands(lhs)
            _walk_operands(rhs)
        elif ntype == "math_expression":
            left, right = _math_operands(node)
            _walk_operands(left)
            _walk_operands(right)
        elif ntype == "call_expression" or ntype == "subroutine_call":
            for arg in _call_args(node, source):
                _walk_operands(arg)
        elif ntype == "variable_declaration":
            for name_node in _decl_name_nodes(node):
                name_text = _ts.node_text(name_node, source)
                if not name_text:
                    continue
                sr, sc = name_node.start_point
                er, ec = name_node.end_point
                decls_by_name.setdefault(name_text.lower(), []).append(
                    (sr, sc, er, ec, name_text)
                )

    if not queried:
        return []

    out: list[Diagnostic] = []
    seen: set[tuple[str, int]] = set()
    for name_lc in queried:
        decls = decls_by_name.get(name_lc)
        if not decls:
            continue
        usage = first_use.get(name_lc)
        for sr, sc, er, ec, name_text in decls:
            key = (name_lc, sr)
            if key in seen:
                continue
            seen.add(key)
            use_hint = ""
            if usage is not None and usage[0] != sr:
                use_hint = f" (e.g. used at line {usage[0] + 1})"
            out.append(
                Diagnostic(
                    file=ctx.file,
                    start=Position(sr + 1, sc + 1),
                    end=Position(er + 1, ec + 1),
                    severity=Severity.WARNING,
                    code="U005",
                    message=(
                        f"{name_text!r} is used in a "
                        f"unit-checked expression but has no @unit{{}} "
                        f"annotation{use_hint}"
                    ),
                )
            )
    return out


def _decl_name_nodes(decl: Node) -> Iterator[Node]:
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


def _is_pure_numeric_constant(node: Node | None) -> bool:
    """Return True if ``node`` is a literal number or a constant
    expression composed entirely of literal numbers.

    Used to suppress H001 on initialisations like ``g = 9.81`` or
    ``omega = 2.0 * 3.14159 / 86400.0``: a unit-bearing variable
    being given a numeric default value is the standard Fortran
    idiom for declaring a physical constant, not a unit error. The
    literal IS the constant; treating it as dimensionless and firing
    H001 produces noise on every model-initialisation file.
    """
    if node is None:
        return False
    t = node.type
    if t == "number_literal" or t == "complex_literal" or t == "boz_literal":
        return True
    if t == "unary_expression" or t == "parenthesized_expression":
        for c in node.children:
            if c.type not in ("+", "-", "(", ")"):
                return _is_pure_numeric_constant(c)
        return False
    if t == "math_expression":
        return all(
            _is_pure_numeric_constant(c) for c in node.children
            if c.type not in ("+", "-", "*", "/", "**")
        )
    return False


def _emit_h001(loc: Node, lhs: UnitExpr, rhs: UnitExpr, ctx: _Ctx) -> Diagnostic:
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H001",
        message=(
            f"Assignment unit mismatch: "
            f"{format_unit(lhs)} ≠ {format_unit(rhs)}"
        ),
    )


def _emit_h002(loc: Node, left: UnitExpr, right: UnitExpr, ctx: _Ctx) -> Diagnostic:
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H002",
        message=(
            f"Operand unit mismatch in '+'/'-': "
            f"{format_unit(left)} ≠ {format_unit(right)} (D1.1)"
        ),
    )


# ---------------------------------------------------------------------------
# Polymorphism: H023 — signature contradicted by body
# ---------------------------------------------------------------------------
#
# A polymorphic function's signature quantifies one or more tyvars
# (``'a``, ``'b``, …); the spec promises that every body operation
# preserves polymorphism. When a body operation would *force* a tyvar
# to a concrete value — e.g. ``'a + kg`` (binds ``'a = kg``),
# ``'a + 'a^(-1)`` (binds ``'a = {1}``) — the signature is dishonest
# and H023 fires instead of the regular H001/H002.
#
# Detection: any failing combine() / power() / dimensionless-intrinsic
# site whose operands reference a tyvar from the enclosing function's
# signature is routed through H023 instead of the regular H001 / H002 /
# H003 / D1.4. The wrappers _emit_h001_or_h023 / _emit_h002_or_h023
# handle the dispatch; SIN-on-tyvar and non-literal-power-on-tyvar are
# wired inline at their respective emission sites.


def _active_free_tyvars(byte_offset: int, ctx: _Ctx) -> frozenset[str]:
    """Free tyvars of the routine enclosing ``byte_offset``.

    Returns an empty frozenset for module-level offsets and for
    non-polymorphic enclosing routines. The set is derived from
    ``ctx.signatures`` on demand — cheap, no caching needed at this
    scale (one set-build per emission site).
    """
    scope = ctx.scope_at(byte_offset)
    if scope is None:
        return frozenset()
    sig = ctx.signatures.get(scope)
    if sig is None:
        return frozenset()
    return free_tyvars_of_sig(sig)


def _tyvars_in_unit_expr(u: UnitExpr | None, active: frozenset[str]) -> frozenset[str]:
    """Subset of ``active`` that appears in ``u``. Empty if ``u`` is None."""
    if u is None or not active:
        return frozenset()
    inner = u
    while isinstance(inner, (LogWrap, ExpWrap)):
        inner = inner.inner
    if not isinstance(inner, Unit):
        return frozenset()
    return frozenset(name for name, _ in inner.tyvars if name in active)


def _h023_involved(
    lu: UnitExpr | None, ru: UnitExpr | None, active: frozenset[str],
) -> frozenset[str]:
    """Union of active tyvars present in either operand. Empty ⇒ no H023."""
    return _tyvars_in_unit_expr(lu, active) | _tyvars_in_unit_expr(ru, active)


def _emit_h023(
    loc: Node, lu: UnitExpr, ru: UnitExpr,
    involved: frozenset[str], ctx: _Ctx,
) -> Diagnostic:
    start, end = _node_span(loc)
    tyvars_str = ", ".join(sorted(involved))
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H023",
        message=(
            f"Polymorphic body forces a binding on type variable "
            f"{tyvars_str} — signature is not actually polymorphic: "
            f"{format_unit(lu)} ≠ {format_unit(ru)}"
        ),
    )


def _emit_h002_or_h023(
    loc: Node, lu: UnitExpr, ru: UnitExpr, ctx: _Ctx,
) -> Diagnostic:
    """H002, or H023 when ``lu``/``ru`` reference an enclosing-function
    free tyvar (the body would force a binding on it)."""
    active = _active_free_tyvars(loc.start_byte, ctx)
    involved = _h023_involved(lu, ru, active)
    if involved:
        return _emit_h023(loc, lu, ru, involved, ctx)
    return _emit_h002(loc, lu, ru, ctx)


def _emit_h001_or_h023(
    loc: Node, lhs: UnitExpr, rhs: UnitExpr, ctx: _Ctx,
) -> Diagnostic:
    """H001, or H023 when ``lhs``/``rhs`` reference an enclosing-function
    free tyvar."""
    active = _active_free_tyvars(loc.start_byte, ctx)
    involved = _h023_involved(lhs, rhs, active)
    if involved:
        return _emit_h023(loc, lhs, rhs, involved, ctx)
    return _emit_h001(loc, lhs, rhs, ctx)


def _emit_h020(
    loc: Node, func_name: str, conflict: Conflict, ctx: _Ctx,
) -> Diagnostic:
    """Polymorphic call-site unification failure (H020).

    Renders the symmetric ``(collides with arg N: name)`` trailer on
    every contributing row. Unification has no ordering, so every slot
    that pushed an inconsistent value is named — no "first arg wins"
    asymmetry.
    """
    start, end = _node_span(loc)
    contribs = conflict.contributions
    # Build the symmetric collision trailer for every row: the partners
    # are every *other* contributor whose implied value differs.
    rows: list[str] = []
    for c in contribs:
        partners = [
            p for p in contribs
            if p.slot_index != c.slot_index and p.implied != c.implied
        ]
        if partners:
            partner_label = ", ".join(
                _arg_label(p.slot_index, p.slot_name) for p in partners
            )
            trailer = f"  (collides with {partner_label})"
        else:
            trailer = ""
        label = _arg_label(c.slot_index, c.slot_name)
        rows.append(
            f"  {label}: {conflict.tyvar} = {format_unit(c.implied)}{trailer}"
        )
    body = "\n".join(rows)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H020",
        message=(
            f"Call to '{func_name}': type variable {conflict.tyvar} bound "
            f"to inconsistent units at this call site\n{body}"
        ),
    )


def _arg_label(index: int, name: str | None) -> str:
    """``arg 1 (x)`` / ``arg 1`` formatting shared by H020 rows."""
    return f"arg {index + 1} ({name})" if name else f"arg {index + 1}"


def _unit_expr_has_tyvars(u: UnitExpr | None) -> bool:
    """``True`` iff ``u`` references any tyvar (unwrap Log/Exp first)."""
    if u is None:
        return False
    inner: UnitExpr = u
    while isinstance(inner, (LogWrap, ExpWrap)):
        inner = inner.inner
    return isinstance(inner, Unit) and bool(inner.tyvars)


def _enclosing_derived_type_name(node: Node, source: bytes) -> str | None:
    """Walk up from ``node`` to the enclosing ``derived_type_definition``
    and return the type's declared name. Returns ``None`` when ``node``
    is not inside a derived-type definition.

    Used by H021 to (a) detect derived-type position and (b) reach
    ``field_units`` for the unit lookup — derived-type fields are keyed
    by ``(type_name, field_name)``, not by name alone.
    """
    cur = node.parent
    while cur is not None:
        if cur.type == "derived_type_definition":
            for ch in cur.children:
                if ch.type == "derived_type_statement":
                    for cc in ch.children:
                        if cc.type == "type_name":
                            return _text(cc, source)
            return None
        cur = cur.parent
    return None


def _decl_is_parameter(node: Node, source: bytes) -> bool:
    """``True`` iff ``node`` is a ``variable_declaration`` carrying a
    ``PARAMETER`` type qualifier. Mirrors the detection in
    :func:`_extract_parameter_values_from_decl`."""
    for c in node.children:
        if c.type == "type_qualifier" and _text(c, source).strip().lower() == "parameter":
            return True
    return False


def _decl_is_save(node: Node, source: bytes) -> bool:
    """``True`` iff ``node`` is a ``variable_declaration`` carrying a
    ``SAVE`` type qualifier — ``real, save :: cache``. The standalone
    ``save :: x, y`` statement form is handled by
    :func:`_collect_save_attribute_names` below.
    """
    for c in node.children:
        if c.type == "type_qualifier" and _text(c, source).strip().lower() == "save":
            return True
    return False


def _collect_common_names(tree: Tree, source: bytes) -> set[tuple[str | None, str]]:
    """Per-routine set of ``(scope_lc, name_lc)`` for every variable
    that appears in a ``common`` statement. Block names (the ``/blk/``
    part) parse as ``name`` nodes, not ``identifier``, so a plain
    descendant-walk for ``identifier`` collects the variable names
    cleanly without false positives.
    """
    out: set[tuple[str | None, str]] = set()
    # scope_at would normally need a _Ctx; replicate the bisect
    # behaviour inline with a routine_scopes view built here. Cheaper:
    # use a stand-alone walk that tracks the enclosing routine via
    # the AST hierarchy directly.
    for n in _ts.walk(tree.root_node):
        if n.type != "common_statement":
            continue
        scope = _enclosing_routine_name(n, source)
        for d in _ts.walk(n):
            if d.type == "identifier":
                out.add((scope, _text(d, source).lower()))
    return out


def _collect_save_attribute_names(
    tree: Tree, source: bytes,
) -> set[tuple[str | None, str]]:
    """Per-routine set of ``(scope_lc, name_lc)`` for variables named in
    a standalone ``save :: x, y`` statement (distinct from the
    ``real, save :: x`` inline form, which is detected via
    :func:`_decl_is_save` on the variable_declaration itself).
    """
    out: set[tuple[str | None, str]] = set()
    for n in _ts.walk(tree.root_node):
        if n.type != "save_statement":
            continue
        scope = _enclosing_routine_name(n, source)
        for d in _ts.walk(n):
            if d.type == "identifier":
                out.add((scope, _text(d, source).lower()))
    return out


def _enclosing_routine_name(node: Node, source: bytes) -> str | None:
    """Walk up to the enclosing SUBROUTINE/FUNCTION and return its
    lower-cased name. Returns ``None`` for module-level / file-level
    positions. Independent of ``_Ctx`` so it can be used during the
    pre-pass that builds the COMMON/SAVE name sets.
    """
    cur = node.parent
    while cur is not None:
        if cur.type in ("subroutine", "function"):
            for c in cur.children:
                if c.type in (
                    "subroutine_statement", "function_statement",
                ):
                    for cc in c.children:
                        if cc.type == "name":
                            return _text(cc, source).lower()
            return None
        cur = cur.parent
    return None


def _emit_h021_tyvar_positions(
    tree: Tree, ctx: _Ctx, source: bytes,
) -> Iterable[Diagnostic]:
    """Fire H021 for every ``@unit{'a}`` attached to a forbidden position.

    Allowed: dummy args, result variables, ordinary locals of a
    SUBROUTINE/FUNCTION body. Forbidden:

    - Module-level / file-level variables (``ctx.scope_at`` returns
      ``None`` at the declaration site).
    - ``PARAMETER``-qualified declarations anywhere.
    - Components of a derived-type definition.

    SAVE'd locals (both the ``real, save :: x`` inline form and the
    standalone ``save :: x, y`` statement) and ``COMMON`` block members
    are forbidden too.
    """
    common_names = _collect_common_names(tree, source)
    save_names = _collect_save_attribute_names(tree, source)
    for n in _ts.walk(tree.root_node):
        if n.type != "variable_declaration":
            continue
        # Derived-type field unit lookups go through ``field_units`` (keyed
        # by ``(type_name, field_name)``), not through the scoped
        # ``unit_for`` map. Determine the enclosing type up front so the
        # lookup branches correctly — without this, every derived-type
        # tyvar would silently sail past the ``_unit_expr_has_tyvars``
        # gate because ``unit_for`` returns None for type components.
        enclosing_type = _enclosing_derived_type_name(n, source)
        for name_node in _decl_name_nodes(n):
            name_text = _ts.node_text(name_node, source)
            if not name_text:
                continue
            byte_offset = name_node.start_byte
            if enclosing_type is not None:
                unit = ctx._field_units_lc.get(
                    (enclosing_type.lower(), name_text.lower())
                )
            else:
                unit = ctx.unit_for(name_text, byte_offset)
            if not _unit_expr_has_tyvars(unit):
                continue

            reason: str | None = None
            if enclosing_type is not None:
                reason = "derived-type component"
            elif _decl_is_parameter(n, source):
                reason = "PARAMETER declaration"
            elif _decl_is_save(n, source):
                reason = "SAVE'd local"
            else:
                # The standalone ``save :: x`` form and COMMON-block
                # membership are scope-name keyed; consult the pre-pass
                # sets once we know which scope this declaration is in.
                key = (ctx.scope_at(byte_offset), name_text.lower())
                if key in save_names:
                    reason = "SAVE'd local"
                elif key in common_names:
                    reason = "COMMON block member"
                elif ctx.scope_at(byte_offset) is None:
                    reason = "module-level variable"
            if reason is None:
                continue

            start, end = _node_span(name_node)
            yield Diagnostic(
                file=ctx.file, start=start, end=end,
                severity=Severity.ERROR, code="H021",
                message=(
                    f"Type variable in @unit{{...}} cannot appear in a "
                    f"{reason}; only function signatures and their body "
                    f"locals may quantify over 'a."
                ),
            )


def _emit_h022(
    loc: Node, func_name: str, arg_index: int,
    expected: UnitExpr, actual: UnitExpr, ctx: _Ctx,
    arg_name: str | None = None,
) -> Diagnostic:
    """Affine actual into a tyvar slot — type variables range over the
    multiplicative unit algebra only; affine units inhabit a separate
    layer that cannot bind ``'a``.
    """
    start, end = _node_span(loc)
    label = _arg_label(arg_index, arg_name)
    # Name the specific tyvar(s) the call would have bound — matches
    # the spec example "cannot bind 'a to affine unit degC" rather
    # than the generic "type variable".
    inner = expected
    while isinstance(inner, (LogWrap, ExpWrap)):
        inner = inner.inner
    tyvars_str = (
        ", ".join(name for name, _ in inner.tyvars)
        if isinstance(inner, Unit) and inner.tyvars
        else "type variable"
    )
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H022",
        message=(
            f"Call to '{func_name}': cannot bind {tyvars_str} to affine "
            f"unit {format_unit(actual)} at {label}; convert to "
            f"{format_unit(actual, show_offset=False)} at the call site, "
            f"or pass as a delta."
        ),
    )


def _emit_h010(
    loc: Node, literal_text: str, target_unit: UnitExpr, ctx: _Ctx
) -> Diagnostic:
    """Implicit-literal-cast warning (D1.5).

    Fires when ``+``/``-`` mixes a dim'less numeric literal with a
    unitful operand. The literal is auto-cast to the target unit; the
    expression types successfully. The warning surfaces the smell and
    suggests promoting the literal to a named PARAMETER.
    """
    start, end = _node_span(loc)
    target = format_unit(target_unit)
    # The PARAMETER example must be valid @unit{} syntax, so use the source
    # serializer (ASCII ``*``/``^``/``/`` — the pretty ``·``/superscript form
    # does not round-trip through parse). It also drops the affine offset
    # ("K + 273.15" is a description, not a parseable unit), which gives the
    # better hint anyway — a literal added to an absolute temperature is a
    # difference, so @unit{K} is the right type.
    target_annot = format_unit_source(target_unit)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.WARNING, code="H010",
        message=(
            f"Implicit cast: literal {literal_text!r} to {target} "
            f"(prefer a named PARAMETER, e.g. "
            f"`REAL, PARAMETER :: <name> = {literal_text}   "
            f"!< @unit{{{target_annot}}}`)"
        ),
    )


def _emit_s001(
    loc: Node,
    left: UnitExpr,
    right: UnitExpr,
    ratio: Fraction | None,
    ctx: _Ctx,
) -> Diagnostic:
    """Scale mismatch (S001): same dimension, different magnitude factor.

    Opt-in — emitted only when ``ctx.scale_mode`` is on. Warning severity,
    overridable via ``[diagnostics] S001``. A *missing* conversion is a
    real bug; a *correct-but-untyped* conversion is fixed by carrying the
    factor on a typed PARAMETER (e.g. ``100. !< @unit{Pa/hPa}``). See
    docs/design/scale.md.
    """
    start, end = _node_span(loc)
    # Both sides share a dimension, so format_unit renders them identically
    # (it normalises prefixes: km → m). Lead with the magnitude ratio — that
    # is the actual discrepancy — rather than a confusing "m vs m".
    ratio_txt = f"×{ratio}" if ratio is not None else "an unknown factor"
    return Diagnostic(
        file=ctx.file,
        start=start,
        end=end,
        severity=Severity.WARNING,
        code="S001",
        message=(
            f"Scale mismatch: same dimension ({format_unit(left)}) but the "
            f"magnitudes differ by {ratio_txt}. If this is a unit conversion, "
            f"carry the factor on a typed PARAMETER; otherwise the units "
            f"disagree in scale."
        ),
    )


def _scale_mismatch_ratio(a: UnitExpr, b: UnitExpr) -> Fraction | None:
    """Return the factor ratio if ``a``/``b`` are dim-equal but scale-differ,
    else ``None``. Thin wrapper over :func:`compare` for the emit sites."""
    v = compare(a, b)
    return v.ratio if v.kind == "scale_mismatch" else None


def _emit_s002(loc: Node, reason: str, ctx: _Ctx) -> Diagnostic:
    """Affine offset violation (S002, Phase 2).

    Opt-in (``ctx.scale_mode``), warning severity, overridable via
    ``[diagnostics] S002``. Covers both detection paths: a boundary
    ``offset_mismatch`` (``K = degC``) and an operation ill-defined on an
    absolute temperature (``degC + degC``, ``2 * degC``). ``reason`` is the
    pre-formatted specifics. See docs/design/scale.md §3.3, §5.
    """
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file,
        start=start,
        end=end,
        severity=Severity.WARNING,
        code="S002",
        message=f"Offset mismatch: {reason}",
    )


def _offset_mismatch_delta(a: UnitExpr, b: UnitExpr) -> Fraction | None:
    """Return the offset delta if ``a``/``b`` are dim+factor-equal but
    differ in zero-point (S002 path 1, boundary), else ``None``."""
    v = compare(a, b)
    return v.delta if v.kind == "offset_mismatch" else None


def _affine_violation(op: str, lu: UnitExpr, ru: UnitExpr) -> str | None:
    """Reason string if ``lu <op> ru`` is an affine-invalid operation
    (S002 path 2), else ``None``. Encodes the §3.3 algebra: an absolute
    operand (``offset != 0``) cannot be multiplied/divided/raised; two
    absolutes cannot be added; a difference minus an absolute (or
    absolutes in different zero-points) is ill-defined. Offsets only live
    on Regular ``Unit`` leaves, so wrappers are out of scope (return None).
    """
    if not isinstance(lu, Unit) or not isinstance(ru, Unit):
        return None
    lo, ro = lu.offset, ru.offset
    if lo == 0 and ro == 0:
        return None  # ordinary operands — Phase-1 territory, nothing affine
    if op in ("*", "/"):
        return ("an absolute temperature (offset unit) cannot be scaled, "
                "multiplied, or divided — use a temperature difference")
    if op == "+":
        if lo != 0 and ro != 0:
            return ("cannot add two absolute temperatures — one operand "
                    "must be a difference (offset-0)")
        return None  # point + vector — legal
    if op == "-":
        if ro != 0 and lo != ro:
            return ("ill-defined subtraction: a difference minus an "
                    "absolute temperature, or absolutes in different "
                    "zero-points")
        return None  # point - vector, or point - point (equal) -> difference
    return None


def _emit_s003(loc: Node, reason: str, ctx: _Ctx) -> Diagnostic:
    """Invalid ``@unit_affine_conversion`` directive (S003, Phase 2c).

    Unlike S001/S002 (warnings, style nudges) this defaults to **error**: a
    *claimed* conversion that the arithmetic doesn't actually perform is a
    bug, not a smell. Overridable via ``[diagnostics] S003``. A *valid*
    directive emits nothing (and suppresses the S002 the statement would
    otherwise raise). See docs/design/scale.md §11.5.
    """
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file,
        start=start,
        end=end,
        severity=Severity.ERROR,
        code="S003",
        message=f"Affine-conversion directive does not verify: {reason}",
    )


def _lin_reduce(
    node: Node | None, src: Unit, ctx: _Ctx, source: bytes,
) -> tuple[Fraction, Fraction, int] | None:
    """Reduce an expression to its affine-linear form ``a*s + b`` in the
    single source operand ``s`` (a quantity of unit ``src``).

    Returns ``(a, b, n_src)`` where ``a``/``b`` are exact ``Fraction``
    coefficients and ``n_src`` is the number of ``src``-typed operands seen
    (the caller requires exactly one). Returns ``None`` when the expression
    is not affine-linear in ``s`` with constant coefficients (a non-source
    variable with no resolvable value, a non-linear product, division by a
    non-constant, an unhandled node shape). See scale.md §11.4(c).

    Resolution order at a leaf matters: a ``src``-typed operand is the
    source (``a=1``) even though it may also be a PARAMETER with a value;
    only a *non*-source node is folded to a constant ``b``.
    """
    if node is None:
        return None
    node = _unwrap_parens(node)
    # Structural nodes recurse FIRST — only a *leaf* operand can be the
    # source `s` or a constant. (A compound RHS like ``t_c + RTT`` resolves
    # to ``degC``, which would spuriously match the source-leaf check; the
    # source is a single variable, never a whole sub-expression.)
    if node.type == "unary_expression":
        sign = 1
        for c in node.children:
            if c.type == "-":
                sign = -sign
                break
            if c.type == "+":
                break
        inner = _lin_reduce(_unary_operand(node), src, ctx, source)
        if inner is None:
            return None
        a, b, n = inner
        return (sign * a, sign * b, n)
    if node.type == "math_expression":
        op = _math_op(node)
        if op not in ("+", "-", "*", "/"):
            return None
        left, right = _math_operands(node)
        lr = _lin_reduce(left, src, ctx, source)
        rr = _lin_reduce(right, src, ctx, source)
        if lr is None or rr is None:
            return None
        la, lb, ln = lr
        ra, rb, rn = rr
        if op == "+":
            return (la + ra, lb + rb, ln + rn)
        if op == "-":
            return (la - ra, lb - rb, ln + rn)
        if op == "*":
            if la == 0:               # const(lb) * linear
                return (ra * lb, rb * lb, rn)
            if ra == 0:               # linear * const(rb)
                return (la * rb, lb * rb, ln)
            return None               # s * s → non-linear
        # op == "/"
        if ra == 0 and rb != 0:       # linear / const(rb)
            return (la / rb, lb / rb, ln)
        return None                   # division by the source / by zero
    # Leaf. A constant term comes first: a literal or PARAMETER with a
    # foldable value is the offset/factor constant — even when it is typed
    # like the source frame (``RTT`` is ``K`` in a ``K -> degC`` conversion,
    # yet it is the 273.15 *constant*, not a second source operand). Its
    # unit is irrelevant; only the numeric value matters (§11.4(c)).
    val = _resolve_constant_value(node, ctx, source)
    if val is not None:
        return (Fraction(0), Fraction(val), 0)
    # Otherwise: the source operand is the runtime quantity typed ``src``.
    u = _resolve(node, ctx, source)
    if isinstance(u, Unit) and compare(u, src).kind == "equal":
        return (Fraction(1), Fraction(0), 1)
    return None


def _verify_affine_conversion(
    node: Node,
    target: Node | None,
    value: Node | None,
    src_text: str,
    tgt_text: str,
    ctx: _Ctx,
    source: bytes,
) -> Diagnostic | None:
    """Verify an ``@unit_affine_conversion{src -> tgt}`` directive on an
    assignment. Returns an ``S003`` diagnostic if invalid, else ``None``
    (valid → the caller suppresses the statement's S002). See scale.md §11.
    """
    # (a) Resolve the directive's units; both must exist and share dimension.
    try:
        s_unit = _units_mod.parse(src_text, ctx.table)
        t_unit = _units_mod.parse(tgt_text, ctx.table)
    except UnitError as exc:
        return _emit_s003(node, f"unknown unit in directive ({exc})", ctx)
    if not isinstance(s_unit, Unit) or not isinstance(t_unit, Unit):
        return _emit_s003(
            node, "affine conversion units must be simple (no LOG/EXP)", ctx
        )
    if tuple(s_unit.dimension) != tuple(t_unit.dimension):
        return _emit_s003(
            node,
            f"{src_text} and {tgt_text} are not affine-compatible "
            f"(different dimensions)",
            ctx,
        )
    # An affine conversion needs a real zero-point shift; a pure-factor pair
    # (both offset 0, e.g. Pa -> hPa) is multiplicative — reject it.
    if s_unit.offset == 0 and t_unit.offset == 0:
        return _emit_s003(
            node,
            f"{src_text} -> {tgt_text} is a multiplicative (offset-0) "
            f"conversion — carry the factor on a typed PARAMETER instead",
            ctx,
        )
    # The unique affine conversion src→tgt: x_t = a*·x_s + b*.
    a_star = s_unit.factor / t_unit.factor
    b_star = (s_unit.offset - t_unit.offset) / t_unit.factor
    # (1) The LHS unit, when declared, must be the target frame.
    tu = _resolve(target, ctx, source) if target is not None else None
    if isinstance(tu, Unit) and compare(tu, t_unit).kind != "equal":
        return _emit_s003(
            node,
            f"target mismatch: the assignment target is {format_unit(tu)} "
            f"but the directive declares target {tgt_text}",
            ctx,
        )
    # (b)+(c) Reduce the RHS to a*s + b with exactly one src operand.
    reduced = _lin_reduce(value, s_unit, ctx, source)
    if reduced is None:
        return _emit_s003(
            node,
            f"the right-hand side is not affine-linear in a single "
            f"{src_text} operand with constant coefficients",
            ctx,
        )
    a, b, n_src = reduced
    if n_src != 1:
        return _emit_s003(
            node,
            f"need exactly one {src_text} operand on the right-hand side "
            f"(found {n_src})",
            ctx,
        )
    # (d) The arithmetic must match the unique conversion law exactly.
    if a != a_star or b != b_star:
        return _emit_s003(
            node,
            f"the {src_text} -> {tgt_text} arithmetic is wrong: the RHS "
            f"computes a*s+b with a={_fmt_frac(a)}, b={_fmt_frac(b)}, but "
            f"the conversion requires a={_fmt_frac(a_star)}, "
            f"b={_fmt_frac(b_star)}",
            ctx,
        )
    return None


def _fmt_frac(x: Fraction) -> str:
    """Render a Fraction as a compact decimal when exact, else as a ratio."""
    if x.denominator == 1:
        return str(x.numerator)
    dec = float(x)
    return f"{dec:g}" if Fraction(dec).limit_denominator(10**6) == x else str(x)


def _is_dimensionless(u: UnitExpr) -> bool:
    """Return True if ``u`` is the dim'less unit (all base exponents zero).

    LOG/EXP wrappers carry no SI dimension, so they count as dimensionless.
    """
    if not isinstance(u, Unit):
        return True
    return all(d == 0 for d in u.dimension)


def _emit_d12(loc: Node, left: UnitExpr, right: UnitExpr, op: str, ctx: _Ctx) -> Diagnostic:
    """D1.2 — undefined wrapper operation (e.g. LOG(p) × LOG(q))."""
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H002",
        message=(
            f"Undefined unit operation '{op}': "
            f"{format_unit(left)} {op} {format_unit(right)} "
            f"has no closed-form unit (D1.2)"
        ),
    )


def _emit_d13(loc: Node, left: UnitExpr, right: UnitExpr, op: str, ctx: _Ctx) -> Diagnostic:
    """D1.3 — undefined sum involving a wrapper (e.g. LOG(p) + Pa)."""
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H002",
        message=(
            f"Undefined unit sum '{op}': "
            f"{format_unit(left)} {op} {format_unit(right)} (D1.3)"
        ),
    )


def _emit_d14(loc: Node, ctx: _Ctx, *, detail: str) -> Diagnostic:
    """D1.4 — unit depends on a runtime-only quantity (non-literal exponent
    or non-literal scalar on a LogWrap)."""
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H001",
        message=f"Runtime-dependent unit: {detail} (D1.4)",
    )


def _emit_d17(
    loc: Node, base: UnitExpr, exp_unit: UnitExpr | None, ctx: _Ctx
) -> Diagnostic:
    """D1.7 — exponent must be dimensionless (default WARNING).

    Fires when an expression of the form ``base ^ exponent`` has an
    exponent whose unit is non-dim'less. The wrapper algebra would
    formally type this as ``ExpWrap(exp_unit)`` via the ``a^b =
    exp(b·log(a))`` derivation, but in practice such expressions are
    virtually always bugs in scientific Fortran code (``2.0 ** speed``
    style typos). Default severity is WARNING so the rare intentional
    case ("I really want exp-tagged space") isn't blocking; projects
    can promote to ERROR or suppress entirely via the
    ``[diagnostics]`` section of ``.dimfort.toml``.
    """
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.WARNING, code="H010",
        message=(
            f"Exponent must be dimensionless: "
            f"{format_unit(base)} ** {format_unit(exp_unit) if exp_unit is not None else '?'} "
            f"— ``{format_unit(exp_unit) if exp_unit is not None else '?'}`` is not dim'less. "
            f"If you genuinely intend an exp-tagged result, write "
            f"``EXP(b * LOG(a))`` explicitly (D1.7)"
        ),
    )


def _emit_d16_untag(
    loc: Node, lhs: UnitExpr, rhs: UnitExpr, ctx: _Ctx
) -> Diagnostic:
    """D1.6 — implicit wrapper untag at assignment (H010 warning).

    Fires when the LHS is a Regular unit and the RHS is a ``LogWrap`` /
    ``ExpWrap`` whose inner unit dimensionally matches the LHS. The
    assignment "untags" the wrapper — semantically OK because log/exp
    of a unitful quantity is just a numerical value with the same
    dimensions, but flagged so the user can decide whether the untag
    was intentional.
    """
    start, end = _node_span(loc)
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.WARNING, code="H010",
        message=(
            f"Implicit wrapper untag: {format_unit(rhs)} assigned to "
            f"{format_unit(lhs)} — if intentional, annotate the LHS "
            f"as @unit{{{format_unit_source(rhs)}}} "
            f"to silence this warning (D1.6)"
        ),
    )


def _emit_h003(loc: Node, intrinsic: str, arg_unit: UnitExpr, ctx: _Ctx) -> Diagnostic:
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
    loc: Node, func: str, arg_index: int, expected: UnitExpr, actual: UnitExpr,
    ctx: _Ctx, arg_name: str | None = None,
) -> Diagnostic:
    start, end = _node_span(loc)
    # Include the formal parameter's name when available so the reader
    # doesn't have to count argument positions to find the offending
    # variable. Index is kept too because formals can share names
    # across overloads or appear multiply via INTENT(INOUT) — counting
    # by position is still the unambiguous reference.
    arg_label = (
        f"argument {arg_index + 1} ({arg_name})"
        if arg_name else f"argument {arg_index + 1}"
    )
    return Diagnostic(
        file=ctx.file, start=start, end=end,
        severity=Severity.ERROR, code="H004",
        message=(
            f"Call to '{func}': {arg_label} unit mismatch: "
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
        op = _math_op(node)
        if op == "**":
            base = _resolve(left, ctx, source)
            if base is None or right is None:
                return
            exponent_value: int | Fraction | Exponent | None = (
                _resolve_constant_value(right, ctx, source)
            )
            exponent_unit = _resolve(right, ctx, source)
            # Mirror _resolve: try symbolic exponent when literal-rational fails.
            if exponent_value is None:
                exponent_value = _resolve_symbolic_exponent(
                    right, ctx, source,
                )
            _, diag = power(base, exponent_unit, exponent_value)
            if diag == "D1.4":
                # Polymorphism: a non-literal exponent on a tyvar-typed
                # base would bind `'a = {1}` (spec table row:
                # ``'a^x, x non-literal — binds 'a = {1}, D1.4 unchanged``
                # — under polymorphism the bind is what makes the body
                # dishonest, so we fire H023 instead of D1.4).
                active = _active_free_tyvars(node.start_byte, ctx)
                involved = _tyvars_in_unit_expr(base, active)
                if involved:
                    one = _units_mod.parse("1", ctx.table)
                    yield _emit_h023(node, base, one, involved, ctx)
                else:
                    yield _emit_d14(
                        node, ctx,
                        detail=(
                            f"power exponent is not a literal rational "
                            f"(base unit: {format_unit(base)})"
                        ),
                    )
            elif diag == "D1.2":
                yield _emit_d12(node, base, base, "**", ctx)
            elif diag == "D1.7":
                yield _emit_d17(node, base, exponent_unit, ctx)
            # Affine (S002 path 2): can't raise an absolute temperature to
            # a power. base is a UnitExpr; offsets only on Regular Units.
            if (ctx.scale_mode and isinstance(base, Unit)
                    and base.offset != 0):
                yield _emit_s002(
                    node,
                    "an absolute temperature (offset unit) cannot be "
                    "raised to a power — use a temperature difference",
                    ctx,
                )
            return
        if op not in ("+", "-", "*", "/"):
            return
        lu = _resolve(left, ctx, source)
        ru = _resolve(right, ctx, source)
        if lu is None or ru is None:
            return
        # Scale layer (opt-in): +/- operands of the same dimension but
        # different magnitude factor → S001. Mutually exclusive with the
        # H002 dim-mismatch below (compare() gives scale_mismatch only when
        # dims agree). */ propagate scale, so only +/- are check sites.
        if ctx.scale_mode and op in ("+", "-"):
            ratio = _scale_mismatch_ratio(lu, ru)
            if ratio is not None:
                yield _emit_s001(node, lu, ru, ratio, ctx)
        # Affine offset (S002 path 2): an operation ill-defined on an
        # absolute temperature (degC+degC, 2*degC, …). compare() can't see
        # these (the operands' offsets may be equal), so check the op rules
        # directly. Mutually exclusive with S001 (factor) by construction.
        if ctx.scale_mode:
            affine_reason = _affine_violation(op, lu, ru)
            if affine_reason is not None:
                yield _emit_s002(node, affine_reason, ctx)
        left_lit_val: int | Fraction | Exponent | None = (
            _resolve_constant_value(left, ctx, source) if left is not None else None
        )
        right_lit_val: int | Fraction | Exponent | None = (
            _resolve_constant_value(right, ctx, source) if right is not None else None
        )
        # Symbolic fallback (same as in _resolve): lets R5.4 accept
        # symbolic multipliers instead of firing D1.4 via R5.5.
        if left_lit_val is None and left is not None:
            left_lit_val = _resolve_symbolic_exponent(left, ctx, source)
        if right_lit_val is None and right is not None:
            right_lit_val = _resolve_symbolic_exponent(right, ctx, source)
        _, diag = combine(
            op, lu, ru,
            a_literal=left_lit_val, b_literal=right_lit_val,
        )
        if diag is None:
            return
        if diag == "D1.1":
            yield _emit_h002_or_h023(node, lu, ru, ctx)
        elif diag == "D1.5":
            # H010 implicit-literal-cast warning. The non-literal side
            # is the target unit (combine() returned it as ``result``).
            # Locate the literal operand to anchor the diagnostic span.
            if left_lit_val is not None and left is not None:
                target = ru
                yield _emit_h010(left, _text(left, source), target, ctx)
            elif right is not None:
                target = lu
                yield _emit_h010(right, _text(right, source), target, ctx)
        elif diag == "D1.2":
            yield _emit_d12(node, lu, ru, op, ctx)
        elif diag == "D1.3":
            yield _emit_d13(node, lu, ru, op, ctx)
        elif diag == "D1.4":
            yield _emit_d14(
                node, ctx,
                detail=(
                    f"scalar multiplier of {format_unit(lu)} / "
                    f"{format_unit(ru)} is not a literal rational"
                ),
            )
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
            # Polymorphism: if the arg references a free tyvar from the
            # enclosing function, the intrinsic's "must be dimensionless"
            # rule would force `'a = {1}` — the body breaks the
            # signature's polymorphic promise. Fire H023 (spec table
            # row: SIN/COS/TAN/ASIN/ATAN/... bind `'a = {1}`) instead
            # of the regular H003.
            active = _active_free_tyvars(node.start_byte, ctx)
            involved = _tyvars_in_unit_expr(u, active)
            if involved:
                yield _emit_h023(node, u, one, involved, ctx)
            else:
                yield _emit_h003(node, name_lc, u, ctx)
        return

    if name_lc in SAME_UNIT_ARG_INTRINSICS:
        yield from _check_same_unit_args(arg_exprs, ctx, source)
        return

    sig = ctx.signatures.get(name_lc)
    if sig is None or sig.is_subroutine:
        return
    yield from _check_call_args_against_sig(
        sig, name_lc, arg_exprs, node, ctx, source
    )


def _check_same_unit_args(
    arg_exprs: list[Node], ctx: _Ctx, source: bytes
) -> Iterable[Diagnostic]:
    """Validate MAX/MIN-style intrinsics whose args must share a unit.

    Mirrors the ``+``/``-`` operand rules: a dimensionless numeric literal
    is auto-cast to the carrying (dimensioned) unit and warns via H010
    (unless its value is 0, which is dimension-agnostic and silent); a
    genuinely dimensioned operand that disagrees with the carrying unit
    fires H002.
    """
    if len(arg_exprs) < 2:
        return
    resolved = [
        (a, _resolve(a, ctx, source), _resolve_constant_value(a, ctx, source))
        for a in arg_exprs
    ]
    carry: UnitExpr | None = None
    for _a, u, _lit in resolved:
        if isinstance(u, Unit) and not _is_dimensionless(u):
            carry = u
            break
    if carry is None:
        return  # all dimensionless / unresolved: nothing to enforce
    for a, u, lit in resolved:
        if u is None:
            continue
        if isinstance(u, Unit) and not _is_dimensionless(u):
            if not equal_dim(u, carry):
                yield _emit_h002_or_h023(a, u, carry, ctx)
        elif _is_number_literal_node(a):
            if lit == 0:
                continue  # literal 0 is dimension-agnostic
            yield _emit_h010(a, _text(a, source), carry, ctx)
        else:
            # a genuinely dimensionless non-literal operand vs a
            # dimensioned carrying unit is a real mismatch
            yield _emit_h002_or_h023(a, u, carry, ctx)


def _check_call_args_against_sig(
    sig: FuncSig,
    func_name: str,
    arg_exprs: list[Node],
    call_node: Node,
    ctx: _Ctx,
    source: bytes,
) -> Iterable[Diagnostic]:
    # ``strict=False``: it's normal for the call site to pass fewer
    # arguments than the signature declares (Fortran allows trailing
    # optional args). We check whichever pairs we can match.
    free_tyvars = free_tyvars_of_sig(sig)

    if not free_tyvars:
        # Concrete signature — per-slot dim check, identical to the
        # pre-polymorphism behaviour.
        for i, (expected, actual_node) in enumerate(
            zip(sig.arg_units, arg_exprs, strict=False)
        ):
            if expected is None or actual_node is None:
                continue
            actual = _resolve(actual_node, ctx, source)
            if actual is None:
                continue
            if not equal_dim(actual, expected):
                arg_name = sig.arg_names[i] if i < len(sig.arg_names) else None
                yield _emit_h004(
                    call_node, func_name, i, expected, actual, ctx,
                    arg_name=arg_name,
                )
        return

    # Polymorphic signature — split slots and dispatch:
    #   - slots whose formal references a free tyvar go into the unifier
    #   - slots whose formal is concrete keep the per-slot H004 check
    # This keeps concrete-slot mismatches as H004 (the existing UX) and
    # routes only the genuine polymorphism failures through H020.
    poly_equations: list[SlotEquation] = []
    concrete_misses: list[tuple[int, UnitExpr, UnitExpr, str | None]] = []
    affine_blocks: list[tuple[int, UnitExpr, UnitExpr, str | None]] = []
    for i, (expected, actual_node) in enumerate(
        zip(sig.arg_units, arg_exprs, strict=False)
    ):
        if expected is None or actual_node is None:
            continue
        actual = _resolve(actual_node, ctx, source)
        if actual is None:
            continue
        arg_name = sig.arg_names[i] if i < len(sig.arg_names) else None
        if _tyvars_in_unit_expr(expected, free_tyvars):
            # Affine actual into a tyvar slot — type variables range over
            # the multiplicative algebra only, so an absolute affine
            # quantity (offset != 0, e.g. degC) cannot bind one. Fire
            # H022 instead of running unification.
            if isinstance(actual, Unit) and actual.offset != 0:
                affine_blocks.append((i, expected, actual, arg_name))
                continue
            # The unifier supports Unit-vs-Unit only; wrapper-typed
            # polymorphic slots fall through to the concrete check
            # below (UnsupportedPolymorphism otherwise — see
            # polymorphism.py).
            if isinstance(expected, Unit) and isinstance(actual, Unit):
                poly_equations.append(SlotEquation(
                    slot_index=i, slot_name=arg_name,
                    formal=expected, actual=actual,
                ))
            else:
                # Wrapper-typed polymorphic slot — Phase 2. Fall back to
                # the concrete dim check so the user still gets *some*
                # signal at this site.
                if not equal_dim(actual, expected):
                    concrete_misses.append((i, expected, actual, arg_name))
        else:
            if not equal_dim(actual, expected):
                concrete_misses.append((i, expected, actual, arg_name))

    # H022 for any tyvar slot receiving an affine actual.
    for i, expected, actual, arg_name in affine_blocks:
        yield _emit_h022(
            call_node, func_name, i, expected, actual, ctx, arg_name=arg_name,
        )

    # Emit H004 for any concrete-slot misses regardless of polymorphism
    # outcome.
    for i, expected, actual, arg_name in concrete_misses:
        yield _emit_h004(
            call_node, func_name, i, expected, actual, ctx, arg_name=arg_name,
        )

    # Run unification on the polymorphic slots.
    if not poly_equations:
        return
    try:
        result = unify(poly_equations, tuple(sorted(free_tyvars)))
    except UnsupportedPolymorphism:
        # Symbolic tyvar exponent or other Phase 2 shape — fall back to
        # per-slot dim check on the polymorphic slots.
        for eq in poly_equations:
            if not equal_dim(eq.actual, eq.formal):
                yield _emit_h004(
                    call_node, func_name, eq.slot_index, eq.formal,
                    eq.actual, ctx, arg_name=eq.slot_name,
                )
        return
    if not result.ok:
        for conflict in result.conflicts:
            yield _emit_h020(call_node, func_name, conflict, ctx)


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


def _extract_parameter_values_from_decl(
    n: Node, source: bytes, out: dict[str, Fraction | int],
) -> None:
    """If ``n`` is a PARAMETER ``variable_declaration``, populate ``out``."""
    is_parameter = False
    for c in n.children:
        if c.type == "type_qualifier" and _text(c, source).strip().lower() == "parameter":
            is_parameter = True
            break
    if not is_parameter:
        return
    for c in n.children:
        if c.type != "init_declarator":
            continue
        name_node = next(
            (cc for cc in c.children if cc.type == "identifier"), None,
        )
        if name_node is None:
            continue
        value_node = None
        seen_eq = False
        for cc in c.children:
            if cc.type == "=":
                seen_eq = True
                continue
            if seen_eq:
                value_node = cc
                break
        if value_node is None:
            continue
        value = _resolve_constant_value(value_node, None, source)
        if value is None:
            continue
        out[_text(name_node, source).lower()] = value


def collect_parameter_values(
    tree: Tree, source: bytes,
) -> dict[str, Fraction | int]:
    """Return ``{name_lc: value}`` for every PARAMETER declaration whose
    initialiser collapses to a rational.

    A PARAMETER declaration is a ``variable_declaration`` with a
    ``type_qualifier`` child reading "parameter". Each ``init_declarator``
    pairs an identifier with a value expression; we evaluate the value
    expression via ``_resolve_constant_value`` (without a ctx — so only
    literals and simple arithmetic, no chained PARAMETER lookup here).
    A two-pass version that resolves chained PARAMETERs is straightforward
    to add when needed; for now the common ``kappa = 2./7.`` style is
    literal-only and that's what this covers.
    """
    out: dict[str, Fraction | int] = {}
    for n in _ts.walk(tree.root_node):
        if n.type != "variable_declaration":
            continue
        _extract_parameter_values_from_decl(n, source, out)
    return out


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
    var_units: dict[str, UnitExpr],
    source: bytes,
) -> dict[str, FuncSig]:
    """Return ``{name_lc: FuncSig}`` for every ``function`` and ``subroutine``."""
    out: dict[str, FuncSig] = {}
    # Case-insensitive view: a header arg may differ in case from its
    # declaration (Fortran identifiers are case-insensitive).
    vu_lc: dict[str, UnitExpr] = {}
    for _k, _v in var_units.items():
        vu_lc.setdefault(_k.lower(), _v)
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
        arg_units = tuple(vu_lc.get(a.lower()) for a in arg_names)

        return_unit: UnitExpr | None = None
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
                    return_unit = vu_lc.get(_text(ret_id, source).lower())
            if return_unit is None:
                return_unit = vu_lc.get(func_name.lower())

        out[func_name.lower()] = FuncSig(
            arg_names=tuple(arg_names),
            arg_units=arg_units,
            return_unit=return_unit,
            is_subroutine=is_subroutine,
        )
    return out


def collect_module_exports(
    tree: Tree,
    var_units: dict[str, UnitExpr],
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
        export_var_units: dict[str, UnitExpr] = {}
        all_var_names: list[str] = []
        # ``lookup`` resolves case-insensitively (Fortran identifiers are).
        lookup = _make_scoped_lookup(var_units, None)
        for decl in n.children:
            if decl.type != "variable_declaration":
                continue
            for vn in _collect_decl_names(decl, source):
                all_var_names.append(vn)
                u = lookup(vn, None)
                if u is not None:
                    export_var_units[vn] = u

        # Contained procedures: walk only the children to scope
        # correctly. ``_signatures_for_subtree`` takes a ``(name,
        # scope)`` lookup callable; the flat-dict back-compat shape
        # of this function is wrapped to match.
        signatures: dict[str, FuncSig] = {}
        for child in n.children:
            if child.type in ("function", "subroutine"):
                signatures.update(
                    _signatures_for_subtree(child, lookup, source)
                )
            elif child.type == "internal_procedures":
                for grandchild in child.children:
                    if grandchild.type in ("function", "subroutine"):
                        signatures.update(
                            _signatures_for_subtree(grandchild, lookup, source)
                        )

        inner_uses = _extract_inner_uses(n, source)
        default_private, public_names, private_names = _extract_visibility(
            n, source,
        )
        out[name.lower()] = ModuleExports(
            name=name,
            var_units=export_var_units,
            signatures=signatures,
            all_var_names=tuple(all_var_names),
            inner_uses=inner_uses,
            default_private=default_private,
            public_names=public_names,
            private_names=private_names,
        )
    return out


def collect_var_types_type_fields_and_parameter_values(
    tree: Tree, source: bytes,
) -> tuple[
    dict[str, str],
    dict[tuple[str, str], str],
    dict[str, Fraction | int],
]:
    """Produce var-type, type-field-type, and parameter-value maps in one walk.

    Equivalent to ``collect_var_types`` + ``collect_type_field_types`` +
    ``collect_parameter_values`` back-to-back, thirded. Used inside ``check``
    so the per-file context only walks the tree once for all three maps.
    """
    var_types: dict[str, str] = {}
    type_field_types: dict[tuple[str, str], str] = {}
    parameter_values: dict[str, Fraction | int] = {}
    for n in _ts.walk(tree.root_node):
        ntype = n.type
        if ntype == "variable_declaration":
            _extract_parameter_values_from_decl(n, source, parameter_values)
            tn = _decl_type_name(n, source)
            if tn is None:
                continue
            tn_lc = tn.lower()
            for vn in _collect_decl_names(n, source):
                var_types[vn.lower()] = tn_lc
        elif ntype == "derived_type_definition":
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
                    type_field_types[(struct_lc, vn.lower())] = field_type.lower()
    return var_types, type_field_types, parameter_values


def collect_var_types_and_type_field_types(
    tree: Tree, source: bytes,
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Back-compat shim — returns var-types + type-field-types only."""
    var_types, type_field_types, _ = (
        collect_var_types_type_fields_and_parameter_values(tree, source)
    )
    return var_types, type_field_types


def _make_scoped_lookup(
    var_units: dict[str, UnitExpr],
    var_units_by_scope: dict[tuple[str | None, str], UnitExpr] | None,
) -> _ScopedLookup:
    """Build a ``(name, scope_lc) -> Unit | None`` lookup.

    Semantics distinguish None from empty dict:

    - ``var_units_by_scope is None``: caller has only flat data (the
      back-compat path for legacy standalone collectors). Fall back
      to ``var_units.get(name)``, which conflates same-named symbols
      across files but matches pre-scope-aware behaviour.
    - ``var_units_by_scope`` (possibly empty dict): scope-aware
      mode. Try ``(scope, name)`` then ``(None, name)``; return
      ``None`` if neither matches. **No flat fallback** — otherwise an
      unannotated parameter of a generic wrapper (e.g. a NetCDF
      ``put_var(...,v)``) would absorb the unit of an unrelated
      same-named variable elsewhere in the workset (e.g. a wind
      ``v: m/s``), producing spurious H004 diagnostics.
    """
    # Fortran identifiers are case-insensitive. Build lowercased views so a
    # header arg / use-import / field reference resolves its declaration
    # regardless of case (e.g. a ``function f(PTE)`` whose body declares
    # ``real :: pte``, or a consumer using a UPPERCASE module constant).
    if var_units_by_scope is None:
        flat_lc: dict[str, UnitExpr] = {}
        for k, v in var_units.items():
            flat_lc.setdefault(k.lower(), v)
        return lambda name, scope: flat_lc.get(name.lower())

    by_scope_lc: dict[tuple[str | None, str], UnitExpr] = {}
    for (s, n), v in var_units_by_scope.items():
        by_scope_lc.setdefault(
            (s.lower() if s is not None else None, n.lower()), v
        )

    def lookup(name: str, scope: str | None) -> UnitExpr | None:
        name_lc = name.lower()
        if scope is not None:
            u = by_scope_lc.get((scope.lower(), name_lc))
            if u is not None:
                return u
        return by_scope_lc.get((None, name_lc))

    return lookup


def collect_function_signatures_and_module_exports(
    tree: Tree,
    var_units: dict[str, UnitExpr],
    source: bytes,
    *,
    var_units_by_scope: dict[tuple[str | None, str], UnitExpr] | None = None,
) -> tuple[dict[str, FuncSig], dict[str, ModuleExports]]:
    """Produce both function/subroutine signatures *and* module exports
    in a single tree walk.

    Equivalent to running ``collect_function_signatures`` and
    ``collect_module_exports`` back-to-back, but visits the tree once
    instead of twice. Profiling a large workspace showed those two walks
    accounted for ~6-7s in the index phase; consolidating saves about
    half. Public collectors are kept for back-compat (LSP hover etc.).

    When ``var_units_by_scope`` is supplied, argument units are looked
    up per-routine so two subroutines declaring the same name with
    different units no longer alias.
    """
    lookup = _make_scoped_lookup(var_units, var_units_by_scope)
    signatures: dict[str, FuncSig] = {}
    module_exports: dict[str, ModuleExports] = {}
    for n in _ts.walk(tree.root_node):
        ntype = n.type
        if ntype == "function" or ntype == "subroutine":
            sig = _signature_for_node(n, lookup, source)
            if sig is not None:
                name_lc, func_sig = sig
                signatures[name_lc] = func_sig
        elif ntype == "module":
            mod = _module_exports_for_node(n, lookup, source)
            if mod is not None:
                name_lc, exp = mod
                module_exports[name_lc] = exp
    return signatures, module_exports


def _signature_for_node(
    node: Node,
    lookup: _ScopedLookup,
    source: bytes,
) -> tuple[str, FuncSig] | None:
    """Extract a single ``FuncSig`` from a ``function`` / ``subroutine`` node.

    Returns ``(name_lc, FuncSig)`` or ``None`` when the node lacks the
    expected ``*_statement`` / ``name`` children (malformed parse).
    """
    is_subroutine = node.type == "subroutine"
    stmt_type = "subroutine_statement" if is_subroutine else "function_statement"
    stmt = next((c for c in node.children if c.type == stmt_type), None)
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    if name_node is None:
        return None
    func_name = _text(name_node, source)
    scope_lc = func_name.lower()

    params = next((c for c in stmt.children if c.type == "parameters"), None)
    arg_names: list[str] = []
    if params is not None:
        for c in params.children:
            if c.type == "identifier":
                arg_names.append(_text(c, source))
    arg_units = tuple(lookup(a, scope_lc) for a in arg_names)

    return_unit: UnitExpr | None = None
    if not is_subroutine:
        result = next(
            (c for c in stmt.children if c.type == "function_result"), None
        )
        if result is not None:
            ret_id = next(
                (c for c in result.children if c.type == "identifier"), None
            )
            if ret_id is not None:
                return_unit = lookup(_text(ret_id, source), scope_lc)
        if return_unit is None:
            return_unit = lookup(func_name, scope_lc)

    return scope_lc, FuncSig(
        arg_names=tuple(arg_names),
        arg_units=arg_units,
        return_unit=return_unit,
        is_subroutine=is_subroutine,
    )


def _module_exports_for_node(
    node: Node,
    lookup: _ScopedLookup,
    source: bytes,
) -> tuple[str, ModuleExports] | None:
    """Extract ``ModuleExports`` from a single ``module`` node."""
    stmt = next((c for c in node.children if c.type == "module_statement"), None)
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    if name_node is None:
        return None
    name = _text(name_node, source)

    # Module-level variables live at scope=None. Track every declared
    # name (whether annotated or not) so the LSP can flag unannotated
    # exports in hover; ``export_var_units`` keeps only the annotated
    # ones for the actual unit-checking path.
    export_var_units: dict[str, UnitExpr] = {}
    all_var_names: list[str] = []
    for decl in node.children:
        if decl.type != "variable_declaration":
            continue
        for vn in _collect_decl_names(decl, source):
            all_var_names.append(vn)
            u = lookup(vn, None)
            if u is not None:
                export_var_units[vn] = u

    signatures: dict[str, FuncSig] = {}
    for child in node.children:
        if child.type in ("function", "subroutine"):
            signatures.update(_signatures_for_subtree(child, lookup, source))
        elif child.type == "internal_procedures":
            for grandchild in child.children:
                if grandchild.type in ("function", "subroutine"):
                    signatures.update(
                        _signatures_for_subtree(grandchild, lookup, source)
                    )

    # Inner ``use`` clauses + visibility statements — needed for the
    # transitive-export closure (panel's Imports section).
    inner_uses = _extract_inner_uses(node, source)
    default_private, public_names, private_names = _extract_visibility(
        node, source,
    )

    return name.lower(), ModuleExports(
        name=name,
        var_units=export_var_units,
        signatures=signatures,
        all_var_names=tuple(all_var_names),
        inner_uses=inner_uses,
        default_private=default_private,
        public_names=public_names,
        private_names=private_names,
    )


def _extract_inner_uses(node: Node, source: bytes) -> tuple[Any, ...]:
    """Return the ``UseRef`` tuple for every ``use`` directly in a module.

    Reuses :func:`workspace_index.extract_uses` on the module's source
    slice — cheaper than re-walking the tree and guarantees identical
    parsing of ``only:`` / rename lists.
    """
    from dimfort.core.workspace_index import extract_uses
    text = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
    return extract_uses(text)


def _extract_visibility(
    node: Node, source: bytes,
) -> tuple[bool, frozenset[str], frozenset[str]]:
    """Parse module-level ``private`` / ``public`` statements.

    Returns ``(default_private, public_names_lc, private_names_lc)``.
    A bare ``private`` flips the default; ``public :: a, b`` /
    ``private :: a, b`` override individual names.
    """
    default_private = False
    public_names: set[str] = set()
    private_names: set[str] = set()
    for child in node.children:
        if child.type == "private_statement":
            ids = [c for c in child.children if c.type == "identifier"]
            if ids:
                for c in ids:
                    private_names.add(_text(c, source).lower())
            else:
                default_private = True
        elif child.type == "public_statement":
            ids = [c for c in child.children if c.type == "identifier"]
            for c in ids:
                public_names.add(_text(c, source).lower())
    return default_private, frozenset(public_names), frozenset(private_names)


def _signatures_for_subtree(
    node: Node,
    lookup: _ScopedLookup,
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
    scope_lc = func_name.lower()
    params = next((c for c in stmt.children if c.type == "parameters"), None)
    arg_names: list[str] = []
    if params is not None:
        for c in params.children:
            if c.type == "identifier":
                arg_names.append(_text(c, source))
    arg_units = tuple(lookup(a, scope_lc) for a in arg_names)
    return_unit: UnitExpr | None = None
    if not is_subroutine:
        result = next(
            (c for c in stmt.children if c.type == "function_result"), None
        )
        if result is not None:
            ret_id = next(
                (c for c in result.children if c.type == "identifier"), None
            )
            if ret_id is not None:
                return_unit = lookup(_text(ret_id, source), scope_lc)
        if return_unit is None:
            return_unit = lookup(func_name, scope_lc)
    out[scope_lc] = FuncSig(
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


def _build_ctx(
    tree: Tree,
    var_units: Mapping[str, str | UnitExpr],
    *,
    source: bytes,
    file: str | Path,
    table: UnitTable | None = None,
    signatures: dict[str, FuncSig] | None = None,
    field_units: Mapping[tuple[str, str], str | UnitExpr] | None = None,
    var_units_by_scope: Mapping[tuple[str | None, str], str | UnitExpr] | None = None,
    routine_scopes: tuple[tuple[int, int, str], ...] = (),
    assumes: dict[int, tuple[str, str, int, int]] | None = None,
    affine_conversions: dict[int, tuple[str, str, int]] | None = None,
    scale_mode: bool = False,
) -> tuple[_Ctx, list[Diagnostic]]:
    """Build the static :class:`_Ctx` for one file's pass.

    Shared by :func:`check` and :func:`dimfort.core.interactions` so the
    fragile table-parsing / collector setup lives in exactly one place.
    Returns ``(ctx, assume_diags)`` — ``assume_diags`` carries the U002s
    raised by un-parseable ``@unit_assume`` units (empty for most files).
    """
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )

    # Parse the var_units / field_units tables ahead of the walk so
    # the resolver doesn't pay a parse cost per lookup. Accepts any
    # ``UnitExpr`` (Unit / LogWrap / ExpWrap) already parsed by an
    # upstream pass, or a raw annotation string to parse now.
    parsed_vars: dict[str, UnitExpr] = {}
    for name, value in var_units.items():
        if isinstance(value, (Unit, LogWrap, ExpWrap)):
            parsed_vars[name] = value
        else:
            try:
                parsed_vars[name] = _units_mod.parse(value, active_table)
            except UnitError:
                continue
    parsed_fields: dict[tuple[str, str], UnitExpr] = {}
    for (tn, fn), value in (field_units or {}).items():
        if isinstance(value, (Unit, LogWrap, ExpWrap)):
            parsed_fields[(tn.lower(), fn.lower())] = value
        else:
            try:
                parsed_fields[(tn.lower(), fn.lower())] = _units_mod.parse(
                    value, active_table
                )
            except UnitError:
                continue

    # Parse the by-scope table the same way (strings → UnitExpr). Empty
    # ⇒ the resolver falls back to the flat ``parsed_vars`` dict.
    parsed_vars_by_scope: dict[tuple[str | None, str], UnitExpr] = {}
    for key, value in (var_units_by_scope or {}).items():
        if isinstance(value, (Unit, LogWrap, ExpWrap)):
            parsed_vars_by_scope[key] = value
        else:
            try:
                parsed_vars_by_scope[key] = _units_mod.parse(value, active_table)
            except UnitError:
                continue

    # Parse the @unit_assume directives (line → (unit_text, reason, col)).
    # A unit that fails to parse surfaces as U002 at its column, mirroring
    # the @unit{} parse-error path; the assume is then dropped.
    parsed_assumes: dict[int, tuple[UnitExpr, str, int, int]] = {}
    assume_diags: list[Diagnostic] = []
    for line_no, (unit_text, reason, col, end_col) in (assumes or {}).items():
        try:
            parsed_assumes[line_no] = (
                _units_mod.parse(unit_text, active_table), reason, col, end_col,
            )
        except UnitError as exc:
            from dimfort.core.rewrite import suggest_rewrite
            suggestion = suggest_rewrite(unit_text, active_table)
            msg = f"@unit_assume unit {unit_text!r}: {exc}"
            if suggestion is not None:
                msg += f"; did you mean {suggestion!r}?"
            assume_diags.append(Diagnostic(
                file=str(file),
                start=Position(line_no, col),
                end=Position(line_no, end_col),
                severity=Severity.ERROR,
                code="U002",
                message=msg,
                suggested_rewrite=suggestion,
            ))

    if signatures is None:
        signatures, _ = collect_function_signatures_and_module_exports(
            tree, parsed_vars, source,
            var_units_by_scope=parsed_vars_by_scope or None,
        )

    var_types, type_field_types, parameter_values = (
        collect_var_types_type_fields_and_parameter_values(tree, source)
    )
    ctx = _Ctx(
        file=str(file),
        var_units=parsed_vars,
        table=active_table,
        signatures=signatures,
        var_types=var_types,
        type_field_types=type_field_types,
        field_units=parsed_fields,
        var_units_by_scope=parsed_vars_by_scope,
        routine_scopes=tuple(routine_scopes),
        _scope_starts=tuple(r[0] for r in routine_scopes),
        parameter_values=parameter_values,
        assumes=parsed_assumes,
        affine_conversions=dict(affine_conversions or {}),
        scope_aware=var_units_by_scope is not None,
        scale_mode=scale_mode,
    )
    return ctx, assume_diags


def check(
    tree: Tree,
    var_units: Mapping[str, str | UnitExpr],
    *,
    source: bytes,
    file: str | Path,
    table: UnitTable | None = None,
    signatures: dict[str, FuncSig] | None = None,
    field_units: Mapping[tuple[str, str], str | UnitExpr] | None = None,
    var_units_by_scope: Mapping[tuple[str | None, str], str | UnitExpr] | None = None,
    routine_scopes: tuple[tuple[int, int, str], ...] = (),
    out_autocast_events: list[AutocastEvent] | None = None,
    assumes: dict[int, tuple[str, str, int, int]] | None = None,
    affine_conversions: dict[int, tuple[str, str, int]] | None = None,
    scale_mode: bool = False,
) -> list[Diagnostic]:
    """Run the checker over a tree-sitter-parsed file.

    Signature parallels :func:`ast_checker.check` so the same callers
    can swap implementations. Tree-sitter requires the original
    ``source`` bytes alongside the tree to extract identifier text.
    """
    ctx, assume_diags = _build_ctx(
        tree,
        var_units,
        source=source,
        file=file,
        table=table,
        signatures=signatures,
        field_units=field_units,
        var_units_by_scope=var_units_by_scope,
        routine_scopes=routine_scopes,
        assumes=assumes,
        affine_conversions=affine_conversions,
        scale_mode=scale_mode,
    )
    out: list[Diagnostic] = []
    out.extend(assume_diags)
    out.extend(_emit_u005_for_unannotated(tree, ctx, source))
    out.extend(_emit_h021_tyvar_positions(tree, ctx, source))
    # P001: flag regions tree-sitter couldn't parse (no unit guarantee there).
    out.extend(_emit_unparsed_regions(tree, ctx))

    # Phase D: if tracing was activated by the caller, open a fresh
    # per-statement trace so each diagnostic carries just its own
    # statement's chain rather than the whole file's accumulated steps.
    tracing_on = current_trace() is not None

    def _attach_traces_since(start_idx: int, trace_obj: Trace | None) -> None:
        if trace_obj is None:
            return
        snapshot = trace_obj.snapshot()
        if not snapshot:
            return
        import dataclasses
        for i in range(start_idx, len(out)):
            out[i] = dataclasses.replace(out[i], trace=snapshot)

    from contextlib import AbstractContextManager, nullcontext

    def _stmt_trace_ctx() -> AbstractContextManager[Trace | None]:
        return with_trace() if tracing_on else nullcontext()

    for node in _ts.walk(tree.root_node):
        kind = node.type

        if kind == "assignment_statement":
            before_len = len(out)
            with _stmt_trace_ctx() as stmt_trace:
                target, value = _assignment_sides(node)
                assume = _assume_for_node(node, ctx)
                affine = (
                    _affine_conv_for_node(node, ctx) if ctx.scale_mode else None
                )
                if assume is not None:
                    au, reason, aline, acol, aend = assume
                    # Escape hatch: do NOT walk the RHS — that suppresses
                    # D1.4 and any interior fire. Record an audit note, then
                    # still check the assumption against a declared LHS unit
                    # (so an assume can't mask a declared-unit conflict).
                    out.append(Diagnostic(
                        file=str(ctx.file),
                        start=Position(aline, acol),
                        end=Position(aline, aend),
                        severity=Severity.INFO,
                        code="U020",
                        message=f"RHS unit assumed {format_unit(au)} ({reason})",
                    ))
                    tu = _resolve(target, ctx, source) if target is not None else None
                    if tu is not None and not equal_dim(tu, au):
                        out.append(_emit_h001_or_h023(node, tu, au, ctx))
                elif affine is not None:
                    src_text, tgt_text, aline, acol = affine
                    # The directive owns scale-checking for this statement.
                    # Walk the RHS for genuine dimension/structure errors but
                    # drop scale codes (S001/S002) — the verification below is
                    # the sole scale authority here (scale.md §11.4 step 3).
                    for d in _walk_expressions(value, ctx, source):
                        if d.code not in ("S001", "S002"):
                            out.append(d)
                    s003 = _verify_affine_conversion(
                        node, target, value, src_text, tgt_text, ctx, source,
                    )
                    if s003 is not None:
                        out.append(s003)
                    # Valid ⇒ nothing emitted, and the S002 the assignment
                    # would raise is suppressed (the scale block below is not
                    # run on this path) — the statement is the blessed
                    # conversion and its result is cleanly the target frame.
                else:
                    out.extend(_walk_expressions(value, ctx, source))
                    verdict, tu, ru = _assignment_homogeneity(
                        target, value, ctx, source,
                    )
                    if verdict == "wrapper_untag" and tu is not None and ru is not None:
                        out.append(
                            _emit_d16_untag(
                                target if target is not None else node,
                                tu, ru, ctx,
                            )
                        )
                    elif verdict == "mismatch" and tu is not None and ru is not None:
                        # Span the squiggle over the whole assignment so the
                        # editor highlights both sides of `=`; lets the user
                        # see the offending statement at a glance instead of
                        # squinting at the LHS identifier.
                        out.append(_emit_h001_or_h023(node, tu, ru, ctx))
                    elif (
                        verdict == "autocast"
                        and out_autocast_events is not None
                        and value is not None
                        and tu is not None
                    ):
                        # R4.4 — record the event for any audit consumer.
                        # The literal_text is the source slice of the RHS,
                        # which may be a compound numeric expression like
                        # ``2.0 * 3.14`` per _is_pure_numeric_constant.
                        out_autocast_events.append(
                            _build_autocast_event(value, tu, str(ctx.file), source)
                        )
                    # Scale layer (opt-in): dims agree (homogeneous) but the
                    # magnitude factors differ → S001, or the zero-point
                    # differs → S002 (path 1, e.g. K = degC). Factor takes
                    # precedence (compare order); they're mutually exclusive
                    # since offset_mismatch requires equal factors. Dimension-
                    # only mode leaves this untouched (scale_mode default off).
                    if (
                        ctx.scale_mode and verdict == "homogeneous"
                        and tu is not None and ru is not None
                    ):
                        ratio = _scale_mismatch_ratio(tu, ru)
                        if ratio is not None:
                            out.append(_emit_s001(node, tu, ru, ratio, ctx))
                        else:
                            delta = _offset_mismatch_delta(tu, ru)
                            if delta is not None:
                                # Render the offset as a readable decimal
                                # (-273.15), not the raw Fraction (-5463/20).
                                delta_txt = f"{float(delta):g}"
                                out.append(_emit_s002(
                                    node,
                                    f"same dimension and scale but a different "
                                    f"zero-point (offsets differ by {delta_txt}, "
                                    f"e.g. °C vs K) — add the conversion or "
                                    f"keep units consistent",
                                    ctx,
                                ))
            _attach_traces_since(before_len, stmt_trace)
            continue

        if kind == "subroutine_call":
            before_len = len(out)
            with _stmt_trace_ctx() as stmt_trace:
                name = _call_callee_name(node, source)
                if name is None:
                    _attach_traces_since(before_len, stmt_trace)
                    continue
                name_lc = name.lower()
                arg_exprs = _call_args(node, source)
                for a in arg_exprs:
                    out.extend(_walk_expressions(a, ctx, source))
                sig = ctx.signatures.get(name_lc)
                if sig is not None and sig.is_subroutine:
                    out.extend(
                        _check_call_args_against_sig(
                            sig, name_lc, arg_exprs, node, ctx, source
                        )
                    )
            _attach_traces_since(before_len, stmt_trace)

    # Apply per-rule severity overrides from .dimfort.toml.
    from dimfort.core.diagnostics import finalize_diagnostics
    return finalize_diagnostics(out)


# ---------------------------------------------------------------------------
# Intended-public checker API.
#
# These are the resolution primitives that tooling consumers (the LSP hover /
# inlay / panel renderers and the cross-site interactions analysis) need in
# order to report units consistently with what ``check`` computes. They are
# exposed under stable public names so those consumers don't reach into the
# leading-underscore internals (which are free to change shape). The aliases
# point at the same objects, so docstrings and behaviour are identical.
#
#   Ctx                     — the per-file static context a resolver runs in
#                             (build one via ``dimfort.lsp.tree_access``).
#   resolve_unit            — resolve a node's unit (or None if unknown).
#   assignment_homogeneity  — the single source of truth for an assignment's
#                             verdict + (lhs, effective-rhs) units.
#   resolve_member_chain    — resolve a derived-type member chain ``a%b%c``.
#   is_pure_numeric_constant — R4.4 autocast predicate (literal-only subtree).
# ---------------------------------------------------------------------------
Ctx = _Ctx
resolve_unit = _resolve
assignment_homogeneity = _assignment_homogeneity
resolve_member_chain = _resolve_member_chain
is_pure_numeric_constant = _is_pure_numeric_constant


__all__ = [
    "Ctx",
    "ModuleExports",
    "apply_use_clauses",
    "assignment_homogeneity",
    "check",
    "collect_function_signatures",
    "collect_module_exports",
    "collect_type_field_types",
    "collect_var_types",
    "is_pure_numeric_constant",
    "resolve_member_chain",
    "resolve_unit",
]
