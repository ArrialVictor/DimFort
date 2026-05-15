"""AST-only unit checker — Phase 0 spike.

Walks LFortran's AST (no ASR) and emits the H001/H002 family of
diagnostics. Reads variable units from an already-attached
``var_units`` table (produced by ``core.annotations`` +
``core.attach``).

Phase 0 scope (deliberately tiny): ``Name | Num | BinOp(+,-,*,/) |
Assignment``. Cross-file, intrinsics, casts, derived types, array
sections, and everything else are explicit "unknown" returns; if a
resolver hits one, the expression's unit is ``None`` and downstream
checks silently no-op for that expression. This is fine for Phase 0
because the test fixture only exercises supported nodes.

See docs/ast-only-design.md for the full multi-phase plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dimfort.core import units as _units_mod
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.units import Unit, UnitError, UnitTable, equal_dim, format_unit


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Static context for one file's check pass."""

    file: str
    var_units: dict[str, Unit]
    table: UnitTable


def _loc_position(loc: dict | None) -> Position:
    """Convert an AST ``loc`` dict to a 1-based Position. Missing loc
    becomes (0, 0) — diagnostics still emit, just without precise
    placement."""
    if not isinstance(loc, dict):
        return Position(0, 0)
    return Position(int(loc.get("first_line", 0)), int(loc.get("first_column", 0)))


def _node(x: object) -> str | None:
    """Return the ``node`` discriminator of an AST node, or ``None``."""
    if isinstance(x, dict):
        kind = x.get("node")
        return kind if isinstance(kind, str) else None
    return None


def _fields(x: object) -> dict:
    if isinstance(x, dict):
        f = x.get("fields")
        if isinstance(f, dict):
            return f
    return {}


def _node_loc(x: object) -> dict | None:
    """``loc`` is a sibling of ``node`` / ``fields`` on every AST node."""
    if isinstance(x, dict):
        loc = x.get("loc")
        return loc if isinstance(loc, dict) else None
    return None


def _resolve(expr: object, ctx: _Ctx) -> Unit | None:
    """Return the unit of ``expr``, or ``None`` if we don't know.

    Unknown is a first-class outcome: many AST node kinds (intrinsics,
    casts, derived-type access, function calls) aren't supported in
    Phase 0. Returning ``None`` lets the caller skip the check rather
    than raise.
    """
    kind = _node(expr)
    if kind is None:
        return None
    fields = _fields(expr)

    if kind == "Name":
        name = fields.get("id")
        if not isinstance(name, str):
            return None
        # ``member`` non-empty means ``a%b%c`` — Phase 1+ territory.
        member = fields.get("member") or []
        if member:
            return None
        return ctx.var_units.get(name)

    if kind == "Num":
        # Numeric literals are dimensionless. We model that as the
        # neutral element of the unit algebra ("1").
        return _units_mod.parse("1", ctx.table)

    if kind == "BinOp":
        left = _resolve(fields.get("left"), ctx)
        right = _resolve(fields.get("right"), ctx)
        op = fields.get("op")
        if left is None or right is None:
            return None
        if op in ("Add", "Sub"):
            # Caller should have already emitted H002 if these don't
            # match. Either way the result unit is the LHS.
            return left
        if op == "Mul":
            return left * right
        if op == "Div":
            return left / right
        # Pow + everything else: not in Phase 0 scope.
        return None

    # Unsupported node kind in Phase 0.
    return None


def _emit_h001(
    target_loc: dict | None,
    target_unit: Unit,
    rhs_unit: Unit,
    ctx: _Ctx,
) -> Diagnostic:
    pos = _loc_position(target_loc)
    return Diagnostic(
        file=ctx.file,
        start=pos,
        end=pos,
        severity=Severity.ERROR,
        code="H001",
        message=(
            f"Assignment unit mismatch: "
            f"{format_unit(target_unit)} ≠ {format_unit(rhs_unit)}"
        ),
    )


def _emit_h002(
    op_loc: dict | None,
    left_unit: Unit,
    right_unit: Unit,
    ctx: _Ctx,
) -> Diagnostic:
    pos = _loc_position(op_loc)
    return Diagnostic(
        file=ctx.file,
        start=pos,
        end=pos,
        severity=Severity.ERROR,
        code="H002",
        message=(
            f"Operand unit mismatch in '+'/'-': "
            f"{format_unit(left_unit)} ≠ {format_unit(right_unit)}"
        ),
    )


def _walk_expressions(expr: object, ctx: _Ctx) -> Iterable[Diagnostic]:
    """Recursive walk of an expression sub-tree, yielding H002s for any
    `+`/`-` operand mismatch encountered."""
    kind = _node(expr)
    if kind != "BinOp":
        # Recurse into any child dicts/lists looking for nested BinOps.
        if isinstance(expr, dict):
            for v in expr.values():
                yield from _walk_expressions(v, ctx)
        elif isinstance(expr, list):
            for v in expr:
                yield from _walk_expressions(v, ctx)
        return

    fields = _fields(expr)
    left = fields.get("left")
    right = fields.get("right")
    yield from _walk_expressions(left, ctx)
    yield from _walk_expressions(right, ctx)

    op = fields.get("op")
    if op not in ("Add", "Sub"):
        return
    lu = _resolve(left, ctx)
    ru = _resolve(right, ctx)
    if lu is None or ru is None:
        return
    if not equal_dim(lu, ru):
        yield _emit_h002(_node_loc(expr), lu, ru, ctx)


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def check(
    ast: dict,
    var_units: dict[str, str],
    *,
    file: str | Path,
    table: UnitTable | None = None,
) -> list[Diagnostic]:
    """Run the Phase 0 AST checker.

    ``var_units`` is the same string-keyed table the ASR checker takes:
    ``{varname: unit_text}``. Strings that don't parse are dropped
    silently (we'd already have flagged them as U002 in the attach
    phase).
    """
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )

    parsed: dict[str, Unit] = {}
    for name, text in var_units.items():
        try:
            parsed[name] = _units_mod.parse(text, active_table)
        except UnitError:
            continue

    ctx = _Ctx(file=str(file), var_units=parsed, table=active_table)
    out: list[Diagnostic] = []

    from dimfort.core.lfortran import walk
    for node in walk(ast):
        if _node(node) != "Assignment":
            continue
        fields = _fields(node)
        target = fields.get("target")
        value = fields.get("value")

        # H002s in sub-expressions of the RHS.
        out.extend(_walk_expressions(value, ctx))

        target_unit = _resolve(target, ctx)
        rhs_unit = _resolve(value, ctx)
        if target_unit is None or rhs_unit is None:
            continue
        if not equal_dim(target_unit, rhs_unit):
            out.append(_emit_h001(_node_loc(target), target_unit, rhs_unit, ctx))

    return out
