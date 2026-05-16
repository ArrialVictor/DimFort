from pathlib import Path

from dimfort.config import (
    CONFIG_FILENAME,
    DimfortConfig,
    find_config,
    load_config,
)


def test_no_config_returns_empty(tmp_path):
    """No ``.dimfort.toml`` anywhere → an empty :class:`DimfortConfig`."""
    cfg = load_config(tmp_path)
    assert cfg == DimfortConfig()


def test_find_walks_upward(tmp_path):
    """``find_config`` ascends parent directories until it hits the file."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (tmp_path / CONFIG_FILENAME).write_text("")
    found = find_config(nested)
    assert found == (tmp_path / CONFIG_FILENAME).resolve()


def test_find_starts_from_file_parent(tmp_path):
    """When given a file path, ``find_config`` walks from its parent dir."""
    (tmp_path / CONFIG_FILENAME).write_text("")
    src = tmp_path / "src.f90"
    src.write_text("")
    assert find_config(src) == (tmp_path / CONFIG_FILENAME).resolve()


def test_parses_supported_sections(tmp_path):
    """Currently-supported sections (project, workset) parse into the dataclass."""
    (tmp_path / CONFIG_FILENAME).write_text("""
[project]
src_paths = ["src/", "dyn3d_common/"]

[workset]
max_size = 80
external_modules = ["ioipsl", "netcdf", "MPI"]
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


def test_parser_section_picks_up_cpp_defines_and_includes(tmp_path):
    """The ``[parser]`` section feeds CPP defines and include paths into the loader."""
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
cpp_defines = ["ISO", "ISOVERIF"]
include_paths = [".dimfort/stubs", "vendor/headers"]
""")
    cfg = load_config(tmp_path)
    assert cfg.cpp_defines == ("ISO", "ISOVERIF")
    assert cfg.include_paths == (
        (tmp_path / ".dimfort" / "stubs").resolve(),
        (tmp_path / "vendor" / "headers").resolve(),
    )


def test_legacy_lfortran_section_still_provides_cpp_keys(tmp_path):
    """Old ``[lfortran]`` cpp_defines / include_paths keys keep working.

    Projects whose ``.dimfort.toml`` predates the rename should not
    need to be updated to keep CPP preprocessing.
    """
    (tmp_path / CONFIG_FILENAME).write_text("""
[lfortran]
include_paths = [".dimfort/stubs"]
cpp_defines = ["ISO"]
""")
    cfg = load_config(tmp_path)
    assert cfg.cpp_defines == ("ISO",)
    assert cfg.include_paths == ((tmp_path / ".dimfort" / "stubs").resolve(),)


def test_legacy_checker_section_silently_ignored(tmp_path):
    """The pre-tree-sitter ``[checker] backend`` is accepted but no longer surfaced."""
    (tmp_path / CONFIG_FILENAME).write_text("""
[project]
src_paths = ["src/"]

[checker]
backend = "asr"
""")
    cfg = load_config(tmp_path)
    assert cfg.src_paths == ((tmp_path / "src").resolve(),)
    assert not hasattr(cfg, "backend")


def test_malformed_returns_empty_but_records_path(tmp_path):
    """A malformed TOML file logs a warning but doesn't crash the loader."""
    cfg_file = tmp_path / CONFIG_FILENAME
    cfg_file.write_text("not = valid = toml")
    cfg = load_config(tmp_path)
    assert cfg.config_path == cfg_file.resolve()
    assert cfg.max_workset_size is None
    assert cfg.src_paths == ()


def test_unknown_keys_ignored(tmp_path):
    """Future-compatibility: unknown keys / sections never break the loader."""
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
    """A negative ``max_size`` is treated as "unset" rather than accepted."""
    (tmp_path / CONFIG_FILENAME).write_text("""
[workset]
max_size = -5
""")
    cfg = load_config(tmp_path)
    assert cfg.max_workset_size is None


def test_string_max_size_is_dropped(tmp_path):
    """A non-int ``max_size`` is treated as "unset" without raising."""
    (tmp_path / CONFIG_FILENAME).write_text("""
[workset]
max_size = "lots"
""")
    cfg = load_config(tmp_path)
    assert cfg.max_workset_size is None
