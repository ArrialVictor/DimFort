"""End-to-end: rational ** exponents in source code."""
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_rational_pow.f90"


def test_rational_pow_flags_one_h001():
    result = check_files([FIXTURE])
    diags = result.diagnostics.get(FIXTURE.resolve(), [])
    h001 = [d for d in diags if d.code == "H001"]
    other = [d for d in diags if d.code != "H001"]
    assert len(h001) == 1, f"expected one H001, got {diags}"
    assert other == [], f"unexpected extras: {other}"
    # The bad assignment is on line 12.
    assert h001[0].start.line == 12
