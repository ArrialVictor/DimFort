"""End-to-end pipeline test: scan -> attach -> check on a real fixture.

The fixture has one correct assignment (``force = mass * accel``) and
one deliberately wrong one (``speed = mass / accel`` — that produces
kg/s but ``speed`` is m/s). The pipeline must flag exactly one H001
and emit no other diagnostics.

Skipped automatically when ``lfortran`` is not installed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401  ensure DEFAULT_TABLE
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_check.f90"


def test_pipeline_flags_one_h001_on_smoke_check():
    scan = scan_file(FIXTURE)
    attached = attach(scan)
    asr = lf.dump_tree(FIXTURE, "asr")
    diags = check(asr, attached.var_units, file=str(FIXTURE))

    h001 = [d for d in diags if d.code == "H001"]
    others = [d for d in diags if d.code != "H001"]

    assert len(h001) == 1, f"expected exactly one H001, got {diags}"
    assert others == [], f"unexpected extra diagnostics: {others}"
    # The flagged assignment is on the `speed = mass / accel` line.
    assert h001[0].start.line == 13
