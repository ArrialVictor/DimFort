"""AST-only unit checker — Phase 0 + Phase 1.

Walks LFortran's AST (no ASR) and emits the H001–H004 family of
diagnostics. Reads variable units from an already-attached
``var_units`` table (produced by ``core.annotations`` +
``core.attach``).

Currently supported expression nodes (single file, no cross-file
``use``-chain resolution yet):
- ``Name``, ``Num`` (integer), ``Real`` (float)
- ``BinOp``: ``Add``, ``Sub``, ``Mul``, ``Div``, ``Pow`` (constant exponent)
- ``UnaryMinus``
- ``FuncCallOrArray``: intrinsic dispatch (six categories) and
  user-defined function calls against a signature table built from
  the same AST.
- ``Assignment``, ``SubroutineCall`` (as top-level statements).

Intrinsic categories are re-used verbatim from ``core.checker`` — no
need to duplicate the data tables.

Out of scope until Phase 2+: cross-file ``use``-chain resolution,
derived-type member access, kind casts, ``Cast`` insertion, array
sections, intrinsics with non-trivial unit semantics beyond the six
categories. Anything unhandled returns ``None`` and the check silently
no-ops on that expression — see ``docs/ast-only-design.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable

from dimfort.core import units as _units_mod
from dimfort.core.checker import (
    DIMENSIONLESS_INTRINSICS,
    FuncSig,
    PRODUCT_INTRINSICS,
    REDUCTION_INTRINSICS,
    SAME_UNIT_ARG_INTRINSICS,
    TRANSFORMING_INTRINSICS,
    TRANSPARENT_INTRINSICS,
)
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.units import Unit, UnitError, UnitTable, equal_dim, format_unit


_RATIONAL_EXPONENT_MAX_DENOMINATOR = 100


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    """Static context for one file's check pass."""

    file: str
    var_units: dict[str, Unit]
    table: UnitTable
    signatures: dict[str, FuncSig]


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


def _constant_exponent_ast(node: object) -> int | Fraction | None:
    """Decode an AST exponent expression into an integer or :class:`Fraction`.

    AST integer literals are ``Num`` nodes with an ``n`` int field; real
    literals are ``Real`` nodes with ``n`` as a string. Anything else
    is "unknown" (so the caller treats the power as unresolved).
    """
    kind = _node(node)
    fields = _fields(node)
    if kind == "Num":
        try:
            return int(fields.get("n", 0))
        except (TypeError, ValueError):
            return None
    if kind == "Real":
        try:
            value = float(fields.get("n", "0"))
        except (TypeError, ValueError):
            return None
        try:
            exact = Fraction(value).limit_denominator(
                _RATIONAL_EXPONENT_MAX_DENOMINATOR
            )
        except (TypeError, ValueError, OverflowError):
            return None
        # Reject when limit_denominator drifted far from the literal —
        # protects against arbitrary floats like 0.314 being misread as
        # 157/500.
        if abs(float(exact) - value) > 1e-6:
            return None
        return exact
    return None


def _fnarg_expr(arg: object) -> object | None:
    """Extract the expression out of a ``fnarg`` wrapper. ``fnarg`` is
    LFortran's positional/keyword argument container; the actual
    expression hides in ``fields.end`` when the arg is not an array
    section."""
    if _node(arg) != "fnarg":
        return arg
    fields = _fields(arg)
    end = fields.get("end")
    if isinstance(end, dict):
        return end
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

    if kind in ("Num", "Real"):
        # Numeric literals are dimensionless. We model that as the
        # neutral element of the unit algebra ("1").
        return _units_mod.parse("1", ctx.table)

    if kind == "UnaryMinus":
        return _resolve(fields.get("operand"), ctx)

    if kind == "BinOp":
        op = fields.get("op")
        if op == "Pow":
            base = _resolve(fields.get("left"), ctx)
            if base is None:
                return None
            exponent = _constant_exponent_ast(fields.get("right"))
            if exponent is None:
                return None
            try:
                return base ** exponent  # type: ignore[arg-type]
            except Exception:
                return None
        left = _resolve(fields.get("left"), ctx)
        right = _resolve(fields.get("right"), ctx)
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
        return None

    if kind == "FuncCallOrArray":
        return _resolve_call(expr, ctx)

    # Unsupported node kind. Returning None lets the caller skip the
    # check rather than emit a false positive.
    return None


