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
