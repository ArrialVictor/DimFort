"""End-to-end: derived-type field annotation + `%`-access checking."""
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_derived_types.f90"


def test_field_annotations_attached_separately_from_locals():
    att = attach(scan_file(FIXTURE))
    # Field units are in their own table.
    assert att.field_units == {
        ("particle", "m"): "kg",
        ("particle", "q"): "C",
        ("particle", "v"): "m/s",
    }
    # Same-named local `mass` is in var_units.
    assert att.var_units.get("mass") == "kg"


def test_derived_type_pipeline_flags_one_h001():
    scan = scan_file(FIXTURE)
    att = attach(scan)
    ast, asr = lf.load_trees(FIXTURE)
    diags = check(
        asr,
        att.var_units,
        ast=ast,
        field_units_text=att.field_units,
        file=str(FIXTURE),
    )
    h001 = [d for d in diags if d.code == "H001"]
    other = [d for d in diags if d.code != "H001"]
    assert len(h001) == 1, f"expected one H001, got {diags}"
    assert other == [], f"unexpected extras: {other}"
    # The flagged line is `b%m = badmass`.
    assert h001[0].start.line == 19