def _resolve_call(expr: object, ctx: _Ctx) -> Unit | None:
    """Resolve a ``FuncCallOrArray`` node's result unit.

    Dispatches in order: intrinsic categories first, then the
    user-defined signature table. Anything unmatched returns ``None``
    — could be an array index expression, an unsupported intrinsic,
    or a call to a function defined elsewhere.
    """
    fields = _fields(expr)
    name = fields.get("func")
    if not isinstance(name, str):
        return None
    name_lc = name.lower()
    arg_exprs = [_fnarg_expr(a) for a in (fields.get("args") or [])]

    # H003-class intrinsics: dimensionless in, dimensionless out.
    if name_lc in DIMENSIONLESS_INTRINSICS:
        return _units_mod.parse("1", ctx.table)

    if name_lc in TRANSFORMING_INTRINSICS:
        if not arg_exprs:
            return None
        base = _resolve(arg_exprs[0], ctx)
        if base is None:
            return None
        exp = TRANSFORMING_INTRINSICS[name_lc]
        try:
            return base ** exp  # type: ignore[arg-type]
        except Exception:
            return None

    if name_lc in TRANSPARENT_INTRINSICS:
        if not arg_exprs:
            return None
        return _resolve(arg_exprs[0], ctx)

    if name_lc in SAME_UNIT_ARG_INTRINSICS:
        if not arg_exprs:
            return None
        return _resolve(arg_exprs[0], ctx)

    if name_lc in PRODUCT_INTRINSICS:
        if len(arg_exprs) < 2:
            return None
        a = _resolve(arg_exprs[0], ctx)
        b = _resolve(arg_exprs[1], ctx)
        if a is None or b is None:
            return None
        return a * b

    if name_lc in REDUCTION_INTRINSICS:
        if not arg_exprs:
            return None
        return _resolve(arg_exprs[0], ctx)

    # User-defined function call.
    sig = ctx.signatures.get(name_lc)
    if sig is not None and not sig.is_subroutine:
        return sig.return_unit

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


def _emit_h003(
    call_loc: dict | None, intrinsic: str, arg_unit: Unit, ctx: _Ctx,
) -> Diagnostic:
    pos = _loc_position(call_loc)
    return Diagnostic(
        file=ctx.file,
        start=pos,
        end=pos,
        severity=Severity.ERROR,
        code="H003",
        message=(
            f"Intrinsic '{intrinsic}' requires a dimensionless argument; "
            f"got {format_unit(arg_unit)}"
        ),
    )


def _emit_h004(
    call_loc: dict | None,
    func_name: str,
    arg_index: int,
    expected: Unit,
    actual: Unit,
    ctx: _Ctx,
) -> Diagnostic:
    pos = _loc_position(call_loc)
    return Diagnostic(
        file=ctx.file,
        start=pos,
        end=pos,
        severity=Severity.ERROR,
        code="H004",
        message=(
            f"Call to '{func_name}': argument {arg_index + 1} unit mismatch: "
            f"expected {format_unit(expected)}, got {format_unit(actual)}"
        ),
    )


def _walk_expressions(expr: object, ctx: _Ctx) -> Iterable[Diagnostic]:
    """Recursive walk of an expression sub-tree, yielding H002/H003/H004
    for any operator or call mismatch encountered."""
    kind = _node(expr)

    if kind == "BinOp":
        fields = _fields(expr)
        left = fields.get("left")
        right = fields.get("right")
        yield from _walk_expressions(left, ctx)
        yield from _walk_expressions(right, ctx)
        op = fields.get("op")
        if op in ("Add", "Sub"):
            lu = _resolve(left, ctx)
            ru = _resolve(right, ctx)
            if lu is not None and ru is not None and not equal_dim(lu, ru):
                yield _emit_h002(_node_loc(expr), lu, ru, ctx)
        return

    if kind == "FuncCallOrArray":
        yield from _check_call(expr, ctx)
        # Recurse into args too — they can themselves contain calls etc.
        fields = _fields(expr)
        for a in fields.get("args") or []:
            yield from _walk_expressions(_fnarg_expr(a), ctx)
        return

    # Default: recurse generically.
    if isinstance(expr, dict):
        for v in expr.values():
            yield from _walk_expressions(v, ctx)
    elif isinstance(expr, list):
        for v in expr:
            yield from _walk_expressions(v, ctx)


def _check_call(call_node: object, ctx: _Ctx) -> Iterable[Diagnostic]:
    """Emit H003 (dimensionless-intrinsic violation) and H004 (user-call
    argument mismatch) for a single ``FuncCallOrArray``."""
    fields = _fields(call_node)
    name = fields.get("func")
    if not isinstance(name, str):
        return
    name_lc = name.lower()
    arg_exprs = [_fnarg_expr(a) for a in (fields.get("args") or [])]

    # H003 — intrinsics that require dimensionless arguments.
    if name_lc in DIMENSIONLESS_INTRINSICS:
        if not arg_exprs:
            return
        u = _resolve(arg_exprs[0], ctx)
        if u is None:
            return
        try:
            one = _units_mod.parse("1", ctx.table)
        except UnitError:
            return
        if not equal_dim(u, one):
            yield _emit_h003(_node_loc(call_node), name_lc, u, ctx)
        return

    # H004 — call to a user-defined function with mismatched arg units.
    sig = ctx.signatures.get(name_lc)
    if sig is None or sig.is_subroutine:
        return
    yield from _check_call_args_against_sig(
        sig, name_lc, arg_exprs, _node_loc(call_node), ctx
    )


