"""``@unit_assume{ <unit> : <reason> }`` — the D1.4 escape hatch.

On an assignment line the directive tells the checker to stop deriving the
RHS unit (suppressing D1.4 and any interior fire) and instead treat the
result as the asserted unit, still consistency-checked against a declared
LHS. Each use emits a U020 INFO note for auditability.

Motivated by LMDZ finding #016: empirical microphysics power-laws such as
``rho_snow = 1.e3*0.178*(r_snow*2.*1000.)**(-0.922)`` raise a length to a
non-rational exponent, an irreducible D1.4 that OQ4 cannot close.
"""
from __future__ import annotations

from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.multifile import check_files


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text)
    return p


# The #016 shape: a length raised to a non-rational decimal exponent.
_EMPIRICAL = "rho = 1.e3*0.178*(r*2.*1000.)**(-0.922)"


def _codes(res, path):
    return [d.code for d in res.diagnostics[path.resolve()]]


def test_without_assume_d14_fires(tmp_path):
    """Guard: the empirical power-law fires D1.4 when *not* assumed."""
    f = _write(
        tmp_path, "m.f90",
        "module m\n contains\n  subroutine s()\n"
        "   real :: r    !< @unit{m}\n"
        "   real :: rho  !< @unit{kg/m^3}\n"
        f"   {_EMPIRICAL}\n"
        "  end subroutine\n end module\n",
    )
    res = check_files([f])
    msgs = " ".join(d.message for d in res.diagnostics[f.resolve()])
    assert "D1.4" in msgs, f"expected a D1.4 fire, got {msgs!r}"


def test_assume_suppresses_d14_and_emits_u010(tmp_path):
    """With the assume, D1.4 is gone and a U020 INFO note is emitted."""
    f = _write(
        tmp_path, "m.f90",
        "module m\n contains\n  subroutine s()\n"
        "   real :: r    !< @unit{m}\n"
        "   real :: rho  !< @unit{kg/m^3}\n"
        f"   {_EMPIRICAL}   !< @unit_assume{{kg/m^3 : empirical-fit Brandes2007}}\n"
        "  end subroutine\n end module\n",
    )
    res = check_files([f])
    diags = res.diagnostics[f.resolve()]
    msgs = " ".join(d.message for d in diags)
    assert "D1.4" not in msgs, f"D1.4 should be suppressed, got {msgs!r}"
    u010 = [d for d in diags if d.code == "U020"]
    assert len(u010) == 1, f"expected one U020, got {[d.code for d in diags]}"
    assert u010[0].severity.value == "info"


def test_assume_conflicts_with_declared_lhs_fires_h001(tmp_path):
    """An assume that contradicts a declared LHS unit still fires H001 —
    the hatch suppresses derivation, never consistency."""
    f = _write(
        tmp_path, "m.f90",
        "module m\n contains\n  subroutine s()\n"
        "   real :: r    !< @unit{m}\n"
        "   real :: e    !< @unit{J}\n"
        f"   e = 1.e3*(r*2.)**(-0.922)   !< @unit_assume{{kg/m^3 : conflict}}\n"
        "  end subroutine\n end module\n",
    )
    res = check_files([f])
    assert "H001" in _codes(res, f), f"expected H001, got {_codes(res, f)}"


def test_assume_missing_reason_is_malformed(tmp_path):
    """A reason is mandatory; omitting the ':' is a U001 malformed error."""
    f = _write(
        tmp_path, "m.f90",
        "module m\n contains\n  subroutine s()\n"
        "   real :: r  !< @unit{m}\n"
        "   real :: a\n"
        "   a = (r*2.)**(-0.5)   !< @unit_assume{m}\n"
        "  end subroutine\n end module\n",
    )
    res = check_files([f])
    assert "U001" in _codes(res, f), f"expected U001, got {_codes(res, f)}"


def test_assume_unparseable_unit_is_u002(tmp_path):
    """A unit that won't parse surfaces as U002 and the assume is dropped."""
    f = _write(
        tmp_path, "m.f90",
        "module m\n contains\n  subroutine s()\n"
        "   real :: r  !< @unit{m}\n"
        "   real :: a\n"
        "   a = (r*2.)**(-0.5)   !< @unit_assume{not_a_unit_xyz : bad}\n"
        "  end subroutine\n end module\n",
    )
    res = check_files([f])
    assert "U002" in _codes(res, f), f"expected U002, got {_codes(res, f)}"


def test_assume_does_not_bleed_to_next_statement(tmp_path):
    """A trailing assume on statement N must not be picked up by N+1.

    Regression for the line-span lookback bug where the next assignment
    grabbed the previous line's assume (double U020 + wrong unit)."""
    f = _write(
        tmp_path, "m.f90",
        "module m\n contains\n  subroutine s()\n"
        "   real :: r    !< @unit{m}\n"
        "   real :: rho  !< @unit{kg/m^3}\n"
        "   real :: g    !< @unit{m}\n"
        f"   rho = 1.e3*(r*2.)**(-0.922)   !< @unit_assume{{kg/m^3 : fit}}\n"
        "   g = r * 2.0\n"  # plain, no assume — must stay clean
        "  end subroutine\n end module\n",
    )
    res = check_files([f])
    diags = res.diagnostics[f.resolve()]
    u010 = [d for d in diags if d.code == "U020"]
    assert len(u010) == 1, f"expected exactly one U020, got {len(u010)}"
    assert u010[0].start.line == 7, f"U020 should be on line 7, got {u010[0].start.line}"
