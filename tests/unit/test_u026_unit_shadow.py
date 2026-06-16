"""Coverage for U026 — symbolic exponent variable shadows a known unit.

The 0.2.7 symbolic-exponent surface (Track B.1) accepts identifiers
in exponent positions: ``@unit{Pa^kappa}``. The 0.2.7 permissive-
unit-lexer recognition flags (Track B.2b) include
``allow_integer_suffix_exp`` whose 14-symbol guard list overlaps
with unit names like ``m``, ``s``, ``kg``. An author writing
``@unit{Pa^m}`` with ``m`` intended as a symbolic exponent variable
is using a name that also denotes the meter — the natural reading
"meter raised to a power" doesn't make dimensional sense.

U026 (HINT severity) catches this name shadow at check time.
Severity is HINT because the code may be intentional — a project
may legitimately declare a PARAMETER named ``m`` distinct from the
unit. The diagnostic surfaces the asymmetry without pressure.

Spec: ``docs/design/shipped/permissive-unit-lexer.md`` §4.1
"Residual edge case" paragraph documenting the original gap;
``docs/reference/diagnostic-codes.md`` for the U026 spec entry.
"""
from __future__ import annotations

import pytest

from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.units import iter_symbolic_exponent_names, parse

# ---------------------------------------------------------------------------
# Helper-level — iter_symbolic_exponent_names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected_names",
    [
        ("kg", []),
        ("Pa^2", []),
        ("Pa^kappa", ["kappa"]),
        ("Pa^m", ["m"]),
        ("Pa^(2*kappa-1/3)", ["kappa"]),
        ("Pa^(kappa+lambda)", ["kappa", "lambda"]),
        ("Pa^(2*kappa+3*m)", ["kappa", "m"]),
        ("'a^kappa", ["kappa"]),
    ],
)
def test_iter_symbolic_exponent_names(expr, expected_names):
    u = parse(expr)
    got = sorted(set(iter_symbolic_exponent_names(u)))
    assert got == sorted(expected_names)


# ---------------------------------------------------------------------------
# End-to-end via check_files — U026 emission
# ---------------------------------------------------------------------------


def _check(tmp_path, source: str):
    """Run check_files on a one-file workspace and return the
    diagnostic list."""
    from dimfort.core.multifile import check_files
    fortran = tmp_path / "f.f90"
    fortran.write_text(source)
    result = check_files([fortran])
    return result.diagnostics.get(fortran.resolve(), [])


def test_u026_fires_on_unit_name_as_symbolic_exponent_variable(tmp_path):
    """``@unit{Pa^m}`` — ``m`` is in the unit table; emit U026."""
    source = (
        "subroutine s\n"
        "  real :: x   !< @unit{Pa^m}\n"
        "end subroutine\n"
    )
    diags = _check(tmp_path, source)
    u026 = [d for d in diags if d.code == "U026"]
    assert len(u026) == 1
    assert "'m'" in u026[0].message
    assert u026[0].severity.value == "hint"


def test_u026_does_not_fire_on_non_unit_symbolic_name(tmp_path):
    """``@unit{Pa^kappa}`` — kappa is not a unit; no U026."""
    source = (
        "subroutine s\n"
        "  real :: x   !< @unit{Pa^kappa}\n"
        "end subroutine\n"
    )
    diags = _check(tmp_path, source)
    u026 = [d for d in diags if d.code == "U026"]
    assert u026 == []


def test_u026_does_not_fire_on_canonical_unit_with_no_symbolic_exponent(tmp_path):
    """``@unit{kg/m^3}`` — no symbolic exponent at all; no U026."""
    source = (
        "subroutine s\n"
        "  real :: rho   !< @unit{kg/m^3}\n"
        "end subroutine\n"
    )
    diags = _check(tmp_path, source)
    u026 = [d for d in diags if d.code == "U026"]
    assert u026 == []


def test_u026_emits_once_per_distinct_shadowed_name(tmp_path):
    """``@unit{Pa^(2*kappa+3*m)}`` — only ``m`` is a unit; ``kappa``
    is fine. One U026 fires for ``m``."""
    source = (
        "subroutine s\n"
        "  real :: x   !< @unit{Pa^(2*kappa+3*m)}\n"
        "end subroutine\n"
    )
    diags = _check(tmp_path, source)
    u026 = [d for d in diags if d.code == "U026"]
    assert len(u026) == 1
    assert "'m'" in u026[0].message


def test_u026_emits_one_per_distinct_name_when_multiple_shadow(tmp_path):
    """``@unit{Pa^(m+s)}`` — both ``m`` and ``s`` shadow units.
    Two U026s, one per name, both at the same source span."""
    source = (
        "subroutine s\n"
        "  real :: x   !< @unit{Pa^(m+s)}\n"
        "end subroutine\n"
    )
    diags = _check(tmp_path, source)
    u026 = [d for d in diags if d.code == "U026"]
    assert len(u026) == 2
    names = {
        # Extract the name from the quoted message body.
        d.message.split("'")[1] for d in u026
    }
    assert names == {"m", "s"}


def test_u026_does_not_fire_on_dimensionless_unit(tmp_path):
    """``@unit{1}`` — no exponent at all; no U026."""
    source = (
        "subroutine s\n"
        "  real :: x   !< @unit{1}\n"
        "end subroutine\n"
    )
    diags = _check(tmp_path, source)
    u026 = [d for d in diags if d.code == "U026"]
    assert u026 == []
