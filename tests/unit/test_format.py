"""Tests for the unit pretty-printer."""
from fractions import Fraction

import pytest

from dimfort.core.units import (
    Unit,
    equal_strict,
    format_unit,
    format_unit_source,
    parse,
)


@pytest.mark.parametrize(
    "expr, pretty",
    [
        ("m", "m"),
        ("kg", "kg"),
        ("1", "1"),
        ("m/s", "m·s⁻¹"),
        ("m*s", "m·s"),
        ("kg*m/s^2", "kg·m·s⁻²"),
        ("kg*m^2/s^2", "kg·m²·s⁻²"),
        ("kg/(m*s)", "kg·m⁻¹·s⁻¹"),
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


@pytest.mark.parametrize(
    "expr, source",
    [
        ("m/s", "m/s"),
        ("1/K", "1/K"),
        ("kg*m/s^2", "kg*m/s^2"),
        ("J/(kg*K)", "m^2/(s^2*K)"),
        ("Pa*s", "kg/(m*s)"),
        ("m^(1/2)", "m^(1/2)"),
    ],
)
def test_source_serializer_is_ascii(expr, source):
    # format_unit_source emits the ASCII DSL (no ·/superscripts) so the
    # result is valid @unit{} syntax — unlike the pretty format_unit.
    assert format_unit_source(parse(expr)) == source


@pytest.mark.parametrize(
    "expr",
    ["m", "m/s", "1/K", "kg*m/s^2", "J/(kg*K)", "W/(m^2*K)", "Pa*s",
     "mol/m^3", "m^(1/2)", "1/kg^(1/2)", "K"],
)
def test_source_serializer_round_trips(expr):
    # The invariant the H010 extract-to-PARAMETER quick-fix relies on:
    # parse(format_unit_source(u)) == u, so a generated @unit{} annotation
    # re-parses to the same unit. (The pretty format_unit does NOT round-trip.)
    u = parse(expr)
    assert equal_strict(parse(format_unit_source(u)), u)


def test_source_serializer_drops_affine_offset():
    # An absolute unit's zero-point shift is not expressible in @unit{}
    # syntax, so degC serializes to its base K (no "+ 273.15").
    assert format_unit_source(parse("degC")) == "K"
