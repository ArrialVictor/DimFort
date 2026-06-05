"""M1 tests for tyvar representation in the unit AST.

Covers the data-model and parser surface only — no unification or
generalization (those land in M2/M3). The contract verified here:

- ``@unit{'a}`` parses to a Unit with a tyvar entry.
- Algebraic operations (``*``, ``/``, ``pow``) compose tyvar exponents
  via the existing Exponent machinery and canonicalize on the fly.
- Symbolic exponents on tyvars compose (``'a^kappa`` round-trips).
- ``is_dimensionless`` treats a tyvar as a live dimension.
- ``equal_dim`` / ``compare`` distinguish tyvar maps.
- ``format_unit`` / ``format_unit_source`` render tyvars first; the
  source form round-trips through ``parse``.
- Cache serde round-trips tyvars, and pre-polymorphism payloads
  (no ``"v"`` key) still load as concrete Units.
"""
from fractions import Fraction

import pytest

from dimfort.core import units  # noqa: F401 — populates DEFAULT_TABLE
from dimfort.core.cache_serde import dump_unit_expr, load_unit_expr
from dimfort.core.units import (
    Exponent,
    Unit,
    Verdict,
    compare,
    equal_dim,
    format_unit,
    format_unit_source,
    is_dimensionless,
    parse,
)

ZERO_DIM = (0, 0, 0, 0, 0, 0, 0)


def _tyvar(name: str, exp: int | Fraction = 1) -> tuple[str, Exponent]:
    return (name, Exponent.from_value(exp))


# ---------------------------------------------------------------------------
# Parsing


@pytest.mark.parametrize(
    "expr, expected_tyvars",
    [
        ("'a", (_tyvar("'a"),)),
        ("'b", (_tyvar("'b"),)),
        ("'a*'b", (_tyvar("'a"), _tyvar("'b"))),
        ("'a^2", (_tyvar("'a", 2),)),
        ("'a^(1/2)", (_tyvar("'a", Fraction(1, 2)),)),
    ],
)
def test_parse_tyvar(expr, expected_tyvars):
    u = parse(expr)
    assert isinstance(u, Unit)
    assert u.dimension == tuple(Exponent.from_value(0) for _ in range(7))
    assert u.tyvars == expected_tyvars


def test_parse_tyvar_with_concrete_unit():
    # ``'a · kg`` — the Van Leer ``u_mq = u_m * q`` intermediate.
    u = parse("'a*kg")
    assert isinstance(u, Unit)
    assert u.tyvars == (_tyvar("'a"),)
    # mass slot = 1.
    assert u.dimension[0] == Exponent.from_value(1)


def test_parse_tyvar_divide_concrete():
    # ``'a / s`` — a tracer-tendency unit.
    u = parse("'a/s")
    assert u.tyvars == (_tyvar("'a"),)
    # time slot = -1.
    assert u.dimension[2] == Exponent.from_value(-1)


# ---------------------------------------------------------------------------
# Algebra closure


def test_tyvar_self_cancel():
    """``'a / 'a`` cancels via the canonicalizer."""
    a = parse("'a")
    result = a / a
    assert result.tyvars == ()
    assert is_dimensionless(result)


def test_tyvar_self_product():
    """``'a * 'a = 'a²``."""
    a = parse("'a")
    result = a * a
    assert result.tyvars == (_tyvar("'a", 2),)


def test_tyvar_pow_int():
    a = parse("'a")
    assert a.pow(3).tyvars == (_tyvar("'a", 3),)


def test_tyvar_pow_rational():
    a = parse("'a")
    assert a.pow(Fraction(1, 2)).tyvars == (_tyvar("'a", Fraction(1, 2)),)


def test_tyvar_pow_neg_one_then_self_product():
    """``'a^(-1) * 'a`` should fully cancel."""
    a = parse("'a")
    result = a.pow(-1) * a
    assert result.tyvars == ()


def test_distinct_tyvars_dont_unify():
    """``'a`` and ``'b`` are independent — product is two-element map."""
    a = parse("'a")
    b = parse("'b")
    assert (a * b).tyvars == (_tyvar("'a"), _tyvar("'b"))


def test_tyvar_times_concrete_then_divide():
    """``'a·kg / kg = 'a``."""
    ak = parse("'a*kg")
    kg = parse("kg")
    assert (ak / kg).tyvars == (_tyvar("'a"),)
    assert all(d.is_zero() for d in (ak / kg).dimension)


# ---------------------------------------------------------------------------
# Composition with symbolic exponents


