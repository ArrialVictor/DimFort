"""Coverage for the 0.2.7 ``parse_exp`` widening.

Two design sources land in one parser change:

- ``docs/design/shipped/permissive-unit-lexer.md`` §3.0 — strict ``^``
  accepts all four integer-exponent shapes (``^N``, ``^-N``,
  ``^(N)``, ``^(-N)``); ships unconditionally, no flag required.
- ``docs/design/shipped/symbolic-exponent-annotations.md`` — closes
  the parked gap between the :class:`Exponent` algebra (shipped
  2026-05-22) and the annotation-surface parser. Surface now
  accepts bare identifiers, paren'd identifiers, and paren'd linear
  forms over Q with identifier generators.

Both halves share ``parse_exp`` as their convergence point.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.units import Exponent, UnitError, parse

# ---------------------------------------------------------------------------
# §3.0 baseline integer-exponent widening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("m^2", 2),
        ("m^-1", -1),
        ("m^(2)", 2),
        ("m^(-1)", -1),
        ("m^(2/3)", Fraction(2, 3)),
        ("m^-(2/3)", Fraction(-2, 3)),
        ("m^(+2)", 2),
    ],
)
def test_integer_exponent_shapes_under_baseline_widening(expr, expected):
    """§3.0 widening: all four integer-exponent shapes parse strictly
    (no flag required), plus paren-rationals and the explicit ``+``
    sign."""
    u = parse(expr)
    assert u.dimension[1] == expected


# ---------------------------------------------------------------------------
# Symbolic exponents — bare and paren'd shapes
# ---------------------------------------------------------------------------


def test_bare_identifier_exponent():
    """``m^kappa`` builds an :class:`Exponent` carrying ``kappa``
    with coefficient 1 and zero constant offset."""
    u = parse("m^kappa")
    assert u.dimension[1] == Exponent.build({"kappa": 1}, 0)


def test_bare_signed_identifier_exponent():
    u = parse("m^-kappa")
    assert u.dimension[1] == Exponent.build({"kappa": -1}, 0)


def test_paren_identifier_exponent():
    u = parse("m^(kappa)")
    assert u.dimension[1] == Exponent.build({"kappa": 1}, 0)


def test_paren_signed_identifier_exponent():
    u = parse("m^(-kappa)")
    assert u.dimension[1] == Exponent.build({"kappa": -1}, 0)


def test_coefficient_times_identifier():
    u = parse("m^(2*kappa)")
    assert u.dimension[1] == Exponent.build({"kappa": 2}, 0)


def test_rational_coefficient_times_identifier():
    u = parse("m^(1/3*kappa)")
    assert u.dimension[1] == Exponent.build({"kappa": Fraction(1, 3)}, 0)


def test_identifier_plus_constant():
    u = parse("m^(kappa+1)")
    assert u.dimension[1] == Exponent.build({"kappa": 1}, 1)


def test_constant_minus_identifier():
    u = parse("m^(1-kappa)")
    assert u.dimension[1] == Exponent.build({"kappa": -1}, 1)


def test_multi_identifier_linear_form():
    u = parse("m^(kappa-lambda)")
    assert u.dimension[1] == Exponent.build(
        {"kappa": 1, "lambda": -1}, 0,
    )


def test_full_linear_form_with_coef_and_constant():
    u = parse("m^(2*kappa-lambda+1/3)")
    assert u.dimension[1] == Exponent.build(
        {"kappa": 2, "lambda": -1}, Fraction(1, 3),
    )


def test_tyvar_with_symbolic_exponent():
    """Composition with polymorphism: a tyvar carries its own
    ``Exponent`` on the exponent slot."""
    u = parse("'a^kappa")
    # tyvars is a tuple of (name, Exponent) pairs.
    assert len(u.tyvars) == 1
    assert u.tyvars[0][0] == "'a"
    assert u.tyvars[0][1] == Exponent.build({"kappa": 1}, 0)


def test_tyvar_with_linear_form_exponent():
    u = parse("'a^(2*kappa)")
    assert u.tyvars[0][1] == Exponent.build({"kappa": 2}, 0)


def test_linear_form_reduces_to_constant_when_symbols_cancel():
    """``(kappa - kappa)`` cancels to 0 — result is a pure-constant
    int 0, not an empty :class:`Exponent`."""
    u = parse("m^(kappa-kappa)")
    # Constant 0; m^0 = dimensionless slot on dim[1].
    assert u.dimension[1] == 0


# ---------------------------------------------------------------------------
# Composition through the rest of the unit grammar
# ---------------------------------------------------------------------------


def test_symbolic_exponent_composes_through_multiplication():
    """``Pa^kappa * m^lambda`` parses each term independently; the
    outer ``*`` is the unit-product operator, not an exponent
    coefficient."""
    u = parse("Pa^kappa * m^lambda")
    # Dimension shape: Pa = kg·m^-1·s^-2; m = m. Result is a
    # mixed unit; we just verify the parse doesn't error and the
    # symbolic terms appear in the resulting dimension exponents.
    assert any(
        isinstance(d, Exponent) and any(t[0] == "kappa" for t in d.terms)
        for d in u.dimension
    )


def test_symbolic_exponent_composes_through_division():
    """Division composes the same way."""
    u = parse("Pa^(kappa) / s^lambda")
    assert any(
        isinstance(d, Exponent) and any(t[0] == "lambda" for t in d.terms)
        for d in u.dimension
    )


# ---------------------------------------------------------------------------
# Rejected shapes — outside the Exponent algebra
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "m^(kappa*lambda)",     # cross-product of identifiers
        "m^(1/kappa)",          # identifier as denominator
        "m^(1.5*kappa)",        # float coefficient (lexer rejects '.')
        "m^kappa^2",            # chained ^ without parens
        "m^(2*)",               # operator stranded
        "m^()",                 # empty parens
    ],
)
def test_non_linear_shapes_rejected(expr):
    """Shapes outside the linear-form-over-Q algebra raise
    :class:`UnitError`."""
    with pytest.raises(UnitError):
        parse(expr)


# ---------------------------------------------------------------------------
# Backward compatibility — existing exponent shapes still parse
# ---------------------------------------------------------------------------


def test_existing_integer_exponent_unchanged():
    """Plain ``m^2`` still compares equal to ``2`` — the algebra's
    :class:`Exponent` / Number coercion shim is what existing call
    sites rely on, and it survives the widening."""
    u = parse("m^2")
    assert u.dimension[1] == 2


def test_existing_paren_rational_unchanged():
    """``m^(1/2)`` still compares equal to ``Fraction(1, 2)``."""
    u = parse("m^(1/2)")
    assert u.dimension[1] == Fraction(1, 2)
