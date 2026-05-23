"""Cross-file ``use`` imports must resolve case-insensitively.

Fortran identifiers are case-insensitive. A module may declare a constant
in one case (ECMWF constants are conventionally UPPERCASE) while a consumer
references it in another (lowercase). The imported ``@unit{}`` must still
apply, so a genuine mismatch fires.

Regression for the bug where ``suphel.f90``'s ``rhoh2o = ratm/100.``
(lowercase) silently lost the units of module ``RHOH2O`` (kg/m^3) and
``RATM`` (Pa) — keyed in declaration case — so the kg/m³ ≠ Pa mismatch
(LMDZ finding #013) never fired.
"""
from __future__ import annotations

from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.multifile import check_files


def _write(tmp_path, name: str, text: str):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_use_import_resolves_when_consumer_uses_other_case(tmp_path):
    """UPPERCASE-declared module constants, referenced lowercase in a
    consumer, must keep their units so a real mismatch fires."""
    mod = _write(
        tmp_path,
        "consts_mod.f90",
        "module consts_mod\n"
        "  real :: RHOH2O  !< @unit{kg/m^3}\n"
        "  real :: RATM    !< @unit{Pa}\n"
        "end module consts_mod\n",
    )
    consumer = _write(
        tmp_path,
        "setup.f90",
        "subroutine setup\n"
        "  use consts_mod\n"
        "  implicit none\n"
        "  rhoh2o = ratm / 100.\n"  # lowercase usage vs UPPERCASE declaration
        "end subroutine setup\n",
    )
    res = check_files([mod, consumer])
    codes = [d.code for d in res.diagnostics[consumer.resolve()]]
    assert "H001" in codes, f"expected H001 (kg/m³ ≠ Pa), got {codes}"
    assert "U007" not in codes  # module resolves fine


def test_use_import_consistent_case_unaffected(tmp_path):
    """Sanity guard: same-case usage (which always worked) still fires."""
    mod = _write(
        tmp_path,
        "consts2_mod.f90",
        "module consts2_mod\n"
        "  real :: rho  !< @unit{kg/m^3}\n"
        "  real :: p    !< @unit{Pa}\n"
        "end module consts2_mod\n",
    )
    consumer = _write(
        tmp_path,
        "setup2.f90",
        "subroutine setup2\n"
        "  use consts2_mod\n"
        "  implicit none\n"
        "  rho = p / 100.\n"
        "end subroutine setup2\n",
    )
    res = check_files([mod, consumer])
    codes = [d.code for d in res.diagnostics[consumer.resolve()]]
    assert "H001" in codes
