"""M2 tests for AG-unification.

Pure-algorithm tests. No checker integration. Covers:

- Single tyvar across one or more slots: agreement → success;
  disagreement → conflict naming every contributing slot.
- Distinct tyvars in one signature (eff_param_ari-shaped).
- Tyvar with concrete-unit slot (``'a*kg`` from vlx).
- Tyvar squared (``'a^2``).
- Free tyvar (declared, no slot references) → defaults to ``{1}``.
- Multi-tyvar product slot (``'a*'b``) — underdetermined; the solver
  picks one valid solution.
- UnsupportedPolymorphism for symbolic tyvar exponents and
  wrapper-typed slots.
- Substitution.apply round-trips signature → call-site unit.
"""
from fractions import Fraction

import pytest

from dimfort.core import units  # noqa: F401
from dimfort.core.polymorphism import (
    SlotEquation,
    Substitution,
    UnsupportedPolymorphism,
    unify,
)
from dimfort.core.units import Exponent, LogWrap, Unit, parse


def _eq(idx: int, formal: str, actual: str, name: str | None = None) -> SlotEquation:
    f = parse(formal)
    a = parse(actual)
    assert isinstance(f, Unit) and isinstance(a, Unit)
    return SlotEquation(slot_index=idx, slot_name=name, formal=f, actual=a)


# ---------------------------------------------------------------------------
# Trivial / basic success


def test_no_tyvars_returns_empty_substitution():
    result = unify([], free_tyvars=())
    assert result.ok
    assert result.substitution.bindings == {}


def test_single_tyvar_single_slot():
    eqs = [_eq(0, "'a", "m")]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("m")


def test_single_tyvar_two_agreeing_slots():
    # eff_param_ari shape: (x: 'a, frac: {1}, agg: 'a)
    eqs = [_eq(0, "'a", "m"), _eq(1, "1", "1"), _eq(2, "'a", "m")]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("m")


def test_two_distinct_tyvars():
    # momentum shape: (m: 'a, v: 'b, p: 'a*'b)
    eqs = [
        _eq(0, "'a", "kg"),
        _eq(1, "'b", "m/s"),
        _eq(2, "'a*'b", "kg*m/s"),
    ]
    result = unify(eqs, free_tyvars=("'a", "'b"))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("kg")
    assert result.substitution.bindings["'b"] == parse("m/s")


def test_tyvar_times_concrete():
    # vlx shape: u_mq = u_m * q. Formal slot 'a*kg, actual m*kg → 'a = m.
    eqs = [_eq(0, "'a*kg", "m*kg")]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("m")


def test_tyvar_squared():
    eqs = [_eq(0, "'a^2", "m^2")]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("m")


def test_tyvar_divided_by_concrete():
    # thermcell_dq shape: dq = (q - qold)/dt → 'a/s
    eqs = [_eq(0, "'a/s", "m/s")]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("m")


# ---------------------------------------------------------------------------
# Free tyvars (declared but no slot constrains)


def test_unconstrained_tyvar_defaults_to_dimensionless():
    # Tyvar 'b never appears in any slot — bound to {1}.
    eqs = [_eq(0, "'a", "m")]
    result = unify(eqs, free_tyvars=("'a", "'b"))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("m")
    assert result.substitution.bindings["'b"] == parse("1")


# ---------------------------------------------------------------------------
# Conflicts


def test_disagreement_produces_conflict():
    eqs = [_eq(0, "'a", "m", name="x"), _eq(1, "'a", "kg", name="y")]
    result = unify(eqs, free_tyvars=("'a",))
    assert not result.ok
    assert len(result.conflicts) == 1
    c = result.conflicts[0]
    assert c.tyvar == "'a"
    # Both slots contribute to the conflict — symmetric.
    assert len(c.contributions) == 2
    slot_indices = {contrib.slot_index for contrib in c.contributions}
    assert slot_indices == {0, 1}


def test_three_way_conflict():
    eqs = [
        _eq(0, "'a", "m"),
        _eq(1, "'a", "kg"),
        _eq(2, "'a", "s"),
    ]
    result = unify(eqs, free_tyvars=("'a",))
    assert not result.ok
    contributions_count = len(result.conflicts[0].contributions)
    assert contributions_count == 3


