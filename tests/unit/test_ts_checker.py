"""Unit tests for the tree-sitter checker.

Exercise the per-file H001-H004 path against tiny fixtures. The
integration tests in ``tests/integration/test_ast_*`` cover the
multi-file workset; here we pin down resolver corner cases that are
easy to regress when changing node-shape logic.
"""
from __future__ import annotations

from dimfort.core import (
    ts_checker,
    unit_config,  # noqa: F401  — installs DEFAULT_TABLE
)
from dimfort.core import ts_parser as ts


def _check(src: str, var_units: dict[str, str], *, scale_mode: bool = False) -> list:
    src_b = src.encode()
    tree = ts.parse_text(src_b)
    return ts_checker.check(
        tree, var_units, source=src_b, file="test.f90", scale_mode=scale_mode,
    )


def test_h001_assignment_mismatch():
    """An assignment with mismatched dimensions fires H001."""
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  a = b\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "m/s", "b": "kg"})
    codes = [d.code for d in diags]
    assert "H001" in codes


def test_h001_suppressed_for_numeric_literal_default():
    """``g = 9.81`` on a unit-bearing variable shouldn't fire H001.

    Numeric literals are dimensionless in Fortran, but assigning one
    to a variable annotated with a unit is the standard idiom for
    declaring a physical constant — the literal IS the value of the
    constant. Treating that as an error makes initialisation files
    unreadable. The suppression applies when the entire RHS is a
    pure literal or a constant expression of literals.
    """
    src = (
        "subroutine s\n"
        "  real :: g, omega\n"
        "  g = 9.81\n"
        "  omega = 2.0 * 3.14159 / 86400.0\n"
        "end subroutine\n"
    )
    diags = _check(src, {"g": "m/s^2", "omega": "1/s"})
    assert all(d.code != "H001" for d in diags), [
        (d.code, d.message) for d in diags
    ]


def test_h001_fires_when_non_literal_rhs_mismatches():
    """The literal-suppression rule must not mask actual unit errors.

    ``a = b * 2.0`` where ``b`` has the wrong unit should still fire.
    """
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  a = b * 2.0\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "m/s", "b": "kg"})
    assert any(d.code == "H001" for d in diags), [
        (d.code, d.message) for d in diags
    ]


def test_h001_passes_when_units_match():
    """An assignment whose dimensions agree (even via mul/div) emits no H001."""
    # force = mass * accel : kg * m/s² == kg·m/s²
    src = (
        "subroutine s\n"
        "  real :: m, a, f\n"
        "  f = m * a\n"
        "end subroutine\n"
    )
    diags = _check(src, {"m": "kg", "a": "m/s^2", "f": "kg*m/s^2"})
    assert all(d.code != "H001" for d in diags)


