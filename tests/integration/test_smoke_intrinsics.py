"""End-to-end test: intrinsic handling through scan + attach + check."""
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_intrinsics.f90"


def test_intrinsics_flags_h003_only():
    scan = scan_file(FIXTURE)
    att = attach(scan)
    ast, asr = lf.load_trees(FIXTURE)
    diags = check(asr, att.var_units, ast=ast, file=str(FIXTURE))

    h001 = [d for d in diags if d.code == "H001"]
    h002 = [d for d in diags if d.code == "H002"]
    h003 = [d for d in diags if d.code == "H003"]
    others = [d for d in diags if d.code not in {"H001", "H002", "H003"}]

    assert h001 == []
    assert h002 == []
    assert len(h003) == 1, f"expected exactly one H003, got {diags}"
    assert "sin" in h003[0].message
    assert others == []
