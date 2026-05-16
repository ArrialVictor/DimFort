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


def _check(src: str, var_units: dict[str, str]) -> list:
    src_b = src.encode()
    tree = ts.parse_text(src_b)
    return ts_checker.check(
        tree, var_units, source=src_b, file="test.f90",
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


def test_h003_dimensionless_intrinsic_violation():
    """Passing a kg argument to ``exp`` fires H003."""
    src = (
        "subroutine s\n"
        "  real :: a, b\n"
        "  b = exp(a)\n"
        "end subroutine\n"
    )
    diags = _check(src, {"a": "kg", "b": "1"})
    codes = [d.code for d in diags]
    assert "H003" in codes


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
