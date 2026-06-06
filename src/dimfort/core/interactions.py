"""On-demand cross-site analysis of a single symbol.

Where :mod:`ts_checker` answers "is this *statement* homogeneous?", this
module answers "what does every site that touches this *variable* imply about
its unit, and do those implications agree?".

For a queried symbol it collects every read/write across the workset, tags each
with the constraint it places on the symbol's unit (``declares`` / ``contributes``
/ ``requires`` / ``uses``), and flags ``X001`` when two sites in the same scope
disagree on the dimension. See ``docs/design/interaction-points.md``.

Public entry: :func:`collect_interactions`. CLI-agnostic — returns a structured
:class:`SymbolReport` that a future LSP/panel consumer can serialise.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tree_sitter import Node

from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.ts_checker import (
    Ctx,
    _assignment_sides,
    _build_ctx,
    _call_args,
    _call_callee_name,
    _math_op,
    _math_operands,
    _position,
    _text,
    assignment_homogeneity,
    is_pure_numeric_constant,
    resolve_unit,
)
from dimfort.core.units import Unit, UnitExpr, compare, format_unit

if TYPE_CHECKING:
    from dimfort.core.multifile import WorksetResult

# Constraint kinds (see the design doc's table).
DECLARES = "declares"
CONTRIBUTES = "contributes"
REQUIRES = "requires"
USES = "uses"

# Only these kinds pin the variable's actual unit; ``uses`` does not.
_CONSTRAINING = frozenset({DECLARES, CONTRIBUTES, REQUIRES})

# User-facing labels. Deliberately *structural* (what the site is) rather than
# directional (contributes/requires) — the latter forced a viewpoint that read
# ambiguously. The internal kind names above stay as-is; this is the only
# vocabulary any user surface (CLI, X001 message) should speak.
KIND_DISPLAY = {
    DECLARES: "declaration",
    CONTRIBUTES: "write",
    REQUIRES: "read",
    USES: "undetermined",
}


@dataclass(frozen=True)
class InteractionPoint:
    """One occurrence of a queried symbol classified by its context.

    Attributes:
        file: Absolute (stringified) path of the source file.
        line: 1-based line number of the occurrence.
        column: 1-based column of the occurrence.
        scope: Lower-cased name of the enclosing routine, or ``None``
            at module level.
        kind: One of ``DECLARES`` / ``CONTRIBUTES`` / ``REQUIRES`` /
            ``USES`` (see the design doc for the semantics).
        unit: Unit the site claims about the symbol, or ``None`` when
            the context places no equality constraint.
        snippet: The enclosing statement, whitespace-collapsed.
    """

    file: str
    line: int           # 1-based
    column: int         # 1-based
    scope: str | None   # enclosing routine (lower-cased), or None at module level
    kind: str           # DECLARES | CONTRIBUTES | REQUIRES | USES
    unit: UnitExpr | None
    snippet: str        # the enclosing statement, whitespace-collapsed

    @property
    def unit_str(self) -> str:
        """Human-readable rendering of :attr:`unit` (``"?"`` if absent)."""
        return format_unit(self.unit) if self.unit is not None else "?"


@dataclass(frozen=True)
class Conflict:
    """A pair of constraining sites that disagree on the symbol's unit.

    Attributes:
        symbol: The queried symbol, in its caller-supplied casing.
        site: The site that disagrees with an earlier reference.
        reference: The earlier site whose claim conflicts with
            :attr:`site`.
        diagnostic: An ``X001`` diagnostic anchored on :attr:`site`,
            ready to be surfaced by a CLI / LSP consumer.
    """

    symbol: str
    site: InteractionPoint       # the site that disagrees
    reference: InteractionPoint  # the earlier site it conflicts with
    diagnostic: Diagnostic


@dataclass(frozen=True)
class SymbolReport:
    """Structured result of :func:`collect_interactions`.

    Attributes:
        symbol: The queried symbol, in its caller-supplied casing.
        points: Every classified occurrence, sorted by
            ``(file, line, column)``.
        conflicts: Every per-scope unit-claim disagreement among the
            constraining points.
    """

    symbol: str
    points: tuple[InteractionPoint, ...] = ()
    conflicts: tuple[Conflict, ...] = ()


# ---------------------------------------------------------------------------
# Tree helpers
# ---------------------------------------------------------------------------


def _same(a: Node | None, b: Node | None) -> bool:
    """Return ``True`` when ``a`` and ``b`` reference the same AST node."""
    return a is not None and b is not None and a.id == b.id


def _iter_identifiers(node: Node, name_lc: str, source: bytes) -> Iterator[Node]:
    """Yield every ``identifier`` node whose text equals ``name_lc`` (case-insensitive).

    Args:
        node: Subtree to search (typically a file's root node).
        name_lc: Lower-cased symbol name to match.
        source: Source bytes the tree was parsed from.

    Yields:
        Each matching identifier node in document order.
    """
    if node.type == "identifier" and _text(node, source).lower() == name_lc:
        yield node
    for child in node.children:
        yield from _iter_identifiers(child, name_lc, source)


def _ancestor_types(node: Node) -> list[str]:
    """Return the ``type`` of every ancestor of ``node``, nearest first."""
    out: list[str] = []
    p = node.parent
    while p is not None:
        out.append(p.type)
        p = p.parent
    return out


_SKIP_ANCESTORS = frozenset({
    "use_statement", "subroutine_statement", "function_statement",
    "derived_type_statement", "derived_type_definition", "interface",
    "import_statement", "implicit_statement",
})


def _enclosing_statement(node: Node) -> Node:
    """Return the nearest ancestor that looks like a statement.

    Used to anchor the human-readable snippet on the smallest enclosing
    statement-shaped node. Falls back to ``node`` itself if none of its
    ancestors qualify.
    """
    p = node
    while p.parent is not None:
        if p.type.endswith("_statement") or p.type in (
            "subroutine_call", "call_expression",
        ):
            return p
        p = p.parent
    return node


def _snippet(node: Node, source: bytes) -> str:
    """Return the whitespace-collapsed text of ``node``'s enclosing statement."""
    return " ".join(_text(_enclosing_statement(node), source).split())


# ---------------------------------------------------------------------------
# The constraint solver
# ---------------------------------------------------------------------------


def _unit_mul(a: UnitExpr | None, b: UnitExpr | None) -> Unit | None:
    """Multiply two units when both are concrete ``Unit`` values, else ``None``."""
    if isinstance(a, Unit) and isinstance(b, Unit):
        return a * b
    return None


def _unit_div(a: UnitExpr | None, b: UnitExpr | None) -> Unit | None:
    """Divide two units when both are concrete ``Unit`` values, else ``None``."""
    if isinstance(a, Unit) and isinstance(b, Unit):
        return a / b
    return None


def _additive_root(node: Node) -> Node:
    """Walk up through a (left/right-nested) ``+``/``-`` chain to its top node."""
    root = node
    while True:
        par = root.parent
        if (
            par is not None and par.type == "math_expression"
            and _math_op(par) in ("+", "-")
        ):
            root = par
        else:
            return root


def _additive_terms(node: Node) -> list[Node]:
    """Flatten a ``+``/``-`` expression into its individual operand subtrees."""
    if node.type == "math_expression" and _math_op(node) in ("+", "-"):
        left, right = _math_operands(node)
        out: list[Node] = []
        if left is not None:
            out += _additive_terms(left)
        if right is not None:
            out += _additive_terms(right)
        return out
    return [node]


def _required_unit_of(node: Node, ctx: Ctx, source: bytes) -> UnitExpr | None:
    """Unit the *position* of ``node`` is forced to have by its context.

    Walks up the AST, propagating a known target unit down through
    arithmetic. ``None`` = the context places no equality constraint
    (so the site is a ``uses``, not a ``requires``). See the design
    doc.

    Args:
        node: The occurrence node whose context is being analysed.
        ctx: The file's typing context (signatures, var_units, scopes).
        source: Source bytes the tree was parsed from.

    Returns:
        The required unit, or ``None`` when no equality constraint
        applies (or the constraint can't be resolved with the current
        information).
    """
    p = node.parent
    if p is None:
        return None
    pt = p.type

    if pt in ("parenthesized_expression", "unary_expression"):
        return _required_unit_of(p, ctx, source)

    if pt == "assignment_statement":
        lhs, rhs = _assignment_sides(p)
        if _same(node, rhs):          # node is the whole RHS
            return resolve_unit(lhs, ctx, source)
        return None                   # node is the LHS → a write, not a constraint

    if pt == "argument_list":
        call = p.parent
        if call is not None and call.type in ("call_expression", "subroutine_call"):
            callee = _call_callee_name(call, source)
            if callee is not None:
                args = _call_args(call, source)
                idx = next(
                    (i for i, a in enumerate(args) if _same(a, node)), None
                )
                sig = ctx.signatures.get(callee.lower())
                if (
                    idx is not None and sig is not None
                    and idx < len(sig.arg_units)
                ):
                    return sig.arg_units[idx]
        return None

    if pt == "math_expression":
        op = _math_op(p)
        left, right = _math_operands(p)
        sibling = right if _same(node, left) else left
        if op in ("+", "-"):
            req = _required_unit_of(p, ctx, source)
            if req is not None:
                return req
            # Any *other* term in the enclosing +/- chain pins the unit (a bare
            # literal ⇒ {1}). Don't insist on resolving one whole sibling
            # subtree — a single unknown inside it shouldn't blind us to a
            # literal elsewhere in the sum. Skip every term within our own
            # subtree (avoids circularity if `node` is itself annotated).
            for term in _additive_terms(_additive_root(p)):
                if node.start_byte <= term.start_byte and term.end_byte <= node.end_byte:
                    continue
                u = resolve_unit(term, ctx, source)
                if u is not None:
                    return u
            return None
        if op in ("*", "/"):
            req = _required_unit_of(p, ctx, source)
            if req is None:
                return None
            sib = resolve_unit(sibling, ctx, source)
            if sib is None:
                return None
            if op == "*":
                return _unit_div(req, sib)            # node * sib = req
            if _same(node, left):
                return _unit_mul(req, sib)            # node / sib = req
            return _unit_div(sib, req)                # sib / node = req
        return None  # '**' and anything else: no equality constraint

    return None


# ---------------------------------------------------------------------------
# Classification of one occurrence
# ---------------------------------------------------------------------------


def _classify(
    occ: Node,
    ctx: Ctx,
    source: bytes,
    file: str,
    name_lc: str,
) -> InteractionPoint | None:
    """Tag a single identifier occurrence with its interaction kind.

    Args:
        occ: The matched ``identifier`` node.
        ctx: The file's typing context.
        source: Source bytes the tree was parsed from.
        file: Stringified path of the source file (carried into the
            returned point).
        name_lc: Lower-cased name being queried (used for the
            function-vs-array disambiguation when ``occ`` is a callee).

    Returns:
        An :class:`InteractionPoint` describing the site, or ``None``
        when the occurrence is one that should be filtered out (e.g.
        on a ``use`` line, the callee slot of a ``CALL``, or a call to
        a user function with the same name as the queried symbol).
    """
    anc = _ancestor_types(occ)

    # Declaration site → a `declares` point (unit from the scoped table).
    if "variable_declaration" in anc:
        scope = ctx.scope_at(occ.start_byte)
        unit = ctx.unit_for(_text(occ, source), occ.start_byte)
        pos = _position(occ)
        return InteractionPoint(
            file=file, line=pos.line, column=pos.column, scope=scope,
            kind=DECLARES, unit=unit, snippet=_snippet(occ, source),
        )

    if any(a in _SKIP_ANCESTORS for a in anc):
        return None

    parent = occ.parent
    if parent is not None and parent.type == "subroutine_call":
        return None  # callee of a CALL statement

    # Resolve the value node (handles array element `x(i)`).
    value = occ
    if parent is not None and parent.type == "call_expression":
        sig = ctx.signatures.get(name_lc)
        if sig is not None and not sig.is_subroutine:
            return None  # a call to a user function named like the symbol
        value = parent  # array element access x(i)

    scope = ctx.scope_at(occ.start_byte)
    pos = _position(occ)
    snippet = _snippet(occ, source)

    # Write (producer): value node is the LHS of an assignment.
    vp = value.parent
    if vp is not None and vp.type == "assignment_statement":
        lhs, rhs = _assignment_sides(vp)
        if _same(value, lhs):
            # Route through the checker's homogeneity logic so a pure-literal
            # RHS (``x = 0.0``) is handled by the autocast rule (R4.4) exactly
            # as ``check`` does: the literal is unit-agnostic, adopts the
            # declared LHS unit, and makes no independent claim — so it can't
            # manufacture a conflict. A real computed RHS keeps its own unit.
            _verdict, lhs_unit, eff_rhs = assignment_homogeneity(
                lhs, rhs, ctx, source
            )
            contributed = (
                lhs_unit
                if (rhs is not None and is_pure_numeric_constant(rhs))
                else eff_rhs
            )
            return InteractionPoint(
                file=file, line=pos.line, column=pos.column, scope=scope,
                kind=CONTRIBUTES, unit=contributed, snippet=snippet,
            )

    # Read: does the context pin a unit?
    req = _required_unit_of(value, ctx, source)
    return InteractionPoint(
        file=file, line=pos.line, column=pos.column, scope=scope,
        kind=REQUIRES if req is not None else USES, unit=req, snippet=snippet,
    )


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


def _is_conflict(a: UnitExpr, b: UnitExpr, *, scale: bool) -> bool:
    """Return ``True`` when two unit claims disagree.

    Dimensional mismatches always count. Magnitude (``factor``)
    mismatches only count when ``scale`` is set, matching the
    opt-in S001 / S002 semantics.
    """
    verdict = compare(a, b)
    if verdict.kind == "dim_mismatch":
        return True
    return bool(scale and verdict.kind == "scale_mismatch")


def _detect_conflicts(
    points: list[InteractionPoint], symbol: str, *, scale: bool
) -> list[Conflict]:
    """Within each (file, scope) group, flag constraining sites that disagree.

    Same-named variables in different routines are different variables
    (finding #018), so conflict detection never crosses a scope
    boundary.

    Args:
        points: All classified occurrences of the symbol.
        symbol: The queried symbol in caller-supplied casing (carried
            into each emitted ``Conflict``).
        scale: Whether magnitude mismatches count as conflicts.

    Returns:
        One :class:`Conflict` per disagreeing site (per group), with
        repeats collapsed when the same line/column would otherwise
        report the same claim twice.
    """
    groups: dict[tuple[str, str | None], list[InteractionPoint]] = {}
    for p in points:
        if p.kind in _CONSTRAINING and p.unit is not None:
            groups.setdefault((p.file, p.scope), []).append(p)

    conflicts: list[Conflict] = []
    seen: set[tuple[str, int, int, str]] = set()
    for constraining in groups.values():
        if len(constraining) < 2:
            continue
        reference = constraining[0]
        for p in constraining[1:]:
            assert reference.unit is not None and p.unit is not None
            # Collapse repeats: a symbol used twice on one line (e.g.
            # `a*x - b*x`) yields identical conflicts — report it once.
            key = (p.file, p.line, reference.line, p.unit_str)
            if key in seen:
                continue
            if _is_conflict(reference.unit, p.unit, scale=scale):
                seen.add(key)
                msg = (
                    f"conflicting unit claims for {symbol!r}: "
                    f"{KIND_DISPLAY[p.kind]} here claims {p.unit_str}, but "
                    f"{KIND_DISPLAY[reference.kind]} at "
                    f"{reference.file}:{reference.line} claims {reference.unit_str}"
                )
                diag = Diagnostic(
                    file=p.file,
                    start=Position(p.line, p.column),
                    end=Position(p.line, p.column),
                    severity=Severity.ERROR,
                    code="X001",
                    message=msg,
                )
                conflicts.append(Conflict(symbol, p, reference, diag))
    return conflicts


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def _file_matches(path: Path, file_filter: str | None) -> bool:
    """Return ``True`` when ``path`` matches the user-supplied file filter.

    A filter matches by basename equality or by string-suffix on the
    stringified path, so users can pass either ``foo.f90`` or
    ``sub/foo.f90``. ``None`` matches every path.
    """
    if file_filter is None:
        return True
    return path.name == file_filter or str(path).endswith(file_filter)


def collect_interactions(
    workset: WorksetResult,
    symbol: str,
    *,
    file: str | None = None,
    scope: str | None = None,
    scale: bool = False,
) -> SymbolReport:
    """Collect every interaction point for ``symbol`` across ``workset``.

    Args:
        workset: A workset already produced by
            :func:`dimfort.core.multifile.check_files`. Trees, scoped
            unit tables, signatures, and routine-scope ranges are all
            consumed.
        symbol: The variable name to analyse (case-insensitive). The
            caller-supplied casing is preserved in the returned
            report.
        file: Optional file filter; see :func:`_file_matches` for
            matching semantics.
        scope: Optional routine-name filter (case-insensitive).
        scale: Include magnitude (``factor``) disagreements as
            conflicts, mirroring S001's opt-in.

    Returns:
        A :class:`SymbolReport` with points sorted by
        ``(file, line, column)`` and conflicts detected per
        ``(file, scope)`` group.
    """
    name_lc = symbol.lower()
    name_bytes = name_lc.encode("utf-8")
    scope_lc = scope.lower() if scope is not None else None
    points: list[InteractionPoint] = []

    for path, (tree, source) in workset.trees.items():
        if not _file_matches(path, file):
            continue
        # Cheap gate: skip files that don't mention the symbol at all, so a
        # whole-workset query (esp. from the LSP) doesn't build a ctx per file.
        if name_bytes not in source.lower():
            continue
        att = workset.attachments.get(path)
        routine_scopes = att.routine_scopes if att is not None else ()
        ctx, _ = _build_ctx(
            tree,
            {},
            source=source,
            file=str(path),
            signatures=workset.signatures,
            field_units=workset.merged_field_units,
            var_units_by_scope=workset.var_units_by_scope.get(path, {}),
            routine_scopes=routine_scopes,
            scale_mode=scale,
        )
        for occ in _iter_identifiers(tree.root_node, name_lc, source):
            point = _classify(occ, ctx, source, str(path), name_lc)
            if point is None:
                continue
            if scope_lc is not None and point.scope != scope_lc:
                continue
            points.append(point)

    points.sort(key=lambda p: (p.file, p.line, p.column))
    conflicts = _detect_conflicts(points, symbol, scale=scale)
    return SymbolReport(symbol=symbol, points=tuple(points), conflicts=tuple(conflicts))
