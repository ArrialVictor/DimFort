"""Tests for the in-memory text override pipe (used by the LSP)."""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core.multifile import check_files


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


CLEAN = """\
program p
  implicit none
  real :: v   !< @unit{m/s}
  v = v
end program
"""

BUGGY = """\
program p
  implicit none
  real :: v   !< @unit{m/s}
  real :: kg  !< @unit{kg}
  v = kg
end program
"""


def test_override_replaces_disk_content(tmp_path: Path):
    src = tmp_path / "p.f90"
    src.write_text(CLEAN)

    # Disk is clean — no diagnostics.
    res = check_files([src])
    assert res.diagnostics.get(src.resolve(), []) == []

    # Override with buggy text — H001 fires even though the file on
    # disk is still clean.
    res = check_files([src], overrides={src: BUGGY})
    diags = res.diagnostics.get(src.resolve(), [])
    codes = [d.code for d in diags]
    assert "H001" in codes


def test_disk_unchanged_after_override_run(tmp_path: Path):
    src = tmp_path / "p.f90"
    src.write_text(CLEAN)
    check_files([src], overrides={src: BUGGY})
    assert src.read_text() == CLEAN, "override must not touch disk"


def test_result_exposes_trees_and_parsed_units(tmp_path: Path):
    src = tmp_path / "p.f90"
    src.write_text(CLEAN)
    res = check_files([src])
    assert src.resolve() in res.trees, "trees should be populated on success"
    assert "v" in res.merged_var_units, "merged_var_units should expose parsed Unit"
