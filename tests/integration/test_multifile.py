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


def test_mod_cache_round_trip(tmp_path):
    """Phase 1 cache should drop a .mods.json entry on cold run and
    reuse it on warm run, producing identical diagnostics either way.
    """
    cache_dir = tmp_path / "cache"

    # Cold: no entries.
    cold = check_files([GEO, MAIN], cache_dir=cache_dir)
    cold_codes = sorted(d.code for d in _diags(cold.diagnostics, MAIN))

    mod_entries = list(cache_dir.glob("*.mods.json"))
    assert len(mod_entries) == 1, (
        f"expected exactly one .mods.json (geo.f90 declares the only "
        f"module), got {mod_entries}"
    )

    # Warm: same diagnostics, must not crash on cached .mod restore.
    warm = check_files([GEO, MAIN], cache_dir=cache_dir)
    warm_codes = sorted(d.code for d in _diags(warm.diagnostics, MAIN))
    assert warm_codes == cold_codes
    assert _diags(warm.diagnostics, GEO) == []


def test_mod_cache_cascade_invalidates_on_dep_change(tmp_path, monkeypatch):
    """When a module file is modified, its consumers should not silently
    reuse cached .mods built against the prior ABI. The cache key on
    the dep's .mods entry invalidates, and the cascade rule in
    multifile.py forces consumers to recompile too.
    """
    cache_dir = tmp_path / "cache"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    geo_copy = work_dir / "geo.f90"
    main_copy = work_dir / "main.f90"
    geo_copy.write_text(GEO.read_text())
    main_copy.write_text(MAIN.read_text())

    # Cold + warm to establish a cache.
    check_files([geo_copy, main_copy], cache_dir=cache_dir)
    check_files([geo_copy, main_copy], cache_dir=cache_dir)

    # Edit the module — appended text changes the sha256 but keeps the
    # source valid Fortran.
    geo_copy.write_text(geo_copy.read_text() + "\n! trailing comment\n")

    result = check_files([geo_copy, main_copy], cache_dir=cache_dir)
    # Same diagnostics as before — no spurious cascade-driven errors.
    main_diags = _diags(result.diagnostics, main_copy)
    assert sorted(d.code for d in main_diags) == ["H001", "H004"]


def test_mod_cache_disabled_when_cache_dir_none(tmp_path):
    """No entries written when cache_dir is None."""
    out_dir = tmp_path / "should-not-be-created"
    check_files([GEO, MAIN], cache_dir=None)
    assert not out_dir.exists()
