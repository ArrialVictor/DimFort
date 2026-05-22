"""Step-1 unit tests for the ``Exponent`` type.

The Exponent is a linear form over rationals with named generators
(opaque symbols). These tests pin down construction, equality, the
arithmetic (`+`, `-`, `*`, unary `-`), the queries (is_zero, is_one,
is_constant, as_fraction), and canonicalisation (drop zero coeffs,
sort by name). The type lives alongside ``Unit`` but doesn't change
``Unit``'s behaviour yet — Step 2 of the symbolic-exponents plan will
wire it in.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from dimfort.core.units import Exponent, UnitError


def test_build_drops_zero_coefficients():
    e = Exponent.build({"x": 0, "y": Fraction(2)}, 0)
    assert e.terms == (("y", Fraction(2)),)


def test_build_promotes_ints_to_fractions():
    e = Exponent.build({"x": 1}, 2)
    assert e == Exponent(terms=(("x", Fraction(1)),), constant=Fraction(2))


def test_build_sorts_terms_by_name():
    e = Exponent.build({"zeta": 1, "alpha": 2, "kappa": Fraction(-1)})
    assert [n for n, _ in e.terms] == ["alpha", "kappa", "zeta"]


def test_direct_construction_with_unsorted_terms_raises():
    with pytest.raises(ValueError):
        Exponent(terms=(("z", Fraction(1)), ("a", Fraction(1))), constant=Fraction(0))


def test_direct_construction_with_zero_coefficient_raises():
    with pytest.raises(ValueError):
        Exponent(terms=(("x", Fraction(0)),), constant=Fraction(0))


def test_from_value_and_from_symbol():
    assert Exponent.from_value(Fraction(2, 7)).as_fraction() == Fraction(2, 7)
    e_kappa = Exponent.from_symbol("kappa")
    assert e_kappa.terms == (("kappa", Fraction(1)),)
    assert e_kappa.constant == 0
    e_3k = Exponent.from_symbol("kappa", 3)
    assert e_3k.terms == (("kappa", Fraction(3)),)


def test_zero_one_constant_queries():
    z = Exponent.build()
    assert z.is_zero() and z.is_constant() and not z.is_one()
    o = Exponent.build(constant=1)
    assert o.is_one() and o.is_constant() and not o.is_zero()
    k = Exponent.from_symbol("kappa")
    assert not k.is_constant()
    assert k.as_fraction() is None
    c = Exponent.from_value(Fraction(2, 7))
    assert c.is_constant()
    assert c.as_fraction() == Fraction(2, 7)


def test_addition_combines_terms_and_constants():
    a = Exponent.build({"kappa": 1}, Fraction(1))
    b = Exponent.build({"kappa": Fraction(-1)}, 0)
    # kappa + (1 - kappa) = 1
    s = a + b
    assert s == Exponent.from_value(Fraction(1))
    assert s.is_one()


def test_addition_keeps_independent_symbols_separate():
    a = Exponent.from_symbol("kappa")
    b = Exponent.from_symbol("lambda")
    s = a + b
    assert s.terms == (("kappa", Fraction(1)), ("lambda", Fraction(1)))
    assert s.constant == 0


def test_subtraction_and_negation():
    a = Exponent.build({"kappa": 2}, Fraction(3))
    b = Exponent.build({"kappa": 1}, Fraction(1))
    d = a - b
    assert d == Exponent.build({"kappa": 1}, Fraction(2))
    assert -a == Exponent.build({"kappa": -2}, Fraction(-3))


def test_addition_with_plain_number():
    a = Exponent.from_symbol("kappa")
    s = a + 3
    assert s == Exponent.build({"kappa": 1}, Fraction(3))
    s2 = 3 + a
    assert s2 == s


def test_subtraction_with_plain_number():
    a = Exponent.from_symbol("kappa")
    d = a - 1
    assert d == Exponent.build({"kappa": 1}, Fraction(-1))
    d2 = 1 - a
    assert d2 == Exponent.build({"kappa": -1}, Fraction(1))


def test_scalar_multiplication_is_linear():
    a = Exponent.build({"kappa": Fraction(2, 7)}, Fraction(3))
    s = a * 7
    assert s == Exponent.build({"kappa": Fraction(2)}, Fraction(21))
    s2 = 7 * a
    assert s2 == s
    s3 = a * Fraction(1, 2)
    assert s3 == Exponent.build(
        {"kappa": Fraction(1, 7)}, Fraction(3, 2),
    )


def test_constant_exponent_times_symbolic_is_scalar_promotion():
    """Multiplying a pure-constant Exponent by a symbolic one promotes
    the constant to a scalar — still linear, no error."""
    c = Exponent.from_value(Fraction(3))
    k = Exponent.from_symbol("kappa")
    assert c * k == Exponent.build({"kappa": 3}, 0)
    assert k * c == Exponent.build({"kappa": 3}, 0)


def test_symbol_times_symbol_is_nonlinear_and_raises():
    a = Exponent.from_symbol("kappa")
    b = Exponent.from_symbol("lambda")
    with pytest.raises(UnitError):
        _ = a * b


def test_cancellation_via_smart_constructor():
    """The canonical form drops zero coefficients, so kappa - kappa
    isn't a {"kappa": 0} entry but disappears entirely."""
    k = Exponent.from_symbol("kappa")
    z = k - k
    assert z.is_zero()
    assert z.terms == ()


def test_equality_is_structural():
    a = Exponent.build({"kappa": 1, "lambda": -1}, Fraction(2))
    b = Exponent.build({"lambda": -1, "kappa": 1}, Fraction(2))
    assert a == b
    assert hash(a) == hash(b)


def test_str_renders_canonical_form():
    assert str(Exponent.build()) == "0"
    assert str(Exponent.from_value(Fraction(2, 7))) == "2/7"
    assert str(Exponent.from_symbol("kappa")) == "kappa"
    assert str(Exponent.from_symbol("kappa", -1)) == "-kappa"
    assert str(Exponent.build({"kappa": Fraction(2, 7)})) == "2/7·kappa"
    assert str(Exponent.build({"kappa": 1}, 1)) == "kappa + 1"
    assert str(Exponent.build({"kappa": -1}, 1)) == "-kappa + 1"


def test_hashability():
    s = {Exponent.from_value(1), Exponent.from_value(1), Exponent.from_symbol("kappa")}
    assert len(s) == 2
