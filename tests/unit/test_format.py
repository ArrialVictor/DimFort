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
        ("m*s", "m*s"),
        ("kg*m/s^2", "kg*m/s^2"),
        ("kg*m^2/s^2", "kg*m^2/s^2"),
        ("kg/(m*s)", "kg/(m*s)"),
        ("m^(1/2)", "m^(1/2)"),
    ],
)
def test_pretty_default_no_factor(expr, pretty):
    u = parse(expr)
    assert format_unit(u) == pretty


def test_factor_hidden_by_default():
    u = parse("km")
    assert format_unit(u) == "m"
    assert format_unit(u, show_factor=True) == "1000*m"


def test_dimensionless_with_factor():
    u = Unit((0, 0, 0, 0, 0, 0, 0), Fraction(60))
    assert format_unit(u, show_factor=True) == "60"