def test_h002_addition_mismatch():
    """Adding kg to m/s fires H002 at the operator."""
    src = (
        "subroutine s\n"
        "  real :: a, b, c\n"
        "  c = a + b\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "kg", "b": "m/s", "c": "kg"})
    codes = [d.code for d in diags]
    assert "H002" in codes


# ---------------------------------------------------------------------------
# H010 — implicit literal cast (D1.5)
# ---------------------------------------------------------------------------


def test_h010_literal_plus_unitful_emits_warning():
    """``1. + speed`` fires H010 (warning), not H002 (error).

    The ``1.+speed(i)`` regularization-constant pattern (common in
    drag / surface-flux routines) is dimensionally smelly but not a real
    bug — the literal has an implicit unit. H010 surfaces the smell
    at warning severity, allowing the expression to type.
    """
    src = (
        "subroutine s\n"
        "  real :: speed, result\n"
        "  result = 1. + speed\n"
        "end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "result": "m/s"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H002" not in codes


def test_h010_fires_with_literal_on_right():
    """``speed + 1.`` (literal on right) fires H010 symmetrically."""
    src = (
        "subroutine s\n"
        "  real :: speed, result\n"
        "  result = speed + 1.\n"
        "end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "result": "m/s"})
    codes = [d.code for d in diags]
    assert "H010" in codes


def test_h010_fires_on_subtraction():
    """``dt - 0.1`` fires H010; `-` is symmetric to `+` for D1.5."""
    src = (
        "subroutine s\n"
        "  real :: dt, t\n"
        "  t = dt - 0.1\n"
        "end subroutine\n"
    )
    diags = _check(src, {"dt": "s", "t": "s"})
    codes = [d.code for d in diags]
    assert "H010" in codes


def test_h010_severity_is_warning():
    """H010 is emitted at Severity.WARNING, not Severity.ERROR.

    Editor companions render Warning as yellow squiggles (not red),
    and CLI exit code is 0 even when H010 is the only diagnostic.
    """
    from dimfort.core.diagnostics import Severity
    src = (
        "subroutine s\n"
        "  real :: speed, result\n"
        "  result = 1. + speed\n"
        "end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "result": "m/s"})
    h010s = [d for d in diags if d.code == "H010"]
    assert h010s, "expected at least one H010"
    assert all(d.severity is Severity.WARNING for d in h010s)


def test_h010_does_not_fire_for_two_variables():
    """Explicit dim'less variable + unitful variable still fires H002.

    The H010 demotion is specifically for *literal* operands. A
    variable that has been explicitly annotated ``@unit{1}`` is a
    deliberate dim'less declaration — adding it to a unitful is a
    real bug, not a smell. H010 must NOT silence this case.
    """
    src = (
        "subroutine s\n"
        "  real :: count, duration, total\n"
        "  total = count + duration\n"
        "end subroutine\n"
    )
    diags = _check(src, {"count": "1", "duration": "s", "total": "s"})
    codes = [d.code for d in diags]
    assert "H010" not in codes
    assert "H002" in codes


def test_h010_does_not_fire_when_both_unitful():
    """``Pa + K`` (both unitful, both variables) still fires H002, not H010."""
    src = (
        "subroutine s\n"
        "  real :: p, t, x\n"
        "  x = p + t\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "t": "K", "x": "Pa"})
    codes = [d.code for d in diags]
    assert "H010" not in codes
    assert "H002" in codes


def test_h010_message_suggests_named_parameter():
    """The H010 message includes the hint to use a named PARAMETER."""
    src = (
        "subroutine s\n"
        "  real :: speed, result\n"
        "  result = 1. + speed\n"
        "end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "result": "m/s"})
    h010 = next(d for d in diags if d.code == "H010")
    assert "PARAMETER" in h010.message
    assert "@unit" in h010.message


def test_h003_dimensionless_intrinsic_violation():
    """SIN/COS/TAN still require dim'less input — H003 on a kg arg.

    Pre-Phase-B this test used ``exp``; ``exp`` / ``log`` now accept
    any unit via the wrapper rules (R3.1 / R3.2), so the dim'less-
    intrinsic check is verified through ``sin`` instead.
    """
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  b = sin(a)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "kg", "b": "1"})
    codes = [d.code for d in diags]
    assert "H003" in codes


def test_exp_of_unitful_types_as_expwrap():
    """Phase B: ``exp`` no longer requires a dim'less arg (R3.2).

    ``b = exp(a)`` with ``a :: kg``, ``b :: 1`` types the RHS as
    ``ExpWrap(kg)``; the assignment to ``b :: 1`` fires H001 — the
    diagnostic class shifts (H003 → H001) but the error is preserved.
    """
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  b = exp(a)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "kg", "b": "1"})
    codes = [d.code for d in diags]
    assert "H003" not in codes
    assert "H001" in codes


def test_h004_function_argument_mismatch():
    """Calling a function with a wrongly-dimensioned arg fires H004."""
    src = (
        "subroutine s\n"
        "  real :: speed, mass, momentum\n"
        "  momentum = scale(mass)\n"
        "contains\n"
        "  real function scale(v)\n"
        "    real :: v\n"
        "    scale = v\n"
        "  end function\n"
        "end subroutine\n"
    )
    # `scale(v)` expects v in m/s (annotated); passing mass (kg) → H004.
    diags = _check(
        src,
        {"speed": "m/s", "mass": "kg", "momentum": "m/s", "v": "m/s", "scale": "m/s"},
    )
    codes = [d.code for d in diags]
    assert "H004" in codes


def test_power_integer_exponent_combines_dims():
    """``a ** 2`` on a length yields length²; mismatch with target raises H001."""
    src = (
        "subroutine s\n"
        "  real :: x, area\n"
        "  area = x ** 2\n"
        "end subroutine\n"
    )
    # x is m, area is m²: OK
    diags_ok = _check(src, {"x": "m", "area": "m^2"})
    assert all(d.code != "H001" for d in diags_ok)
    # If we mis-annotate area as m, the H001 fires (m² ≠ m).
    diags_bad = _check(src, {"x": "m", "area": "m"})
    assert any(d.code == "H001" for d in diags_bad)


def test_derived_type_member_resolves_via_var_types():
    """``p%mass`` resolves through ``var_types`` + ``field_units``."""
    src = (
        "module m\n"
        "  type :: particle\n"
        "    real :: mass\n"
        "  end type\n"
        "  type(particle) :: p\n"
        "  real :: tot\n"
        "contains\n"
        "  subroutine s\n"
        "    tot = p%mass\n"
        "  end subroutine\n"
        "end module\n"
    )
    src_b = src.encode()
    tree = ts.parse_text(src_b)
    # var_units for the local: tot in kg; field_units key the type/field.
    diags = ts_checker.check(
        tree,
        {"tot": "kg"},
        source=src_b,
        file="t.f90",
        field_units={("particle", "mass"): "kg"},
    )
    # No H001 — kg = kg.
    assert all(d.code != "H001" for d in diags)


def test_h001_squiggle_spans_lhs_not_a_single_char():
    """The H001 diagnostic range must cover the LHS identifier, not just one char.

    A zero-length range gets widened to one char by the LSP layer; this
    test pins the per-checker invariant that ``start < end`` on the
    offending node.
    """
    src = (
        "subroutine s\n"
        "  real :: aaaaa, b\n"
        "  aaaaa = b\n"
        "end subroutine\n"
    )
    diags = _check(src, {"aaaaa": "m/s", "b": "kg"})
    h001 = next(d for d in diags if d.code == "H001")
    # 'aaaaa' is 5 chars on line 3, column 3-8. We require a non-zero span.
    assert h001.end.column > h001.start.column
    assert h001.end.column - h001.start.column >= 5


def test_u005_fires_when_var_used_in_assignment_without_annotation():
    """A variable used as the RHS of an assignment but not annotated triggers U005."""
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  a = b\n"
        "end subroutine\n"
    )
    # Annotate only ``a``, leave ``b`` bare. ``b`` is read on line 3
    # → U005 fires on the declaration line.
    diags = _check(src, {"a": "m/s"})
    u005 = [d for d in diags if d.code == "U005"]
    assert len(u005) == 1
    assert "'b'" in u005[0].message
    assert u005[0].start.line == 2


def test_u005_does_not_fire_when_var_only_declared():
    """A variable declared but never used in a checked expression doesn't get U005."""
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  real :: unused\n"
        "  a = b\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "m/s", "b": "m/s"})
    u005_names = [d.message for d in diags if d.code == "U005"]
    assert all("unused" not in m for m in u005_names)


def test_unsupported_expression_does_not_emit_false_positive():
    """If we can't resolve one side of an assignment, we emit nothing rather than guess."""
    # `transfer` isn't in any of our intrinsic categories, so its
    # result unit is unknown. The H001 check must skip.
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  a = transfer(b, a)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "m/s", "b": "kg"})
    assert all(d.code != "H001" for d in diags)


