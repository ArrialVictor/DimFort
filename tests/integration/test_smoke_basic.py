"""End-to-end smoke test: scan + attach on a representative fixture.

Confirms the full annotation pipeline works on real Fortran exercising
every supported attachment shape:

- inline POST (``!< @unit{...}``)
- PRE block before a declaration
- declaration list (apply-to-all)
- continued declaration with POST on the last line  (form B)
- continued declaration with POST on the first line (form C)
- continued declaration preceded by a PRE block     (form A)

Attachment is now fully source-side, so this test no longer needs
LFortran — it stays in unit-test territory but lives under
``tests/integration/`` because it exercises the full scan + attach
pipeline on a real file.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core.annotations import scan_file
from dimfort.core.attach import attach

FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_basic.f90"


def test_smoke_basic_end_to_end():
    scan = scan_file(FIXTURE)
    res = attach(scan)

    expected = {
        "vel": "m/s",
        "mass": "kg",
        "x": "m", "y": "m", "z": "m",
        "pressure": "Pa", "temperature": "Pa", "density": "Pa",
        "a1": "kg", "a2": "kg", "a3": "kg",
        "alpha": "1", "beta": "1", "gamma": "1",
    }

    missing = {k: v for k, v in expected.items() if k not in res.var_units}
    wrong = {
        k: (res.var_units[k], v)
        for k, v in expected.items()
        if k in res.var_units and res.var_units[k] != v
    }

    assert not missing and not wrong, (
        f"missing={missing}\nwrong={wrong}\norphans={res.orphans}\n"
        f"got={res.var_units}"
    )
    assert res.conflicts == []
