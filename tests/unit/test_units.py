"""Tests for the unit-expression parser and algebra."""
from fractions import Fraction

import pytest

from dimfort.core import units  # noqa: F401 — ensure DEFAULT_TABLE is populated
from dimfort.core.units import (
    UnitAmbiguityWarning,
    UnitError,
    equal_dim,
    equal_strict,
    parse,
)

DIMLESS = (0, 0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize(
    "expr, dim, factor",
    [
        ("m", (0, 1, 0, 0, 0, 0, 0), 1),
        ("kg", (1, 0, 0, 0, 0, 0, 0), 1),
        ("s", (0, 0, 1, 0, 0, 0, 0), 1),
        ("1", DIMLESS, 1),
        ("rad", DIMLESS, 1),
        ("m/s", (0, 1, -1, 0, 0, 0, 0), 1),
        ("m*s", (0, 1, 1, 0, 0, 0, 0), 1),
        ("kg*m/s^2", (1, 1, -2, 0, 0, 0, 0), 1),
        ("kg/m/s", (1, -1, -1, 0, 0, 0, 0), 1),
        ("kg/(m*s)", (1, -1, -1, 0, 0, 0, 0), 1),
        # Fortran-style ``**`` power, normalised to ``^``. Must bind
        # tighter than ``/`` so ``m/s**2`` is ``m/(s**2)``, not ``(m/s)**2``.
        ("m/s**2", (0, 1, -2, 0, 0, 0, 0), 1),
        ("s**2", (0, 0, 2, 0, 0, 0, 0), 1),
        ("m/(s**2)", (0, 1, -2, 0, 0, 0, 0), 1),
        ("kg*m/s**2", (1, 1, -2, 0, 0, 0, 0), 1),
        ("m**(1/2)", (0, Fraction(1, 2), 0, 0, 0, 0, 0), 1),
        ("m^2", (0, 2, 0, 0, 0, 0, 0), 1),
        ("m^-1", (0, -1, 0, 0, 0, 0, 0), 1),
        ("m^(1/2)", (0, Fraction(1, 2), 0, 0, 0, 0, 0), 1),
        ("N", (1, 1, -2, 0, 0, 0, 0), 1),
        ("J", (1, 2, -2, 0, 0, 0, 0), 1),
        ("Pa", (1, -1, -2, 0, 0, 0, 0), 1),
        ("km", (0, 1, 0, 0, 0, 0, 0), 1000),
        ("ms", (0, 0, 1, 0, 0, 0, 0), Fraction(1, 1000)),
    ],
)
def test_parses_to_expected_dim_and_factor(expr, dim, factor):
    u = parse(expr)
    assert u.dimension == tuple(dim)
    assert u.factor == factor


def test_equal_dim_ignores_factor():
    assert equal_dim(parse("km"), parse("m"))


def test_equal_strict_distinguishes_factor():
    assert not equal_strict(parse("km"), parse("m"))
    assert equal_strict(parse("m"), parse("m"))


def test_unknown_identifier_raises():
    with pytest.raises(UnitError):
        parse("widget")


def test_decimal_exponent_rejected():
    with pytest.raises(UnitError):
        parse("m^1.5")


def test_implicit_multiplication_rejected():
    with pytest.raises(UnitError):
        parse("m s")


def test_ambiguous_slash_warns():
    with pytest.warns(UnitAmbiguityWarning):
        u = parse("kg/m*s")
    assert u.dimension == (1, -1, 1, 0, 0, 0, 0)


def test_no_warning_when_unambiguous():
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error", UnitAmbiguityWarning)
        parse("kg/s")
        parse("kg/(m*s)")
        parse("kg*m/s^2")


# ---------------------------------------------------------------------------
# Phase B sub-step 1: wrapper types, canonicalization, parser, pretty-print
# ---------------------------------------------------------------------------

from dimfort.core.units import (  # noqa: E402
    ExpWrap,
    LogWrap,
    format_unit,
    is_dimensionless,
    wrap_exp,
    wrap_log,
)


def test_wrap_log_of_regular_creates_logwrap():
    pa = parse("Pa")
    u = wrap_log(pa)
    assert isinstance(u, LogWrap)
    assert u.inner == pa


def test_wrap_exp_of_regular_creates_expwrap():
    k = parse("K")
    u = wrap_exp(k)
    assert isinstance(u, ExpWrap)
    assert u.inner == k


def test_r21_log_of_exp_cancels():
    k = parse("K")
    assert wrap_log(wrap_exp(k)) == k


def test_r22_exp_of_log_cancels():
    pa = parse("Pa")
    assert wrap_exp(wrap_log(pa)) == pa


def test_r23_log_of_dimless_collapses():
    one = parse("1")
    assert wrap_log(one) == one
    assert not isinstance(wrap_log(one), LogWrap)


def test_r23_exp_of_dimless_collapses():
    one = parse("1")
    assert wrap_exp(one) == one
    assert not isinstance(wrap_exp(one), ExpWrap)


def test_nested_log_no_cancellation():
    pa = parse("Pa")
    nested = wrap_log(wrap_log(pa))
    assert isinstance(nested, LogWrap)
    assert isinstance(nested.inner, LogWrap)
    assert nested.inner.inner == pa


def test_deep_round_trip_inverse():
    k = parse("K")
    # LOG(EXP(LOG(EXP(K)))) — peels two layers, ends at K
    assert wrap_log(wrap_exp(wrap_log(wrap_exp(k)))) == k


def test_parse_log_wrapper():
    pa = parse("Pa")
    assert parse("LOG(Pa)") == LogWrap(pa)


def test_parse_exp_wrapper():
    k = parse("K")
    assert parse("EXP(K)") == ExpWrap(k)


def test_parse_nested_wrapper():
    pa = parse("Pa")
    assert parse("LOG(LOG(Pa))") == LogWrap(LogWrap(pa))


def test_parse_wrapper_lowercase():
    # A2 — parser accepts lowercase
    assert parse("log(Pa)") == parse("LOG(Pa)")
    assert parse("exp(K)") == parse("EXP(K)")


def test_parse_wrapper_whitespace_tolerance():
    # A2 — internal whitespace
    assert parse("LOG( Pa )") == parse("LOG(Pa)")
    assert parse("LOG (Pa)") == parse("LOG(Pa)")


def test_parse_inverse_pair_cancels_on_parse():
    # A3 — canonicalization on read
    assert parse("EXP(LOG(Pa))") == parse("Pa")
    assert parse("LOG(EXP(K))") == parse("K")


def test_parse_log_of_dimless_collapses():
    assert parse("LOG(1)") == parse("1")
    assert parse("EXP(1)") == parse("1")


def test_format_wrapper_prefix():
    # The inner Unit prints via the existing base-symbol formatter,
    # which expands derived names like ``Pa`` into base SI form. Only
    # the wrapper layer is checked here.
    assert format_unit(parse("LOG(Pa)")).startswith("LOG(")
    assert format_unit(parse("LOG(Pa)")).endswith(")")


def test_format_wrapper_with_base_inner():
    # ``K`` is a base symbol so the inner round-trips cleanly through
    # the formatter — exercises wrapper nesting.
    assert format_unit(parse("EXP(K)")) == "EXP(K)"
    assert format_unit(LogWrap(LogWrap(parse("K")))) == "LOG(LOG(K))"


def test_log_bare_word_rejected():
    # P5 — parens required after LOG / EXP
    with pytest.raises(UnitError):
        parse("LOG Pa")


def test_log_without_paren_after_treated_as_unknown():
    # 'LOG' alone is not a unit identifier
    with pytest.raises(UnitError):
        parse("LOG")


def test_is_dimensionless_regular():
    assert is_dimensionless(parse("1"))
    assert not is_dimensionless(parse("Pa"))


def test_is_dimensionless_wrapper():
    # Wrappers are never dim'less after canonicalization (R2.3 collapses
    # any wrapper-of-dim'less). A constructed wrapper around a unitful
    # leaf is dimensionally distinct.
    assert not is_dimensionless(parse("LOG(Pa)"))
    assert not is_dimensionless(parse("EXP(K)"))


def test_equal_dim_recurses_into_wrappers():
    assert equal_dim(parse("LOG(Pa)"), parse("LOG(Pa)"))
    # Different inners → unequal
    assert not equal_dim(parse("LOG(Pa)"), parse("LOG(K)"))
    # Wrapper ≠ leaf
    assert not equal_dim(parse("LOG(Pa)"), parse("Pa"))
    # LogWrap ≠ ExpWrap even if inner matches
    assert not equal_dim(LogWrap(parse("Pa")), ExpWrap(parse("Pa")))


# ---------------------------------------------------------------------------
# Phase B sub-step 3: combine() dispatch for Regular + LogWrap (R4 / R5)
# ---------------------------------------------------------------------------

from dimfort.core.units import combine, power  # noqa: E402


def _u(expr):
    return parse(expr)


# R5.1 — LogWrap + LogWrap → LogWrap(inner · inner)
def test_combine_r51_log_plus_log():
    a, b = wrap_log(_u("Pa")), wrap_log(_u("Pa"))
    result, diag = combine("+", a, b)
    assert diag is None
    assert result == wrap_log(_u("Pa") * _u("Pa"))


# R5.2 — LogWrap - LogWrap → LogWrap(inner / inner); collapse via R2.3
def test_combine_r52_log_minus_log_pressure_ratio():
    a, b = wrap_log(_u("Pa")), wrap_log(_u("Pa"))
    result, diag = combine("-", a, b)
    assert diag is None
    assert result == _u("1")  # collapse


# R5.3 — LogWrap ± dim'less → LogWrap
def test_combine_r53_log_minus_dimless():
    a = wrap_log(_u("Pa"))
    result, diag = combine("-", a, _u("1"))
    assert diag is None
    assert result == a


def test_combine_r53_log_plus_dimless():
    a = wrap_log(_u("Pa"))
    result, diag = combine("+", a, _u("1"))
    assert diag is None
    assert result == a


# R5.4 — literal_k · LogWrap(U) → LogWrap(U^k)
def test_combine_r54_literal_times_log():
    a = wrap_log(_u("Pa"))
    result, diag = combine("*", _u("1"), a, a_literal=Fraction(2))
    assert diag is None
    assert result == wrap_log(_u("Pa").pow(Fraction(2)))


# R5.5 — non-literal scalar · LogWrap → D1.4
def test_combine_r55_nonliteral_times_log_errors():
    a = wrap_log(_u("Pa"))
    result, diag = combine("*", _u("1"), a)  # no a_literal/b_literal
    assert diag == "D1.4"
    assert result is None


# R5.6 — LogWrap × LogWrap → D1.2
def test_combine_r56_log_times_log_errors():
    a, b = wrap_log(_u("Pa")), wrap_log(_u("Pa"))
    result, diag = combine("*", a, b)
    assert diag == "D1.2"
    assert result is None


# R5.7 — LogWrap × non-dim'less Regular → D1.2
def test_combine_r57_log_times_unitful_errors():
    a = wrap_log(_u("Pa"))
    result, diag = combine("*", a, _u("kg"))
    assert diag == "D1.2"
    assert result is None


# R5.8 — literal 1.0 · LogWrap → LogWrap (identity case of R5.4)
def test_combine_r58_identity_times_log():
    a = wrap_log(_u("Pa"))
    result, diag = combine("*", _u("1"), a, a_literal=Fraction(1))
    assert diag is None
    assert result == a


# R5.9 — LogWrap^k for k ≠ 1 → D1.2
def test_power_r59_log_squared_errors():
    a = wrap_log(_u("Pa"))
    result, diag = power(a, parse("1"), 2)
    assert diag == "D1.2"
    assert result is None


def test_power_r59_log_to_one_is_identity():
    a = wrap_log(_u("Pa"))
    result, diag = power(a, parse("1"), 1)
    assert diag is None
    assert result == a


# R5.10 — LogWrap ± non-dim'less Regular → D1.3
def test_combine_r510_log_plus_pressure_errors():
    a = wrap_log(_u("Pa"))
    result, diag = combine("+", a, _u("Pa"))
    assert diag == "D1.3"
    assert result is None


# R7.2 — nested LogWrap addition errors via inner R5.6
def test_combine_r72_nested_log_addition_errors():
    a = wrap_log(wrap_log(_u("Pa")))
    b = wrap_log(wrap_log(_u("Pa")))
    result, diag = combine("+", a, b)
    assert diag == "D1.2"
    assert result is None


# R4.1 — Regular ± Regular dim mismatch → D1.1
def test_combine_r41_mismatch_d11():
    result, diag = combine("+", _u("Pa"), _u("K"))
    assert diag == "D1.1"
    assert result is None


# R4.3 — Regular ^ non-literal → D1.4
def test_power_r43_nonliteral_errors():
    result, diag = power(_u("m"), None, None)
    assert diag == "D1.4"
    assert result is None


# ---------------------------------------------------------------------------
# Phase B sub-step 4: ExpWrap (R6.x) + cross-cases (R7.1)
# ---------------------------------------------------------------------------


# R6.1 — ExpWrap × ExpWrap → ExpWrap(inner + inner)
def test_combine_r61_exp_product():
    a, b = wrap_exp(_u("K")), wrap_exp(_u("K"))
    result, diag = combine("*", a, b)
    assert diag is None
    assert result == wrap_exp(_u("K"))  # K + K = K


def test_combine_r61_exp_product_mismatch_d11():
    a, b = wrap_exp(_u("K")), wrap_exp(_u("Pa"))
    result, diag = combine("*", a, b)
    # Inner K + Pa mismatches per R4.1 → cascades as D1.1.
    assert diag == "D1.1"
    assert result is None


# R6.2 — ExpWrap / ExpWrap → ExpWrap(inner - inner)
def test_combine_r62_exp_quotient():
    a, b = wrap_exp(_u("K")), wrap_exp(_u("K"))
    result, diag = combine("/", a, b)
    assert diag is None
    assert result == wrap_exp(_u("K"))


# R6.3 — ExpWrap × dim'less → ExpWrap
def test_combine_r63_exp_times_dimless():
    a = wrap_exp(_u("K"))
    result, diag = combine("*", a, _u("1"))
    assert diag is None
    assert result == a


def test_combine_r63_exp_div_dimless():
    a = wrap_exp(_u("K"))
    result, diag = combine("/", a, _u("1"))
    assert diag is None
    assert result == a


# R6.4 — ExpWrap ^ literal_k → ExpWrap(k · inner)
def test_power_r64_exp_squared():
    a = wrap_exp(_u("K"))
    result, diag = power(a, parse("1"), 2)
    assert diag is None
    assert result == wrap_exp(_u("K").pow(2))


# R6.5 — ExpWrap + ExpWrap → D1.3
def test_combine_r65_exp_plus_exp_errors():
    a, b = wrap_exp(_u("K")), wrap_exp(_u("K"))
    result, diag = combine("+", a, b)
    assert diag == "D1.3"
    assert result is None


# R6.6 — ExpWrap + non-ExpWrap (non-literal) → D1.3
def test_combine_r66_exp_plus_pressure_errors():
    a, b = wrap_exp(_u("K")), _u("Pa")
    result, diag = combine("+", a, b)
    assert diag == "D1.3"
    assert result is None


# R6.7 — ExpWrap × non-dim'less Regular → D1.2
def test_combine_r67_exp_times_pressure_errors():
    a = wrap_exp(_u("K"))
    result, diag = combine("*", a, _u("Pa"))
    assert diag == "D1.2"
    assert result is None


# R7.1 — LogWrap × ExpWrap → D1.2
def test_combine_r71_log_times_exp_errors():
    a, b = wrap_log(_u("Pa")), wrap_exp(_u("K"))
    result, diag = combine("*", a, b)
    assert diag == "D1.2"
    assert result is None


def test_combine_r71_exp_div_log_errors():
    a, b = wrap_exp(_u("K")), wrap_log(_u("Pa"))
    result, diag = combine("/", a, b)
    assert diag == "D1.2"
    assert result is None


# Mixed wrapper + : LogWrap + ExpWrap goes through R6.6 path (D1.3)
def test_combine_log_plus_exp_d13():
    a, b = wrap_log(_u("Pa")), wrap_exp(_u("K"))
    result, diag = combine("+", a, b)
    assert diag == "D1.3"
    assert result is None


# ---------------------------------------------------------------------------
# Phase B follow-up: 4×4 power() table + D1.7 (exponent must be dim'less)
# ---------------------------------------------------------------------------


# Gate 1 — exponent type-check (D1.7) fires regardless of base.

def test_power_d17_rd_base_rn_exponent():
    """``2.0 ** speed`` style: dim'less base, unitful exponent → D1.7."""
    result, diag = power(_u("1"), _u("m/s"), None)
    assert diag == "D1.7"
    assert result is None


def test_power_d17_rn_base_rn_exponent():
    """``Pa ** speed``: D1.7 (regardless of base's units)."""
    result, diag = power(_u("Pa"), _u("m/s"), None)
    assert diag == "D1.7"


def test_power_d17_ln_exponent_errors():
    """Exponent is a LogWrap → D1.7."""
    result, diag = power(_u("Pa"), wrap_log(_u("Pa")), None)
    assert diag == "D1.7"


def test_power_d17_en_exponent_errors():
    """Exponent is an ExpWrap → D1.7."""
    result, diag = power(_u("Pa"), wrap_exp(_u("K")), None)
    assert diag == "D1.7"


def test_power_d17_skipped_when_exponent_unit_unknown():
    """Unknown exponent unit (unannotated variable) does NOT fire D1.7 —
    U005 on the declaration is the right signal."""
    result, diag = power(_u("Pa"), None, None)
    # Rn base with non-literal exponent → D1.4 (existing rule), not D1.7.
    assert diag == "D1.4"


# Gate 2 — base-specific rules (when exponent is dim'less).

def test_power_rd_base_non_literal_exponent_returns_rd():
    """The (α) refinement: Rd ^ non-literal-dim'less → Rd. 0·k = 0."""
    result, diag = power(_u("1"), _u("1"), None)
    assert diag is None
    assert result == _u("1")


def test_power_rd_base_literal_exponent_returns_rd():
    result, diag = power(_u("1"), _u("1"), 5)
    assert diag is None
    assert result == _u("1")


def test_power_rn_base_literal_exponent_scales():
    result, diag = power(_u("m"), _u("1"), 2)
    assert diag is None
    assert result == _u("m").pow(2)


def test_power_rn_base_non_literal_exponent_fires_d14():
    """Real Exner-pattern case: ``p ** kappa`` where kappa is non-literal."""
    result, diag = power(_u("Pa"), _u("1"), None)
    assert diag == "D1.4"


def test_power_ln_base_identity_only():
    result, diag = power(wrap_log(_u("Pa")), _u("1"), 1)
    assert diag is None
    result, diag = power(wrap_log(_u("Pa")), _u("1"), 2)
    assert diag == "D1.2"


def test_power_en_base_literal_scales_inner():
    result, diag = power(wrap_exp(_u("K")), _u("1"), 2)
    assert diag is None
    assert result == wrap_exp(_u("K").pow(2))


def test_power_en_base_non_literal_fires_d14():
    result, diag = power(wrap_exp(_u("K")), _u("1"), None)
    assert diag == "D1.4"