# ---------------------------------------------------------------------------
# Phase B sub-step 2: LOG/EXP intrinsic typing (R3.1, R3.2) + cancellation
# ---------------------------------------------------------------------------


def test_log_of_pa_types_as_logwrap():
    """``LOG(p)`` with ``p :: Pa`` types as ``LogWrap(Pa)`` (R3.1).

    Used here as the RHS of an assignment to an ``@unit{LOG(Pa)}``-
    annotated LHS — no H001 should fire.
    """
    src = (
        "subroutine s\n"
        "  real :: p, lp\n"
        "  lp = log(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "lp": "LOG(Pa)"})
    assert all(d.code != "H001" for d in diags)


def test_exp_of_log_cancels_in_assignment():
    """``EXP(LOG(p))`` ⇒ ``p`` via R2.2 cancellation — clean assignment."""
    src = (
        "subroutine s\n"
        "  real :: psol, pref\n"
        "  pref = exp(log(psol))\n"
        "end subroutine\n"
    )
    diags = _check(src, {"psol": "Pa", "pref": "Pa"})
    assert [d.code for d in diags] == []


def test_log_of_exp_cancels_in_assignment():
    """``LOG(EXP(x))`` ⇒ ``x`` via R2.1 cancellation."""
    src = (
        "subroutine s\n"
        "  real :: x, y\n"
        "  y = log(exp(x))\n"
        "end subroutine\n"
    )
    diags = _check(src, {"x": "K", "y": "K"})
    assert [d.code for d in diags] == []


def test_log_of_dimless_collapses():
    """``LOG(c)`` with ``c :: 1`` ⇒ Regular(dim'less) via R2.3.

    Assignment to a dim'less LHS is clean — no H001.
    """
    src = (
        "subroutine s\n"
        "  real :: c, r\n"
        "  r = log(c)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"c": "1", "r": "1"})
    assert [d.code for d in diags] == []


def test_log10_same_as_log():
    """LOG10 types identically to LOG per R3.3."""
    src = (
        "subroutine s\n"
        "  real :: p, lp\n"
        "  lp = log10(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "lp": "LOG(Pa)"})
    assert all(d.code != "H001" for d in diags)


def test_hydrostatic_projection_types_cleanly():
    """The hydrostatic-projection idiom should type to Pa.

    ``EXP(LOG(psol) - dgeop/RT)`` — the inner ``LOG(psol) - dim'less``
    types as ``LogWrap(Pa)`` (R5.3 absorbs the dim'less constant; in
    sub-step 2 this is the default ``return left_u`` behaviour). The
    outer ``EXP`` cancels via R2.2 → Pa. No diagnostics.
    """
    src = (
        "subroutine s\n"
        "  real :: psol, dgeop, RT, pref\n"
        "  pref = exp(log(psol) - dgeop / RT)\n"
        "end subroutine\n"
    )
    diags = _check(src, {
        "psol": "Pa", "dgeop": "m^2/s^2", "RT": "m^2/s^2", "pref": "Pa",
    })
    assert [d.code for d in diags] == []


def test_assignment_logwrap_to_matching_regular_warns_d16():
    """Pre-Phase-C this fired H001; Phase C demotes to H010 (D1.6).

    Assigning ``LOG(p)`` to a Regular Pa LHS is the implicit-untag
    case described by D1.6 — inner dimension matches LHS, so the
    assignment is allowed with a warning rather than rejected.
    """
    src = (
        "subroutine s\n"
        "  real :: p, q\n"
        "  q = log(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "q": "Pa"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H001" not in codes
    assert any("D1.6" in d.message for d in diags)


# ---------------------------------------------------------------------------
# Phase B sub-step 3: LogWrap arithmetic diagnostics (D1.2 / D1.3 / D1.4)
# ---------------------------------------------------------------------------


def test_log_times_log_emits_d12():
    src = (
        "subroutine s\n"
        "  real :: p1, p2, r\n"
        "  r = log(p1) * log(p2)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p1": "Pa", "p2": "Pa", "r": "1"})
    codes = [d.code for d in diags]
    assert "H002" in codes
    assert any("D1.2" in d.message for d in diags)


def test_log_times_unitful_emits_d12():
    src = (
        "subroutine s\n"
        "  real :: p, mass, r\n"
        "  r = log(p) * mass\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "mass": "kg", "r": "1"})
    assert any("D1.2" in d.message for d in diags)


def test_log_plus_pressure_emits_d13():
    src = (
        "subroutine s\n"
        "  real :: p, q, r\n"
        "  r = log(p) + q\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "q": "Pa", "r": "1"})
    assert any("D1.3" in d.message for d in diags)


def test_oq4_parameter_exponent_resolves_as_literal_rational():
    """OQ4: ``p ** kappa`` where ``kappa`` is a PARAMETER with a literal
    initialiser must resolve the exponent as a rational and *not* fire
    D1.4. Matches the classical Exner ``p^kappa`` pattern."""
    src = (
        "subroutine s\n"
        "  real, parameter :: kappa = 2./7.\n"
        "  real :: p, pi\n"
        "  pi = (p/1.e5) ** kappa\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "pi": "1"})
    # D1.4 used to fire here pre-OQ4; with PARAMETER-aware exponent
    # resolution, kappa is treated as the literal rational 2/7 and the
    # power resolves to Pa^(2/7), which (combined with `1.e5` cancelling
    # to dim'less ratio) matches ``pi : 1``.
    codes_and_msgs = [(d.code, d.message) for d in diags]
    assert not any("D1.4" in m for _c, m in codes_and_msgs), \
        f"D1.4 unexpectedly fired: {codes_and_msgs}"


def test_oq4_parameter_value_simple_literal():
    """OQ4 also handles a plain literal PARAMETER whose value reduces to
    a clean rational under the existing denominator cap."""
    src = (
        "subroutine s\n"
        "  real, parameter :: half = 0.5\n"
        "  real :: p, q\n"
        "  q = p ** half\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "q": "Pa^(1/2)"})
    assert not any("D1.4" in d.message for d in diags)


def test_symbolic_exponent_dimless_identifier_resolves_no_d14():
    """Step 3 of the symbolic-exponents work. A ``REAL :: kappa`` (not a
    PARAMETER) annotated dim'less, used as the exponent of ``p ** kappa``,
    should now resolve symbolically: no D1.4, result unit is Pa^kappa."""
    src = (
        "subroutine s\n"
        "  real :: kappa\n"
        "  real :: p\n"
        "  real :: r\n"
        "  r = p ** kappa\n"
        "end subroutine\n"
    )
    # ``kappa`` is dim'less, ``p`` is Pa, ``r`` is annotated Pa^kappa
    # (the symbolic unit). format_unit renders that as e.g. "kg^(kappa)
    # / (m^(kappa) × s^(2·kappa))" so we check no D1.4 surfaces.
    diags = _check(src, {"kappa": "1", "p": "Pa", "r": "Pa"})
    # No D1.4: kappa resolves as a symbol.
    assert not any("D1.4" in d.message for d in diags)
    # H001 *does* fire (r is annotated Pa, not Pa^kappa), with an
    # informative message about the symbolic exponent — not a D1.4.
    h001 = [d for d in diags if d.code == "H001"]
    assert h001, f"expected H001 for Pa^kappa ≠ Pa, got {diags}"


def test_symbolic_exponent_cancellation_homogeneous():
    """`Pa^kappa * Pa^(1-kappa) = Pa` should type-check cleanly with no
    diagnostics."""
    src = (
        "subroutine s\n"
        "  real :: kappa\n"
        "  real :: p\n"
        "  real :: r\n"
        "  r = (p ** kappa) * (p ** (1 - kappa))\n"
        "end subroutine\n"
    )
    diags = _check(src, {"kappa": "1", "p": "Pa", "r": "Pa"})
    # No diagnostics: the symbolic exponents cancel structurally.
    h_diags = [d for d in diags if d.code in ("H001", "H002")]
    assert not h_diags, f"unexpected H001/H002: {h_diags}"


def test_symbolic_exponent_non_dimless_identifier_still_fires_d17():
    """An identifier used as exponent must still be dim'less (D1.7).
    Symbolic resolution doesn't bypass that check."""
    src = (
        "subroutine s\n"
        "  real :: m\n"
        "  real :: p, r\n"
        "  r = p ** m\n"
        "end subroutine\n"
    )
    diags = _check(src, {"m": "kg", "p": "Pa", "r": "Pa"})
    # D1.7 is the exponent-must-be-dim'less rule.
    assert any("D1.7" in d.message for d in diags)


def test_oq4_falls_back_to_d14_when_no_path_resolves():
    """Sanity: when the exponent is a non-PARAMETER, *unannotated*
    identifier, neither PARAMETER lookup (OQ4) nor symbolic exponent
    resolution (Step 3) applies, and D1.4 still fires honestly.

    With Step 3 in place, an exponent annotated dim'less is resolved
    symbolically and *does not* fire D1.4 — see
    test_symbolic_exponent_dimless_identifier_resolves_no_d14."""
    src = (
        "subroutine s\n"
        "  real :: p, kappa, pi\n"
        "  pi = p ** kappa\n"
        "end subroutine\n"
    )
    # kappa is left unannotated (no @unit{} in var_units), so symbolic
    # resolution can't safely treat it as a dim'less generator either.
    diags = _check(src, {"p": "Pa", "pi": "1"})
    assert any("D1.4" in d.message for d in diags)


def test_nonlinear_scalar_times_log_emits_d14():
    """A multiplier that's dim'less but *not representable as a
    linear Exponent* (e.g. ``k**2`` or a non-linear sub-expression)
    still fires D1.4 honestly. Symbolic-LogWrap closed the linear
    case; the non-linear case stays as a tool refusal."""
    src = (
        "subroutine s\n"
        "  real :: p, k, r\n"
        "  r = (k ** 2) * log(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "k": "1", "r": "1"})
    # The multiplier ``k**2`` is dim'less but not linear in k (would be
    # k² as an Exponent term, which we explicitly reject). R5.5 fires
    # D1.4.
    assert any("D1.4" in d.message for d in diags)


def test_symbolic_logwrap_dimless_multiplier():
    """symbolic-logwrap: ``k * LOG(p)`` with k annotated dim'less now
    resolves symbolically — no D1.4. Result types as LogWrap(Pa^k)."""
    src = (
        "subroutine s\n"
        "  real :: p, k, r\n"
        "  r = k * log(p)\n"
        "end subroutine\n"
    )
    # k annotated dim'less.
    # The RHS is LogWrap(Pa^k). The LHS r is annotated LOG(Pa^k) to
    # match — if we annotated it as just LogWrap(1), there'd be an
    # honest H001 because the symbolic Pa^k ≠ 1.
    diags = _check(src, {"p": "Pa", "k": "1", "r": "LOG(Pa^k)"})
    # No D1.4.
    assert not any("D1.4" in d.message for d in diags)


def test_symbolic_logwrap_divisor_refuses_d14():
    """A symbolic divisor on a LogWrap is non-linear (1/kappa isn't a
    linear form), so the algebra refuses with D1.4 honestly."""
    src = (
        "subroutine s\n"
        "  real :: p, k, r\n"
        "  r = log(p) / k\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "k": "1", "r": "1"})
    assert any("D1.4" in d.message for d in diags)


def test_literal_scalar_times_log_no_diag():
    src = (
        "subroutine s\n"
        "  real :: p, r\n"
        "  r = 2.0 * log(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "r": "LOG(Pa^2)"})
    codes = [d.code for d in diags]
    # No H001 / H002; result LogWrap(Pa^2) matches LHS annotation.
    assert "H001" not in codes
    assert "H002" not in codes


def test_log_squared_emits_d12():
    src = (
        "subroutine s\n"
        "  real :: p, r\n"
        "  r = log(p) ** 2\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "r": "1"})
    assert any("D1.2" in d.message for d in diags)


def test_pressure_ratio_log_diff_clean():
    """LOG(p1) - LOG(p2) → 1 via R5.2 + R2.3; clean assignment to dim'less."""
    src = (
        "subroutine s\n"
        "  real :: p1, p2, ratio\n"
        "  ratio = log(p1) - log(p2)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p1": "Pa", "p2": "Pa", "ratio": "1"})
    assert [d.code for d in diags] == []


def test_log_homomorphism_addition():
    """LOG(p1) + LOG(p2) → LOG(Pa^2) via R5.1."""
    src = (
        "subroutine s\n"
        "  real :: p1, p2, lp2\n"
        "  lp2 = log(p1) + log(p2)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p1": "Pa", "p2": "Pa", "lp2": "LOG(Pa^2)"})
    assert [d.code for d in diags] == []


# ---------------------------------------------------------------------------
# Phase B sub-step 4: ExpWrap arithmetic + cross-cases
# ---------------------------------------------------------------------------


def test_exp_product_clean():
    """EXP(t1) * EXP(t2) → EXP(K) via R6.1."""
    src = (
        "subroutine s\n"
        "  real :: t1, t2, r\n"
        "  r = exp(t1) * exp(t2)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"t1": "K", "t2": "K", "r": "EXP(K)"})
    assert [d.code for d in diags] == []


def test_exp_plus_exp_emits_d13():
    src = (
        "subroutine s\n"
        "  real :: x, y, r\n"
        "  r = exp(x) + exp(y)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"x": "K", "y": "K", "r": "1"})
    assert any("D1.3" in d.message for d in diags)


def test_exp_plus_literal_warns_h010():
    """R6.6 D1.5 demotion: EXP(x) + 1.0 → H010 warning."""
    src = (
        "subroutine s\n"
        "  real :: x, r\n"
        "  r = exp(x) + 1.0\n"
        "end subroutine\n"
    )
    diags = _check(src, {"x": "K", "r": "EXP(K)"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H002" not in codes


def test_exp_times_pressure_emits_d12():
    src = (
        "subroutine s\n"
        "  real :: t, p, r\n"
        "  r = exp(t) * p\n"
        "end subroutine\n"
    )
    diags = _check(src, {"t": "K", "p": "Pa", "r": "1"})
    assert any("D1.2" in d.message for d in diags)


def test_log_times_exp_emits_d12():
    """R7.1 — LogWrap × ExpWrap is undefined."""
    src = (
        "subroutine s\n"
        "  real :: p, t, r\n"
        "  r = log(p) * exp(t)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "t": "K", "r": "1"})
    assert any("D1.2" in d.message for d in diags)


def test_magnus_formula_no_diag():
    """Magnus-shape: ``e0 * EXP(dim'less)`` types as Pa via R2.3 + R4.2.

    ``EXP(dim'less)`` collapses to ``Regular(dim'less)`` per R2.3; the
    outer product is then ``Pa × 1 = Pa`` via R4.2.
    """
    src = (
        "subroutine s\n"
        "  real :: e0, ratio, p\n"
        "  p = e0 * exp(ratio)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"e0": "Pa", "ratio": "1", "p": "Pa"})
    assert [d.code for d in diags] == []


# ---------------------------------------------------------------------------
# Phase C: D1.6 — implicit wrapper untag at assignment
# ---------------------------------------------------------------------------


def test_d16_logwrap_untag_to_regular_warns_h010():
    """LHS Pa, RHS LOG(p) where p :: Pa — H010 (D1.6), not H001."""
    src = (
        "subroutine s\n"
        "  real :: p, q\n"
        "  q = log(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "q": "Pa"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H001" not in codes
    assert any("D1.6" in d.message for d in diags)


def test_d16_expwrap_untag_to_regular_warns_h010():
    """LHS K, RHS EXP(t) where t :: K — H010 (D1.6), not H001."""
    src = (
        "subroutine s\n"
        "  real :: t, u\n"
        "  u = exp(t)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"t": "K", "u": "K"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H001" not in codes
    assert any("D1.6" in d.message for d in diags)


def test_d16_inner_dim_mismatch_still_h001():
    """LHS Pa, RHS LOG(t) where t :: K — H001 (inner dim mismatch)."""
    src = (
        "subroutine s\n"
        "  real :: t, q\n"
        "  q = log(t)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"t": "K", "q": "Pa"})
    codes = [d.code for d in diags]
    assert "H001" in codes
    assert "H010" not in codes


def test_d16_matching_wrapper_lhs_no_diag():
    """LHS LOG(Pa), RHS LOG(p) — exact match, no diagnostic."""
    src = (
        "subroutine s\n"
        "  real :: p, lp\n"
        "  lp = log(p)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "lp": "LOG(Pa)"})
    assert [d.code for d in diags] == []


# ---------------------------------------------------------------------------
# Literal 0 is dimension-agnostic; MAX/MIN autocast + warn like +/-
# ---------------------------------------------------------------------------


def test_literal_zero_plus_unitful_is_silent():
    """``0. + speed`` emits nothing — 0 is the additive identity in every
    dimension, so it adopts the sibling unit silently (no H010 smell)."""
    src = (
        "subroutine s\n"
        "  real :: speed, result\n"
        "  result = 0. + speed\n"
        "end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "result": "m/s"})
    assert [d.code for d in diags] == []


def test_max_with_literal_zero_is_silent():
    """``max(0., qq)`` emits nothing (the canonical clamp idiom)."""
    src = (
        "subroutine s\n"
        "  real :: qq, result\n"
        "  result = max(0., qq)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"qq": "m/s", "result": "m/s"})
    assert [d.code for d in diags] == []


def test_max_with_nonzero_literal_warns_h010():
    """``max(0.5, qq)`` autocasts the literal to the sibling unit and warns
    (H010), rather than hard-erroring — mirrors the ``+`` behavior."""
    src = (
        "subroutine s\n"
        "  real :: qq, result\n"
        "  result = max(0.5, qq)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"qq": "m/s", "result": "m/s"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H002" not in codes
    assert "H001" not in codes


def test_max_with_enotation_literal_warns_h010():
    """An E-notation literal (``2.546E-5``) is still a numeric literal —
    detect it structurally, so it warns H010 (not H002)."""
    src = (
        "subroutine s\n"
        "  real :: coriol, result\n"
        "  result = max(coriol, 2.546E-5)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"coriol": "1/s", "result": "1/s"})
    codes = [d.code for d in diags]
    assert "H010" in codes
    assert "H002" not in codes


def test_max_with_mismatched_units_fires_h002():
    """``max(mass, speed)`` (two genuinely dimensioned, disagreeing args)
    fires H002 — MAX/MIN now validate operands."""
    src = (
        "subroutine s\n"
        "  real :: mass, speed, result\n"
        "  result = max(mass, speed)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"mass": "kg", "speed": "m/s", "result": "kg"})
    codes = [d.code for d in diags]
    assert "H002" in codes


# ---------------------------------------------------------------------------
# S001 — opt-in multiplicative-scale checking (scale_mode)
# ---------------------------------------------------------------------------


def test_scale_assignment_fires_s001_when_enabled():
    """``a[m] = b[km]`` — same dimension, factor 1 vs 1000 → S001 when
    scale_mode is on (a missing/untyped conversion)."""
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  a = b\n"
        "end subroutine\n"
    )
    codes = [d.code for d in _check(src, {"a": "m", "b": "km"}, scale_mode=True)]
    assert "S001" in codes
    assert "H001" not in codes  # dims agree, so it's scale not dimension


def test_scale_off_by_default_is_silent():
    """Same code with scale_mode off (the default) → no S001; dimension-only
    behaviour is byte-for-byte unchanged."""
    codes = [d.code for d in _check(
        "subroutine s\n  real :: a, b\n  a = b\nend subroutine\n",
        {"a": "m", "b": "km"},
    )]
    assert "S001" not in codes
    assert codes == []  # a[m] = b[km] is dim-homogeneous → silent today


def test_scale_operand_fires_s001():
    """``c = a + b`` with a[m] + b[km] → S001 on the + operand."""
    src = (
        "subroutine s\n"
        "  real :: a, b, c\n"
        "  c = a + b\n"
        "end subroutine\n"
    )
    codes = [d.code for d in _check(
        src, {"a": "m", "b": "km", "c": "m"}, scale_mode=True,
    )]
    assert "S001" in codes


def test_scale_matching_factor_no_s001():
    """``a[m] = b[m]`` — identical factor → no S001 even with scale on."""
    codes = [d.code for d in _check(
        "subroutine s\n  real :: a, b\n  a = b\nend subroutine\n",
        {"a": "m", "b": "m"}, scale_mode=True,
    )]
    assert "S001" not in codes


def test_scale_dim_mismatch_is_h001_not_s001():
    """``a[m] = b[s]`` — different dimension → H001 (dimension), never S001."""
    codes = [d.code for d in _check(
        "subroutine s\n  real :: a, b\n  a = b\nend subroutine\n",
        {"a": "m", "b": "s"}, scale_mode=True,
    )]
    assert "H001" in codes
    assert "S001" not in codes


# ---------------------------------------------------------------------------
# S002 — opt-in affine-offset checking (Phase 2, °C/K)
# ---------------------------------------------------------------------------


def _s002_src(stmt: str) -> str:
    return (
        "subroutine s\n"
        "  real :: t_k, t_c, t_c2, dt, prod\n"
        f"  {stmt}\n"
        "end subroutine\n"
    )


_TEMP_UNITS = {"t_k": "K", "t_c": "degC", "t_c2": "degC", "dt": "K", "prod": "K"}


def _codes(stmt, *, scale_mode=True):
    return [d.code for d in _check(_s002_src(stmt), _TEMP_UNITS, scale_mode=scale_mode)]


def test_s002_assignment_missing_conversion():
    """``t_k[K] = t_c[degC]`` — same dim+factor, offset differs → S002 (path 1)."""
    codes = _codes("t_k = t_c")
    assert "S002" in codes
    assert "H001" not in codes  # dims agree → not a dimension error


def test_s002_point_plus_point():
    """``t_c[degC] + t_c2[degC]`` — adding two absolutes → S002 (path 2)."""
    assert "S002" in _codes("t_c2 = t_c + t_c2")


def test_s002_point_minus_point_is_silent():
    """``t_c[degC] - t_c2[degC]`` → a difference (offset 0); assigning it to a
    K slot is clean. Must NOT fire — pins the legal point−point case."""
    assert "S002" not in _codes("dt = t_c - t_c2")


def test_s002_point_plus_vector_is_silent():
    """``t_c[degC] + dt[K]`` (absolute + difference) → degC, legal at the +."""
    assert "S002" not in _codes("t_c2 = t_c + dt")


def test_s002_scaling_an_absolute():
    """``2.0 * t_c[degC]`` — scaling an absolute temperature → S002 (path 2)."""
    assert "S002" in _codes("prod = 2.0 * t_c")


def test_s002_off_by_default_is_silent():
    """All the above are silent with scale_mode off — dimension-only, the
    byte-identical guarantee (everything here is dim-homogeneous)."""
    for stmt in ("t_k = t_c", "t_c2 = t_c + t_c2", "prod = 2.0 * t_c"):
        assert "S002" not in _codes(stmt, scale_mode=False)


def test_s002_matching_offset_no_s002():
    """``t_k[K] = dt[K]`` — same offset (both 0) → no S002 even with scale on."""
    assert "S002" not in _codes("t_k = dt")


def test_s002_untyped_literal_conversion_fires():
    """Documented caveat (§3.3): ``t_k[K] = t_c[degC] + 273.15`` still fires
    S002 — a bare literal can't carry the additive offset, so the RHS stays
    degC and mismatches the K target. Pins the limitation, not a bug."""
    assert "S002" in _codes("t_k = t_c + 273.15")


def test_abs_preserves_logwrap():
    """``abs(log(x))`` must preserve LogWrap. Previously abs was
    classified TRANSFORMING and routed through pow(1), which rejected
    non-Unit operands — silently turning the wrapper into ``None`` and
    masking downstream H001 detection. abs is now TRANSPARENT."""
    src = (
        "subroutine s\n"
        "  real :: p\n"
        "  real :: l\n"
        "  l = abs(log(p))\n"
        "end subroutine\n"
    )
    diags = _check(src, {"p": "Pa", "l": "LOG(Pa)"})
    # Must NOT fire H001 — abs(log(p)) = log(p) = LogWrap(Pa),
    # which matches l's annotated LOG(Pa).
    assert all(d.code != "H001" for d in diags), [
        (d.code, d.message) for d in diags
    ]


def test_abs_preserves_concrete_unit():
    """``abs(kg)`` returns kg — sanity check that the TRANSPARENT
    reclassification doesn't break the common case."""
    src = (
        "subroutine s\n"
        "  real :: x\n"
        "  real :: y\n"
        "  y = abs(x)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"x": "kg", "y": "kg"})
    assert all(d.code != "H001" for d in diags), [
        (d.code, d.message) for d in diags
    ]
