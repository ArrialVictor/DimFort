"""Dimensional homogeneity checker (semantic phase).

Consumes:

- An LFortran ASR tree (a ``dict`` from JSON).
- A ``{var_name: unit_text}`` map produced by :mod:`dimfort.core.attach`.
- Optionally, a :class:`UnitTable` for resolving the unit strings; the
  default loaded by :mod:`dimfort.core.unit_config` is used otherwise.

Produces a list of :class:`Diagnostic`s.

This is the v1 slice. Currently handles:

- ``H001`` — assignment LHS unit must match RHS unit.
- ``H002`` — ``+`` / ``-`` operands must share a dimension.
- Expressions: ``Var``, ``RealConstant``, ``IntegerConstant``,
  ``RealBinOp`` (Add / Sub / Mul / Div / Pow with integer exponent),
  ``IntegerBinOp`` (same ops), and ``RealUnaryMinus``.

Not yet handled (returns "unknown unit" → checks are skipped on the
affected expressions):

- Intrinsic function calls (``sqrt``, ``exp``, …).
- User-defined function and subroutine calls.
- Derived-type field access (``b%v``).
- Rational exponents in ``Pow``.
- Array operations, intent propagation, generic interfaces.

An expression with any unknown sub-unit returns ``None`` to avoid
false-positive diagnostics — better to under-report than over-report
while the implementation is incomplete.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from fractions import Fraction

from dimfort.core import units as _units_mod
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.lfortran import walk
from dimfort.core.units import (
    ZERO_DIM,
    Unit,
    UnitError,
    UnitTable,
    equal_dim,
    format_unit,
)

# ---------------------------------------------------------------------------
# Diagnostic codes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeSpec:
    code: str
    severity: Severity
    description: str


CODES: dict[str, CodeSpec] = {
    "H001": CodeSpec("H001", Severity.ERROR, "assignment unit mismatch"),
    "H002": CodeSpec(
        "H002", Severity.ERROR, "operands have different dimensions"
    ),
    "U002": CodeSpec(
        "U002", Severity.ERROR, "unit annotation could not be parsed"
    ),
}


# ---------------------------------------------------------------------------
# ASR helpers
# ---------------------------------------------------------------------------


def _loc_positions(node: dict) -> tuple[Position, Position]:
    loc = node.get("loc") or {}
    start = Position(loc.get("first_line", 0), loc.get("first_column", 0))
    end = Position(loc.get("last_line", 0), loc.get("last_column", 0))
    return start, end


def _node_file(node: dict) -> str:
    loc = node.get("loc") or {}
    return loc.get("first_filename", "<unknown>")


def _var_name(var_node: dict) -> str:
    """Extract the bare variable name from an ASR ``Var`` node.

    ASR encodes the symbol as e.g. ``"a (SymbolTable2)"``; we keep only
    the name.
    """
    v = var_node.get("fields", {}).get("v", "")
    return v.split(" ", 1)[0]


# ---------------------------------------------------------------------------
# Expression unit resolver
# ---------------------------------------------------------------------------


_ADDITIVE_OPS = {"Add", "Sub"}
_MUL_OP = "Mul"
_DIV_OP = "Div"
_POW_OP = "Pow"


class _Resolver:
    """Recursive unit resolver. Accumulates diagnostics in ``self.diags``."""

    def __init__(self, var_units: dict[str, Unit], table: UnitTable, file: str):
        self.var_units = var_units
        self.table = table
        self.file = file
        self.diags: list[Diagnostic] = []

    # ---- top-level dispatch ------------------------------------------------

    def resolve(self, node: dict) -> Unit | None:
        kind = node.get("node")
        if kind == "Var":
            return self.var_units.get(_var_name(node))
        if kind in ("RealConstant", "IntegerConstant"):
            # Numeric literals are dimensionless.
            return Unit(ZERO_DIM, factor=Fraction(1))
        if kind in ("RealBinOp", "IntegerBinOp"):
            return self._binop(node)
        if kind in ("RealUnaryMinus", "IntegerUnaryMinus"):
            inner = node.get("fields", {}).get("arg")
            return self.resolve(inner) if isinstance(inner, dict) else None
        # Anything else (FunctionCall, IntrinsicElementalFunction, etc.) is
        # not handled in v1 → unknown unit.
        return None

    # ---- arithmetic --------------------------------------------------------

    def _binop(self, node: dict) -> Unit | None:
        fields = node.get("fields", {})
        op = fields.get("op")
        left_node = fields.get("left")
        right_node = fields.get("right")
        if not isinstance(left_node, dict) or not isinstance(right_node, dict):
            return None

        if op in _ADDITIVE_OPS:
            return self._additive(node, left_node, right_node)
        if op == _MUL_OP:
            return self._multiplicative(left_node, right_node, divide=False)
        if op == _DIV_OP:
            return self._multiplicative(left_node, right_node, divide=True)
        if op == _POW_OP:
            return self._pow(left_node, right_node)
        return None

    def _additive(
        self, node: dict, left_node: dict, right_node: dict
    ) -> Unit | None:
        lu = self.resolve(left_node)
        ru = self.resolve(right_node)
        if lu is None or ru is None:
            return lu if lu is not None else ru
        if not equal_dim(lu, ru):
            start, end = _loc_positions(node)
            self.diags.append(
                Diagnostic(
                    file=self.file,
                    start=start,
                    end=end,
                    severity=CODES["H002"].severity,
                    code="H002",
                    message=(
                        f"'+' / '-' operands have different dimensions: "
                        f"{format_unit(lu, table=self.table)} vs "
                        f"{format_unit(ru, table=self.table)}"
                    ),
                )
            )
            return None
        return lu

    def _multiplicative(
        self, left_node: dict, right_node: dict, *, divide: bool
    ) -> Unit | None:
        lu = self.resolve(left_node)
        ru = self.resolve(right_node)
        if lu is None or ru is None:
            return None
        return lu / ru if divide else lu * ru

    def _pow(self, base_node: dict, exp_node: dict) -> Unit | None:
        base = self.resolve(base_node)
        if base is None:
            return None
        # Only integer constant exponents are supported in v1.
        if exp_node.get("node") != "IntegerConstant":
            return None
        try:
            n = int(exp_node.get("fields", {}).get("n", 0))
        except (TypeError, ValueError):
            return None
        return base.pow(n)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _walk_assignments(asr: dict) -> Iterator[dict]:
    for n in walk(asr):
        if isinstance(n, dict) and n.get("node") == "Assignment":
            yield n


def _resolve_var_units(
    raw: dict[str, str], table: UnitTable, file: str
) -> tuple[dict[str, Unit], list[Diagnostic]]:
    """Parse unit strings into :class:`Unit` objects, reporting U002 on bad ones."""
    out: dict[str, Unit] = {}
    diags: list[Diagnostic] = []
    for name, text in raw.items():
        try:
            out[name] = _units_mod.parse(text, table)
        except UnitError as exc:
            diags.append(
                Diagnostic(
                    file=file,
                    start=Position(0, 0),
                    end=Position(0, 0),
                    severity=CODES["U002"].severity,
                    code="U002",
                    message=f"unit annotation for {name!r}: {exc}",
                )
            )
    return out, diags


def check(
    asr: dict,
    var_units_text: dict[str, str],
    *,
    table: UnitTable | None = None,
    file: str | None = None,
) -> list[Diagnostic]:
    """Walk an ASR and produce homogeneity diagnostics."""
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )
    src_file = file or "<asr>"

    var_units, diags = _resolve_var_units(var_units_text, active_table, src_file)
    resolver = _Resolver(var_units, active_table, src_file)

    for asn in _walk_assignments(asr):
        target = asn.get("fields", {}).get("target")
        value = asn.get("fields", {}).get("value")
        if not isinstance(target, dict) or not isinstance(value, dict):
            continue

        lhs_unit = resolver.resolve(target)
        rhs_unit = resolver.resolve(value)
        if lhs_unit is None or rhs_unit is None:
            continue
        if not equal_dim(lhs_unit, rhs_unit):
            start, end = _loc_positions(asn)
            diags.append(
                Diagnostic(
                    file=src_file,
                    start=start,
                    end=end,
                    severity=CODES["H001"].severity,
                    code="H001",
                    message=(
                        f"assignment unit mismatch: target "
                        f"{format_unit(lhs_unit, table=active_table)} "
                        f"vs value "
                        f"{format_unit(rhs_unit, table=active_table)}"
                    ),
                )
            )

    diags.extend(resolver.diags)
    return diags
