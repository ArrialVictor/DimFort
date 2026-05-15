from pathlib import Path

from dimfort.config import (
    CONFIG_FILENAME,
    DimfortConfig,
    find_config,
    load_config,
)


def test_no_config_returns_empty(tmp_path):
    cfg = load_config(tmp_path)
    assert cfg == DimfortConfig()


def test_find_walks_upward(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / CONFIG_FILENAME).write_text("")
    found = find_config(nested)
    assert found == (tmp_path / CONFIG_FILENAME).resolve()


def test_find_starts_from_file_parent(tmp_path):
    (tmp_path / CONFIG_FILENAME).write_text("")
    src = tmp_path / "src.f90"
    src.write_text("")
    assert find_config(src) == (tmp_path / CONFIG_FILENAME).resolve()


def test_parses_all_sections(tmp_path):
    (tmp_path / CONFIG_FILENAME).write_text("""
[project]
src_paths = ["src/", "dyn3d_common/"]

[workset]
max_size = 80
external_modules = ["ioipsl", "netcdf", "MPI"]

[lfortran]
binary = "tools/lfortran"
""")
    cfg = load_config(tmp_path)
    assert cfg.config_path == (tmp_path / CONFIG_FILENAME).resolve()
    assert cfg.src_paths == (
        (tmp_path / "src").resolve(),
        (tmp_path / "dyn3d_common").resolve(),
    )
    assert cfg.max_workset_size == 80
    # External modules lower-cased.
    assert cfg.external_modules == ("ioipsl", "netcdf", "mpi")
    assert cfg.lfortran_binary == (tmp_path / "tools" / "lfortran").resolve()


def test_malformed_returns_empty_but_records_path(tmp_path):
    cfg_file = tmp_path / CONFIG_FILENAME
    cfg_file.write_text("not = valid = toml")
    cfg = load_config(tmp_path)
    assert cfg.config_path == cfg_file.resolve()
    assert cfg.max_workset_size is None
    assert cfg.src_paths == ()


def test_unknown_keys_ignored(tmp_path):
    (tmp_path / CONFIG_FILENAME).write_text("""
[project]
src_paths = ["x"]
mystery_field = "ignored"

[future_section]
anything = 42
""")
    cfg = load_config(tmp_path)
    assert cfg.src_paths == ((tmp_path / "x").resolve(),)


def test_invalid_max_size_is_dropped(tmp_path):
    (tmp_path / CONFIG_FILENAME).write_text("""
[workset]
max_size = -5
""")
    cfg = load_config(tmp_path)
    assert cfg.max_workset_size is None


def test_string_max_size_is_dropped(tmp_path):
    (tmp_path / CONFIG_FILENAME).write_text("""
[workset]
max_size = "lots"
""")
    cfg = load_config(tmp_path)
    assert cfg.max_workset_size is None