def _check_call_args_against_sig(
    sig: FuncSig,
    func_name: str,
    arg_exprs: list,
    call_loc: dict | None,
    ctx: _Ctx,
) -> Iterable[Diagnostic]:
    for i, (expected, actual_expr) in enumerate(zip(sig.arg_units, arg_exprs)):
        if expected is None or actual_expr is None:
            continue
        actual = _resolve(actual_expr, ctx)
        if actual is None:
            continue
        if not equal_dim(actual, expected):
            yield _emit_h004(call_loc, func_name, i, expected, actual, ctx)


# ---------------------------------------------------------------------------
# Signature collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleExports:
    """Public surface of one Fortran module, ready to splice into a
    consumer file's local scope via a ``use`` clause.

    Variable units and function/subroutine signatures are keyed by the
    name LFortran reports — lower-case for ``signatures`` (matching the
    convention in ``collect_function_signatures``); original case for
    ``var_units`` to keep parity with how scan/attach surfaces them.
    Phase 2 treats every module-level declaration as exported (no
    ``private`` honouring yet) — refinement in a later phase.
    """

    name: str
    var_units: dict[str, Unit]
    signatures: dict[str, FuncSig]


def collect_module_exports(
    ast: dict,
    var_units: dict[str, Unit],
) -> dict[str, ModuleExports]:
    """Walk an AST and return ``{module_name_lower: ModuleExports}``.

    For each ``Module`` node, gathers:
      - Module-level variable names (their ``var_unit`` if annotated).
      - ``Function`` and ``Subroutine`` definitions found in
        ``module.fields.contains``.

    The shared ``var_units`` table already covers both module-level
    names and names declared inside contained subprograms, so we
    filter against the per-module variable name list to avoid leaking
    contained-procedure locals into the export surface.
    """
    from dimfort.core.lfortran import walk
    out: dict[str, ModuleExports] = {}
    for node in walk(ast):
        if _node(node) != "Module":
            continue
        fields = _fields(node)
        name = fields.get("name")
        if not isinstance(name, str):
            continue

        # Module-level variables: walk the ``decl`` list and pick out
        # ``var_sym`` names from each Declaration.
        export_var_units: dict[str, Unit] = {}
        for decl in fields.get("decl") or []:
            if _node(decl) != "Declaration":
                continue
            for sym in _fields(decl).get("syms") or []:
                sym_name = _fields(sym).get("name")
                if isinstance(sym_name, str) and sym_name in var_units:
                    export_var_units[sym_name] = var_units[sym_name]

        # Contained procedures: reuse the function-signature collector
        # but scope it to this module's `contains` block only.
        contained_ast = {
            "node": "TranslationUnit",
            "fields": {"items": fields.get("contains") or []},
        }
        signatures = collect_function_signatures(contained_ast, var_units)

        out[name.lower()] = ModuleExports(
            name=name,
            var_units=export_var_units,
            signatures=signatures,
        )
    return out


def apply_use_clauses(
    uses: tuple,
    module_exports: dict[str, ModuleExports],
    base_var_units: dict[str, Unit],
    base_signatures: dict[str, FuncSig],
) -> tuple[dict[str, Unit], dict[str, FuncSig], frozenset[str]]:
    """Merge imported symbols into a file's scope.

    ``uses`` is the tuple of :class:`workspace_index.UseRef` produced
    by ``extract_uses``. Local declarations always win over imports
    (no shadow warning at this phase). Returns the merged
    ``(var_units, signatures)`` tables plus the set of module names
    referenced by ``use`` that we couldn't resolve — the caller can
    surface those as U007.
    """
    var_units = dict(base_var_units)
    signatures = dict(base_signatures)
    unresolved: set[str] = set()
    for use in uses:
        mod_name = use.module.lower()
        exports = module_exports.get(mod_name)
        if exports is None:
            unresolved.add(mod_name)
            continue

        # Build the in-scope (local_name, remote_name) pairs.
        if use.only is None:
            pairs = [(n, n) for n in exports.var_units]
            pairs.extend((n, n) for n in exports.signatures)
        else:
            # ``only`` already lower-cased; expand renames first, then
            # plain names. ``renames`` is the authoritative map for
            # any locally-renamed import.
            rename_map = {local: remote for local, remote in use.renames}
            pairs = []
            for local in use.only:
                remote = rename_map.get(local, local)
                pairs.append((local, remote))

        for local, remote in pairs:
            if local in base_var_units:
                continue  # local declaration wins
            # Variable lookup is case-sensitive against export keys;
            # try the lower-cased name too as a fallback because the
            # scanner reports names verbatim while ``use`` syntax is
            # case-insensitive in F90.
            if remote in exports.var_units:
                var_units.setdefault(local, exports.var_units[remote])
            else:
                for k, v in exports.var_units.items():
                    if k.lower() == remote:
                        var_units.setdefault(local, v)
                        break
            # Signatures are stored lower-cased; ``remote`` is already
            # lower from extract_uses.
            sig = exports.signatures.get(remote)
            if sig is not None:
                signatures.setdefault(local.lower(), sig)
    return var_units, signatures, frozenset(unresolved)


