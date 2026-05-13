"""End-to-end test: multi-file workset with a module + a consumer."""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401
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


FIXTURES = Path(__file__).parents[1] / "fixtures" / "multifile"
GEO = FIXTURES / "geo.f90"
MAIN = FIXTURES / "main.f90"


def _diags(result_dict, path):
    return result_dict.get(Path(path).resolve(), [])


def test_workset_compiles_module_and_finds_cross_file_diagnostics():
    result = check_files([GEO, MAIN])

    geo_diags = _diags(result.diagnostics, GEO)
    main_diags = _diags(result.diagnostics, MAIN)

    # The module file itself should be clean.
    assert geo_diags == [], f"module file should be clean: {geo_diags}"

    # The consumer should produce exactly one H001 and one H004.
    codes = sorted(d.code for d in main_diags)
    assert codes == ["H001", "H004"], (
        f"expected exactly [H001, H004] in main.f90, got {codes}; "
        f"full diags={main_diags}"
    )

    h004 = next(d for d in main_diags if d.code == "H004")
    assert "scale" in h004.message
    assert "expected 1" in h004.message  # factor must be dimensionless


def test_workset_order_independent():
    # Same expected behaviour when the consumer is listed before the module.
    result = check_files([MAIN, GEO])
    geo_diags = _diags(result.diagnostics, GEO)
    main_diags = _diags(result.diagnostics, MAIN)
    assert geo_diags == []
    assert sorted(d.code for d in main_diags) == ["H001", "H004"]