def test_one_tyvar_conflicts_other_succeeds():
    eqs = [
        _eq(0, "'a", "m"),
        _eq(1, "'a", "kg"),   # conflict on 'a
        _eq(2, "'b", "m/s"),  # 'b consistent
    ]
    result = unify(eqs, free_tyvars=("'a", "'b"))
    assert not result.ok
    # Only 'a is reported.
    conflict_names = {c.tyvar for c in result.conflicts}
    assert conflict_names == {"'a"}


# ---------------------------------------------------------------------------
# Contributions for diagnostic rendering


def test_contributions_recorded_per_slot():
    eqs = [_eq(0, "'a", "m", name="x"), _eq(1, "'a", "m", name="y")]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    contribs = result.all_contributions["'a"]
    assert len(contribs) == 2
    assert contribs[0].slot_name == "x"
    assert contribs[1].slot_name == "y"
    assert all(c.implied == parse("m") for c in contribs)


def test_no_contribution_from_slot_without_tyvar():
    eqs = [_eq(0, "'a", "m"), _eq(1, "1", "1")]
    result = unify(eqs, free_tyvars=("'a",))
    contribs = result.all_contributions["'a"]
    assert len(contribs) == 1  # slot 1 contributed nothing
    assert contribs[0].slot_index == 0


# ---------------------------------------------------------------------------
# Substitution.apply


def test_apply_substitutes_tyvar():
    eqs = [_eq(0, "'a", "m")]
    result = unify(eqs, free_tyvars=("'a",))
    sigma = result.substitution
    # Apply to a formal carrying 'a → should produce the corresponding actual.
    formal = parse("'a*kg")
    applied = sigma.apply(formal)
    assert applied == parse("m*kg")


def test_apply_passes_through_unbound_tyvar():
    sigma = Substitution(bindings={"'a": parse("m")})
    formal = parse("'b")
    assert sigma.apply(formal) == parse("'b")


def test_apply_handles_powered_tyvar():
    sigma = Substitution(bindings={"'a": parse("m")})
    formal = parse("'a^2")
    assert sigma.apply(formal) == parse("m^2")


def test_apply_handles_compound_tyvar():
    sigma = Substitution(bindings={"'a": parse("kg"), "'b": parse("m/s")})
    formal = parse("'a*'b")
    assert sigma.apply(formal) == parse("kg*m/s")


# ---------------------------------------------------------------------------
# Unsupported polymorphism


def test_symbolic_tyvar_exponent_unsupported():
    # Formal slot 'a^kappa is Phase 2 scope.
    kappa = Exponent.from_symbol("kappa")
    formal = Unit(
        tuple(Exponent.from_value(0) for _ in range(7)),
        Fraction(1),
        tyvars=(("'a", kappa),),
    )
    actual = parse("m")
    eqs = [SlotEquation(slot_index=0, slot_name=None,
                        formal=formal, actual=actual)]
    with pytest.raises(UnsupportedPolymorphism):
        unify(eqs, free_tyvars=("'a",))


def test_wrapper_typed_slot_unsupported():
    formal = LogWrap(parse("'a"))
    actual = LogWrap(parse("m"))
    # SlotEquation expects Unit-typed slots; force the unsupported path
    # through a non-Unit formal.
    eq = SlotEquation(slot_index=0, slot_name=None,
                      formal=formal, actual=actual)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedPolymorphism):
        unify([eq], free_tyvars=("'a",))


# ---------------------------------------------------------------------------
# Real-world signature shapes (drawn from physics-model annotation work)


def test_vlx_shape():
    """Van Leer ``vlx``: q['a], pente_max[1], masse[kg], u_m[kg].
    Call site passes q={kg/kg}, pente_max={1}, masse={kg}, u_m={kg}."""
    eqs = [
        _eq(0, "'a", "kg/kg", name="q"),
        _eq(1, "1", "1", name="pente_max"),
        _eq(2, "kg", "kg", name="masse"),
        _eq(3, "kg", "kg", name="u_m"),
    ]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("kg/kg")


def test_thermcell_dq_shape():
    """thermcell_dq: q['a], dq['a/s], qa['a]. Call: q={kg/kg}."""
    eqs = [
        _eq(0, "'a", "kg/kg", name="q"),
        _eq(1, "'a/s", "kg/(kg*s)", name="dq"),
        _eq(2, "'a", "kg/kg", name="qa"),
    ]
    result = unify(eqs, free_tyvars=("'a",))
    assert result.ok
    assert result.substitution.bindings["'a"] == parse("kg/kg")
