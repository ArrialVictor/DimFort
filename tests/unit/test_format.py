"""Tests for the unit pretty-printer."""
from fractions import Fraction

import pytest

from dimfort.core.units import Unit, format_unit, parse


@pytest.mark.parametrize(
    "expr, pretty",
    [
        ("m", "m"),
        ("kg", "kg"),
        ("1", "1"),
        ("m/s", "m/s"),
        ("m*s", "m×s"),
        ("kg*m/s^2", "kg×m/s²"),
        ("kg*m^2/s^2", "kg×m²/s²"),
        ("kg/(m*s)", "kg/(m×s)"),
        ("m^(1/2)", "m^(1/2)"),
    ],
)
def test_pretty_default_no_factor(expr, pretty):
    u = parse(expr)
    assert format_unit(u) == pretty


def test_factor_hidden_by_default():
    u = parse("km")
    assert format_unit(u) == "m"
    assert format_unit(u, show_factor=True) == "1000×m"


def test_dimensionless_with_factor():
    u = Unit((0, 0, 0, 0, 0, 0, 0), Fraction(60))
    assert format_unit(u, show_factor=True) == "60"


def test_affine_offset_shown_by_default():
    """An absolute unit (offset != 0) appends its zero-point shift so degC is
    distinguishable from K. Rendered as a decimal, not the raw Fraction."""
    degc = parse("degC")
    assert format_unit(degc) == "K + 273.15"
    assert format_unit(degc, show_factor=True) == "K + 273.15"


def test_affine_offset_suppressible():
    """show_offset=False drops the offset so the rendering is valid @unit{}
    syntax (used in the H010 PARAMETER suggestion)."""
    assert format_unit(parse("degC"), show_offset=False) == "K"


def test_offset_zero_units_unchanged():
    """Every non-affine (offset-0) unit renders byte-identically to before —
    the offset gate keeps existing messages stable."""
    for expr in ("K", "m/s", "hPa", "kg/kg"):
        u = parse(expr)
        assert format_unit(u) == format_unit(u, show_offset=False)


def test_negative_offset_renders_with_minus():
    u = Unit(parse("K").dimension, Fraction(1), Fraction(-10))
    assert format_unit(u) == "K - 10"