def collect_function_signatures(
    ast: dict,
    var_units: dict[str, Unit],
) -> dict[str, FuncSig]:
    """Walk an AST and return ``{name_lower: FuncSig}`` for every
    ``Function`` and ``Subroutine`` defined in it.

    Argument and return units come from the file-level ``var_units``
    table (already produced by the annotation/attach pipeline). If a
    formal arg or return var carries no annotation, the corresponding
    slot is ``None`` and the checker treats it as unconstrained.
    """
    from dimfort.core.lfortran import walk
    out: dict[str, FuncSig] = {}
    for node in walk(ast):
        kind = _node(node)
        if kind not in ("Function", "Subroutine"):
            continue
        fields = _fields(node)
        name = fields.get("name")
        if not isinstance(name, str):
            continue

        arg_names: list[str] = []
        arg_units: list[Unit | None] = []
        for arg in fields.get("args") or []:
            arg_name = _fields(arg).get("arg")
            if not isinstance(arg_name, str):
                continue
            arg_names.append(arg_name)
            arg_units.append(var_units.get(arg_name))

        return_unit: Unit | None = None
        is_subroutine = kind == "Subroutine"
        if not is_subroutine:
            ret = fields.get("return_var")
            ret_name = _fields(ret).get("id") if isinstance(ret, dict) else None
            if isinstance(ret_name, str):
                return_unit = var_units.get(ret_name)
            else:
                # When no explicit ``result`` clause, F90 implicitly
                # uses the function's own name as the result variable.
                return_unit = var_units.get(name)

        out[name.lower()] = FuncSig(
            arg_names=tuple(arg_names),
            arg_units=tuple(arg_units),
            return_unit=return_unit,
            is_subroutine=is_subroutine,
        )
    return out


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def check(
    ast: dict,
    var_units: dict[str, str],
    *,
    file: str | Path,
    table: UnitTable | None = None,
    signatures: dict[str, FuncSig] | None = None,
) -> list[Diagnostic]:
    """Run the AST checker over one file's AST.

    ``var_units`` is the same string-keyed table the ASR checker takes:
    ``{varname: unit_text}``. Strings that don't parse are dropped
    silently (we'd already have flagged them as U002 in the attach
    phase).

    ``signatures`` is the user-defined function/subroutine signature
    table. ``None`` means "build it from this file's own AST" — that's
    the single-file Phase 1 mode. Pass a wider map (e.g. one merged
    across an entire workset) once cross-file Phase 2 lands.
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

    if signatures is None:
        signatures = collect_function_signatures(ast, parsed)

    ctx = _Ctx(
        file=str(file),
        var_units=parsed,
        table=active_table,
        signatures=signatures,
    )
    out: list[Diagnostic] = []

    from dimfort.core.lfortran import walk
    for node in walk(ast):
        kind = _node(node)

        if kind == "Assignment":
            fields = _fields(node)
            target = fields.get("target")
            value = fields.get("value")

            out.extend(_walk_expressions(value, ctx))

            target_unit = _resolve(target, ctx)
            rhs_unit = _resolve(value, ctx)
            if target_unit is None or rhs_unit is None:
                continue
            if not equal_dim(target_unit, rhs_unit):
                out.append(
                    _emit_h001(_node_loc(target), target_unit, rhs_unit, ctx)
                )
            continue

        if kind == "SubroutineCall":
            fields = _fields(node)
            name = fields.get("name")
            if not isinstance(name, str):
                continue
            name_lc = name.lower()
            arg_exprs = [_fnarg_expr(a) for a in (fields.get("args") or [])]
            # Recurse into args (they may contain expressions to check).
            for a in arg_exprs:
                out.extend(_walk_expressions(a, ctx))
            sig = ctx.signatures.get(name_lc)
            if sig is None or not sig.is_subroutine:
                continue
            out.extend(
                _check_call_args_against_sig(
                    sig, name_lc, arg_exprs, _node_loc(node), ctx
                )
            )

    return out
