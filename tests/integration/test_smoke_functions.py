"""End-to-end: user-defined function and subroutine call checks."""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401
from dimfort.core.annotations import scan_file
from dimfort.core.attach import attach
from dimfort.core.checker import check


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_functions.f90"


def test_functions_pipeline_finds_h001_and_h004():
    scan = scan_file(FIXTURE)
    att = attach(scan)
    ast, asr = lf.load_trees(FIXTURE)
    diags = check(asr, att.var_units, ast=ast, file=str(FIXTURE))

    h001 = [d for d in diags if d.code == "H001"]
    h004 = [d for d in diags if d.code == "H004"]
    other = [d for d in diags if d.code not in {"H001", "H004"}]

    assert len(h001) == 1, f"expected one H001, got {diags}"
    assert len(h004) == 1, f"expected one H004, got {diags}"
    assert other == [], f"unexpected extras: {other}"

    assert "box_area" not in h001[0].message
    # H004 must name the called routine.
    assert "scale" in h004[0].message