def test_tyvar_with_symbolic_exponent_cancels():
    """``'a^κ * 'a^(1-κ) = 'a``. Exponent algebra handles symbolic cancellation
    for tyvars exactly as it does for SI dims."""
    kappa = Exponent.from_symbol("kappa")
    one_minus_kappa = Exponent.from_value(1) - kappa
    a = parse("'a")
    left = a.pow(kappa)
    right = a.pow(one_minus_kappa)
    result = left * right
    assert result.tyvars == (_tyvar("'a", 1),)


# ---------------------------------------------------------------------------
# is_dimensionless / equal_dim / compare


def test_tyvar_unit_is_not_dimensionless():
    assert not is_dimensionless(parse("'a"))


def test_dimensionless_unit_unchanged():
    assert is_dimensionless(parse("1"))


def test_equal_dim_requires_matching_tyvars():
    assert not equal_dim(parse("'a"), parse("'b"))
    assert equal_dim(parse("'a"), parse("'a"))


def test_equal_dim_concrete_vs_tyvar():
    assert not equal_dim(parse("kg"), parse("'a*kg"))


def test_compare_distinguishes_tyvars():
    assert compare(parse("'a"), parse("'b")) == Verdict("dim_mismatch")
    assert compare(parse("'a"), parse("'a")) == Verdict("equal")


# ---------------------------------------------------------------------------
# Format round-trip


@pytest.mark.parametrize(
    "expr, expected_source",
    [
        ("'a", "'a"),
        ("'a*kg", "'a*kg"),
        ("'a*'b", "'a*'b"),
        ("'a^2", "'a^2"),
        ("'a/s", "'a/s"),
        ("'a/'b", "'a/'b"),
    ],
)
def test_format_unit_source_roundtrip(expr, expected_source):
    u = parse(expr)
    src = format_unit_source(u)
    assert src == expected_source
    # Round-trip: parse(format_unit_source(parse(x))) == parse(x).
    assert parse(src) == u


def test_format_unit_display_tyvar_first():
    """Display puts tyvars before SI dims (``'a·kg``, not ``kg·'a``)."""
    u = parse("'a*kg")
    assert format_unit(u) == "'a·kg"


def test_format_unit_display_superscript():
    assert format_unit(parse("'a^2")) == "'a²"
    assert format_unit(parse("'a^-1")) == "'a⁻¹"


# ---------------------------------------------------------------------------
# Cache round-trip


def test_cache_roundtrip_tyvar():
    u = parse("'a*kg/s")
    payload = dump_unit_expr(u)
    assert "v" in payload
    restored = load_unit_expr(payload)
    assert restored == u


def test_cache_concrete_unit_omits_v_key():
    """Pre-polymorphism shape: a concrete Unit serialises with no ``v`` key
    so old caches keep their exact byte payload."""
    u = parse("kg/s")
    payload = dump_unit_expr(u)
    assert "v" not in payload


def test_cache_pre_polymorphism_payload_loads():
    """A payload written before the ``v`` field existed (no ``v`` key)
    must still load — defaults to empty tyvars."""
    u = parse("m/s")
    payload = dump_unit_expr(u)
    payload.pop("v", None)  # simulate v4 cache entry
    restored = load_unit_expr(payload)
    assert restored == u
    assert restored.tyvars == ()


# ---------------------------------------------------------------------------
# Construction invariants


def test_unit_canonicalizes_tyvar_order():
    """Construction with out-of-order tyvars canonicalizes to sorted form."""
    u = Unit(
        dimension=tuple(Exponent.from_value(0) for _ in range(7)),
        factor=Fraction(1),
        tyvars=(("'b", Exponent.from_value(1)), ("'a", Exponent.from_value(1))),
    )
    assert u.tyvars == (_tyvar("'a"), _tyvar("'b"))


def test_unit_canonicalizes_drops_zero_exponent():
    """A tyvar entry with a zero Exponent is dropped during construction."""
    u = Unit(
        dimension=tuple(Exponent.from_value(0) for _ in range(7)),
        factor=Fraction(1),
        tyvars=(("'a", Exponent.from_value(0)),),
    )
    assert u.tyvars == ()


def test_unit_canonicalizes_aggregates_duplicates():
    """Duplicate tyvar names get summed."""
    u = Unit(
        dimension=tuple(Exponent.from_value(0) for _ in range(7)),
        factor=Fraction(1),
        tyvars=(("'a", Exponent.from_value(1)), ("'a", Exponent.from_value(2))),
    )
    assert u.tyvars == (_tyvar("'a", 3),)
