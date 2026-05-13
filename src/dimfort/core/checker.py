"""Dimensional homogeneity checker (semantic phase).

Consumes:

- An LFortran ASR tree (the semantic view, JSON ``dict``).
- Optionally, an LFortran AST tree (the parse view, also JSON
  ``dict``) — needed to recover intrinsic function names, which ASR
  exposes only as numeric ids.
- A ``{var_name: unit_text}`` map produced by :mod:`dimfort.core.attach`.

Produces a list of :class:`Diagnostic`s.

Currently handles:

- ``H001`` — assignment LHS unit must match RHS unit.
- ``H002`` — ``+`` / ``-`` operands, or same-unit intrinsic args
  (``min``, ``max``, ``mod``, …) with mismatched dimensions.
- ``H003`` — intrinsic that requires a dimensionless argument
  (``exp``, ``log``, trigonometry, …) given a non-dimensionless one.
- Expressions: ``Var``, ``RealConstant``, ``IntegerConstant``,
  ``RealBinOp`` and ``IntegerBinOp`` (Add / Sub / Mul / Div / Pow
  with integer exponent), unary minus, and the intrinsics covered by
  the category sets below.

Not yet handled (these still resolve to "unknown unit" so checks on
the surrounding expression are silently skipped):

- User-defined function and subroutine calls (H004 — coming).
- Derived-type field access (``b%v``).
- Rational exponents in ``Pow``.
- Array operations and intent propagation.
- Generic interfaces.

An expression with any unknown sub-unit returns ``None`` to avoid
false-positive diagnostics — better to under-report than over-report
while the implementation grows.
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
    "H003": CodeSpec(
        "H003", Severity.ERROR, "intrinsic argument must be dimensionless"
    ),
    "H004": CodeSpec(
        "H004", Severity.ERROR, "function-call argument unit mismatch"
    ),
    "U002": CodeSpec(
        "U002", Severity.ERROR, "unit annotation could not be parsed"
    ),
}


@dataclass(frozen=True)
class FuncSig:
    """A user-defined function or subroutine's unit interface.

    ``arg_names[i]`` and ``arg_units[i]`` describe the i-th formal
    argument; ``arg_units[i]`` is ``None`` when that argument has no
    unit annotation (the checker then doesn't constrain the actual).
    ``return_unit`` is ``None`` for subroutines and for functions whose
    return variable carries no annotation.
    """

    arg_names: tuple[str, ...]
    arg_units: tuple[Unit | None, ...]
    return_unit: Unit | None
    is_subroutine: bool = False


# ---------------------------------------------------------------------------
# Intrinsic categories (ported from V4)
# ---------------------------------------------------------------------------

# Require dimensionless input; produce dimensionless output.
DIMENSIONLESS_INTRINSICS: frozenset[str] = frozenset({
    "exp", "log", "log10",
    "sin", "cos", "tan",
    "asin", "acos", "atan",
    "sinh", "cosh", "tanh",
})

# Raise the argument's unit to a fixed exponent. Keys are intrinsic
# names; values are the exponent to apply.
TRANSFORMING_INTRINSICS: dict[str, Fraction] = {
    "sqrt": Fraction(1, 2),
    "abs": Fraction(1),
}

# Result has the first argument's unit; remaining args (if any) don't
# constrain it. Covers kind conversions and ``sign(a, b)``.
TRANSPARENT_INTRINSICS: frozenset[str] = frozenset({
    "floor", "ceiling", "nint", "int", "real", "dble", "sign",
    "aimag", "anint",
})

# All listed args must share a unit; result has that unit. For
# ``merge(tsource, fsource, mask)`` only the first two args are
# compared (the third is logical).
SAME_UNIT_ARG_INTRINSICS: frozenset[str] = frozenset({
    "min", "max", "mod", "modulo", "merge",
})

# Result = unit_of(arg[0]) * unit_of(arg[1]).
PRODUCT_INTRINSICS: frozenset[str] = frozenset({"dot_product", "matmul"})

# Reductions over an array; result has the array element's unit.
REDUCTION_INTRINSICS: frozenset[str] = frozenset({
    "sum", "minval", "maxval",
})


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


_RATIONAL_EXPONENT_MAX_DENOMINATOR = 100


def _constant_exponent(node: dict) -> int | Fraction | None:
    """Decode an ASR exponent node into an integer or :class:`Fraction`.

    Recognised forms:

    - ``IntegerConstant`` → ``int``.
    - ``RealConstant`` whose float value is close to a rational with
      denominator ≤ 100 — converted via ``Fraction.limit_denominator``.
      A value that doesn't match any "nice" rational (e.g. ``0.314``)
      yields ``None``, so the caller treats the exponent as unknown.
    - Anything else → ``None``.
    """
    kind = node.get("node")
    fields = node.get("fields", {})
    if kind == "IntegerConstant":
        try:
            return int(fields.get("n", 0))
        except (TypeError, ValueError):
            return None
    if kind == "RealConstant":
        raw = fields.get("r")
        if not isinstance(raw, (int, float)):
            return None
        try:
            exact = Fraction(raw).limit_denominator(
                _RATIONAL_EXPONENT_MAX_DENOMINATOR
            )
        except (TypeError, ValueError, OverflowError):
            return None
        # Reject when limit_denominator drifted noticeably — protects against
        # users writing irrational-looking floats like ``a ** 0.314``.
        if abs(float(exact) - float(raw)) > 1e-9:
            return None
        if exact.denominator == 1:
            return exact.numerator
        return exact
    return None


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


_INTRINSIC_NODES = frozenset({
    "IntrinsicElementalFunction",
    "IntrinsicArrayFunction",
    "IntrinsicScalarFunction",
})


def _dimensionless() -> Unit:
    return Unit(ZERO_DIM, factor=Fraction(1))


def collect_intrinsic_names(ast: dict) -> dict[tuple[int, int], str]:
    """Build a ``{(line, column): name}`` map from AST ``FuncCallOrArray`` nodes.

    ASR identifies intrinsics by a numeric id; the human-readable name
    is only preserved in the AST. The two trees share source positions,
    so a join by ``(first_line, first_column)`` recovers names.
    """
    out: dict[tuple[int, int], str] = {}
    for n in walk(ast):
        if not isinstance(n, dict) or n.get("node") != "FuncCallOrArray":
            continue
        loc = n.get("loc") or {}
        line = loc.get("first_line")
        col = loc.get("first_column")
        func = n.get("fields", {}).get("func")
        if isinstance(line, int) and isinstance(col, int) and isinstance(func, str):
            out[(line, col)] = func
    return out


class _Resolver:
    """Recursive unit resolver. Accumulates diagnostics in ``self.diags``."""

    def __init__(
        self,
        var_units: dict[str, Unit],
        table: UnitTable,
        file: str,
        intrinsic_names: dict[tuple[int, int], str] | None = None,
        functions: dict[str, FuncSig] | None = None,
        field_units: dict[tuple[str, str], Unit] | None = None,
    ):
        self.var_units = var_units
        self.table = table
        self.file = file
        self.intrinsic_names = intrinsic_names or {}
        self.functions = functions or {}
        self.field_units = field_units or {}
        self.diags: list[Diagnostic] = []

    # ---- top-level dispatch ------------------------------------------------

    def resolve(self, node: dict) -> Unit | None:
        kind = node.get("node")
        if kind == "Var":
            return self.var_units.get(_var_name(node))
        if kind in ("RealConstant", "IntegerConstant"):
            return _dimensionless()
        if kind in ("RealBinOp", "IntegerBinOp"):
            return self._binop(node)
        if kind in ("RealUnaryMinus", "IntegerUnaryMinus"):
            inner = node.get("fields", {}).get("arg")
            return self.resolve(inner) if isinstance(inner, dict) else None
        if kind in _INTRINSIC_NODES:
            return self._intrinsic(node)
        if kind == "FunctionCall":
            return self._function_call(node)
        if kind == "StructInstanceMember":
            return self._struct_member(node)
        # Generic dispatch and a few other niche forms remain unsupported
        # → unknown unit, no diagnostic.
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
        exponent = _constant_exponent(exp_node)
        if exponent is None:
            return None
        try:
            return base.pow(exponent)
        except UnitError:
            # Rational exponent on a prefixed/scaled unit (e.g. ``(km)^0.5``).
            # We don't try to preserve the factor; treat as unknown.
            return None

    # ---- intrinsics --------------------------------------------------------

    def _intrinsic(self, node: dict) -> Unit | None:
        loc = node.get("loc") or {}
        key = (loc.get("first_line"), loc.get("first_column"))
        name = self.intrinsic_names.get(key)
        if name is None:
            return None
        args = node.get("fields", {}).get("args") or []
        arg_nodes = [a for a in args if isinstance(a, dict)]

        if name in DIMENSIONLESS_INTRINSICS:
            for a in arg_nodes:
                u = self.resolve(a)
                if u is None or equal_dim(u, _dimensionless()):
                    continue
                start, end = _loc_positions(a)
                self.diags.append(
                    Diagnostic(
                        file=self.file,
                        start=start,
                        end=end,
                        severity=CODES["H003"].severity,
                        code="H003",
                        message=(
                            f"intrinsic {name!r} requires a dimensionless "
                            f"argument; got "
                            f"{format_unit(u, table=self.table)}"
                        ),
                    )
                )
            return _dimensionless()

        if name in TRANSFORMING_INTRINSICS:
            if not arg_nodes:
                return None
            base = self.resolve(arg_nodes[0])
            if base is None:
                return None
            exp = TRANSFORMING_INTRINSICS[name]
            try:
                return base.pow(exp)
            except (UnitError, ValueError):
                return None

        if name in TRANSPARENT_INTRINSICS:
            return self.resolve(arg_nodes[0]) if arg_nodes else None

        if name in SAME_UNIT_ARG_INTRINSICS:
            compared = arg_nodes[:2] if name == "merge" else arg_nodes
            units = [self.resolve(a) for a in compared]
            known = [u for u in units if u is not None]
            if len(known) < 2:
                return known[0] if known else None
            first = known[0]
            for u in known[1:]:
                if not equal_dim(first, u):
                    start, end = _loc_positions(node)
                    self.diags.append(
                        Diagnostic(
                            file=self.file,
                            start=start,
                            end=end,
                            severity=CODES["H002"].severity,
                            code="H002",
                            message=(
                                f"intrinsic {name!r} arguments have different "
                                f"dimensions: "
                                f"{format_unit(first, table=self.table)} vs "
                                f"{format_unit(u, table=self.table)}"
                            ),
                        )
                    )
                    return None
            return first

        if name in PRODUCT_INTRINSICS:
            if len(arg_nodes) < 2:
                return None
            a = self.resolve(arg_nodes[0])
            b = self.resolve(arg_nodes[1])
            return a * b if a is not None and b is not None else None

        if name in REDUCTION_INTRINSICS:
            return self.resolve(arg_nodes[0]) if arg_nodes else None

        return None  # unknown intrinsic — keep unit unknown

    # ---- user-defined function and subroutine calls ------------------------

    def _call_name(self, node: dict) -> str:
        v = node.get("fields", {}).get("name", "")
        return v.split(" ", 1)[0] if isinstance(v, str) else ""

    def _check_call_args(
        self, call_node: dict, sig: FuncSig, name: str
    ) -> None:
        """Compare actual argument units against the formal signature."""
        actuals = call_node.get("fields", {}).get("args") or []
        for i, actual in enumerate(actuals):
            if not isinstance(actual, dict):
                continue
            # ASR wraps each argument in a `call_arg` shim — unwrap to the value.
            arg_node = actual.get("fields", {}).get("value")
            if not isinstance(arg_node, dict):
                # Omitted optional → fields.value is [] or None. Skip silently.
                continue
            if i >= len(sig.arg_units):
                continue
            formal = sig.arg_units[i]
            if formal is None:
                continue
            actual_unit = self.resolve(arg_node)
            if actual_unit is None:
                continue
            if not equal_dim(actual_unit, formal):
                start, end = _loc_positions(arg_node)
                self.diags.append(
                    Diagnostic(
                        file=self.file,
                        start=start,
                        end=end,
                        severity=CODES["H004"].severity,
                        code="H004",
                        message=(
                            f"call to {name!r}: argument {i + 1} unit "
                            f"mismatch: expected "
                            f"{format_unit(formal, table=self.table)}, "
                            f"got "
                            f"{format_unit(actual_unit, table=self.table)}"
                        ),
                    )
                )

    def _function_call(self, node: dict) -> Unit | None:
        name = self._call_name(node)
        sig = self.functions.get(name)
        if sig is None:
            return None
        self._check_call_args(node, sig, name)
        return sig.return_unit

    # ---- derived-type field access -----------------------------------------

    def _struct_member(self, node: dict) -> Unit | None:
        """Resolve ``b%field``'s unit.

        ASR encodes the qualified field as ``<index>_<typename>_<fieldname>``
        before a ``(SymbolTable<n>)`` suffix. We strip the suffix, peel
        off the leading ``<digits>_``, then try every known type-name
        prefix and look the remainder up in :attr:`field_units`.
        """
        if not self.field_units:
            return None
        m_field = node.get("fields", {}).get("m")
        if not isinstance(m_field, str):
            return None
        qualified = m_field.split(" ", 1)[0]
        # Strip the leading numeric index, e.g. "1_particle_m" → "particle_m".
        if "_" in qualified:
            head, rest = qualified.split("_", 1)
            if head.isdigit():
                qualified = rest
        # Try every known type as a prefix.
        for (type_name, field_name), unit in self.field_units.items():
            if qualified == f"{type_name}_{field_name}":
                return unit
        return None

    def check_subroutine_call(self, node: dict) -> None:
        """Same arg-unit check as a function call, but as a statement.

        Subroutines don't return a value so this is fire-and-forget; the
        diagnostics land in ``self.diags`` like everywhere else.
        """
        name = self._call_name(node)
        sig = self.functions.get(name)
        if sig is None:
            return
        self._check_call_args(node, sig, name)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _walk_assignments(asr: dict) -> Iterator[dict]:
    for n in walk(asr):
        if isinstance(n, dict) and n.get("node") == "Assignment":
            yield n


def _walk_subroutine_calls(asr: dict) -> Iterator[dict]:
    for n in walk(asr):
        if isinstance(n, dict) and n.get("node") == "SubroutineCall":
            yield n


def _var_arg_name(var_node: dict) -> str:
    """Variable name from an ASR Var-shaped node used as a formal arg."""
    if not isinstance(var_node, dict):
        return ""
    v = var_node.get("fields", {}).get("v", "")
    return v.split(" ", 1)[0] if isinstance(v, str) else ""


def collect_function_signatures(
    asr: dict, var_units: dict[str, Unit]
) -> dict[str, FuncSig]:
    """Build a ``{func_name: FuncSig}`` map by walking ASR ``Function`` /
    ``Subroutine`` nodes and reading each formal's unit out of
    ``var_units``.

    Subroutines have an empty list for ``return_var`` (LFortran convention);
    we record their signature with ``return_unit=None``.

    v1 limitation: keyed by the bare function name. Cross-scope name
    collisions are not disambiguated — last definition wins.
    """
    out: dict[str, FuncSig] = {}
    for n in walk(asr):
        if not isinstance(n, dict):
            continue
        kind = n.get("node")
        if kind not in ("Function", "Subroutine"):
            continue
        fields = n.get("fields", {})
        name = fields.get("name")
        if not isinstance(name, str):
            continue
        formal_args = fields.get("args") or []
        arg_names: list[str] = []
        arg_units: list[Unit | None] = []
        for a in formal_args:
            argname = _var_arg_name(a)
            arg_names.append(argname)
            arg_units.append(var_units.get(argname))
        rv = fields.get("return_var")
        return_unit: Unit | None = None
        # LFortran 0.63 emits subroutines as ``Function`` nodes too;
        # ``return_var = []`` (empty list) is the distinguishing marker.
        is_sub = (kind == "Subroutine") or not isinstance(rv, dict)
        if isinstance(rv, dict):
            return_unit = var_units.get(_var_arg_name(rv))
        out[name] = FuncSig(
            arg_names=tuple(arg_names),
            arg_units=tuple(arg_units),
            return_unit=return_unit,
            is_subroutine=is_sub,
        )
    return out


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
    ast: dict | None = None,
    field_units_text: dict[tuple[str, str], str] | None = None,
    functions: dict[str, FuncSig] | None = None,
    table: UnitTable | None = None,
    file: str | None = None,
) -> list[Diagnostic]:
    """Walk an ASR and produce homogeneity diagnostics.

    ``ast`` enables intrinsic checks. ``field_units_text`` enables
    derived-type ``%``-access checks. ``functions``, when supplied,
    replaces this file's signature scan — used by the multi-file
    orchestrator so callers can see signatures defined elsewhere.
    Without it, signatures are collected from ``asr`` alone.
    """
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )
    src_file = file or "<asr>"

    var_units, diags = _resolve_var_units(var_units_text, active_table, src_file)
    field_units: dict[tuple[str, str], Unit] = {}
    for (type_name, field_name), text in (field_units_text or {}).items():
        try:
            field_units[(type_name, field_name)] = _units_mod.parse(text, active_table)
        except UnitError as exc:
            diags.append(
                Diagnostic(
                    file=src_file,
                    start=Position(0, 0),
                    end=Position(0, 0),
                    severity=CODES["U002"].severity,
                    code="U002",
                    message=(
                        f"unit annotation for {type_name}%{field_name}: {exc}"
                    ),
                )
            )
    intrinsic_names = collect_intrinsic_names(ast) if ast is not None else {}
    active_functions = (
        functions if functions is not None
        else collect_function_signatures(asr, var_units)
    )
    resolver = _Resolver(
        var_units,
        active_table,
        src_file,
        intrinsic_names,
        active_functions,
        field_units,
    )

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

    # Subroutine calls are statements, not expressions, so they don't appear
    # via resolver.resolve. Walk them explicitly.
    for call in _walk_subroutine_calls(asr):
        resolver.check_subroutine_call(call)

    diags.extend(resolver.diags)
    return diags
