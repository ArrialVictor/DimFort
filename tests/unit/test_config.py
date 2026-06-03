
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


def test_units_file_resolved_relative_to_config(tmp_path):
    """``[units] file`` is resolved relative to the .dimfort.toml directory.

    Mirrors how src_paths / include_paths are handled — a relative
    path like ``user_units.toml`` should not depend on the current
    working directory of whatever invoked DimFort.
    """
    (tmp_path / "user_units.toml").write_text("[derived]\nday = { expr = \"s\" }\n")
    (tmp_path / CONFIG_FILENAME).write_text("""
[units]
file = "user_units.toml"
""")
    cfg = load_config(tmp_path)
    assert cfg.units_file == (tmp_path / "user_units.toml").resolve()


def test_units_file_missing_field_means_none(tmp_path):
    """Without ``[units] file``, the resolved value is ``None`` — i.e. use defaults."""
    (tmp_path / CONFIG_FILENAME).write_text("[project]\nsrc_paths = []\n")
    cfg = load_config(tmp_path)
    assert cfg.units_file is None


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


def test_install_default_picks_up_user_units(tmp_path):
    """``install_default`` re-builds ``DEFAULT_TABLE`` so user units parse.

    Before this wiring, a real-world workspace whose annotations used
    ``hPa`` / ``degrees`` / ``day`` saw U002 errors because the
    shipped table only knows base SI units.
    """
    from dimfort.core import unit_config
    from dimfort.core import units as _units_mod
    user_file = tmp_path / "user.toml"
    user_file.write_text(
        '[derived]\n'
        'day = { expr = "s" }\n'
    )
    original_table = _units_mod.DEFAULT_TABLE
    try:
        unit_config.install_default(user_file)
        # parsing "day" must now succeed against the installed default
        result = _units_mod.parse("day", _units_mod.DEFAULT_TABLE)
        assert result is not None
    finally:
        _units_mod.DEFAULT_TABLE = original_table


def test_install_default_tolerates_missing_file(tmp_path):
    """A missing user-units file leaves the shipped default in place; no raise."""
    from dimfort.core import unit_config
    from dimfort.core import units as _units_mod
    original_table = _units_mod.DEFAULT_TABLE
    try:
        unit_config.install_default(tmp_path / "nonexistent.toml")
        assert _units_mod.DEFAULT_TABLE is original_table
    finally:
        _units_mod.DEFAULT_TABLE = original_table


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


# ---------------------------------------------------------------------------
# Unit comment delimiters (0.2.2)
# ---------------------------------------------------------------------------


def test_unit_delimiters_default_when_unset(tmp_path):
    """No keys set → defaults preserve today's canonical forms."""
    from dimfort.config import (
        DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS,
        DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS,
        DEFAULT_UNIT_COMMENT_DELIMITERS,
    )
    (tmp_path / CONFIG_FILENAME).write_text("[parser]\n")
    cfg = load_config(tmp_path)
    assert cfg.unit_comment_delimiters == DEFAULT_UNIT_COMMENT_DELIMITERS
    assert cfg.unit_assume_comment_delimiters == DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS
    assert cfg.unit_affine_comment_delimiters == DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS


def test_unit_delimiters_custom_entries(tmp_path):
    from dimfort.config import StructuredPatternEntry, UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "{{",           close = "}}", sep = "::" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
""")
    cfg = load_config(tmp_path)
    assert cfg.unit_comment_delimiters == (
        UnitPatternEntry(open="@unit{", close="}"),
        UnitPatternEntry(open="[", close="]"),
    )
    assert cfg.unit_assume_comment_delimiters == (
        StructuredPatternEntry(open="@unit_assume{", close="}", sep=":"),
        StructuredPatternEntry(open="{{", close="}}", sep="::"),
    )
    assert cfg.unit_affine_comment_delimiters == (
        StructuredPatternEntry(open="@unit_affine_conversion{", close="}", sep="->"),
    )


def test_unit_delimiters_explicit_empty_logs_and_defaults(tmp_path, caplog):
    """An explicitly empty list logs an error and falls back to the default."""
    import logging

    from dimfort.config import DEFAULT_UNIT_COMMENT_DELIMITERS
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_comment_delimiters = []
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comment_delimiters == DEFAULT_UNIT_COMMENT_DELIMITERS
    assert any("explicitly empty" in r.message for r in caplog.records)


def test_unit_delimiters_missing_required_field_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comment_delimiters == (UnitPatternEntry(open="@unit{", close="}"),)
    assert any("close" in r.message for r in caplog.records)


def test_unit_delimiters_unknown_key_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[", close = "]", sep = ":" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comment_delimiters == (UnitPatternEntry(open="@unit{", close="}"),)
    assert any("unknown key" in r.message for r in caplog.records)


def test_unit_delimiters_duplicate_entries_dropped(tmp_path, caplog):
    import logging

    from dimfort.config import UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "@unit{", close = "}" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comment_delimiters == (UnitPatternEntry(open="@unit{", close="}"),)
    assert any("duplicate" in r.message for r in caplog.records)


def test_structured_delimiters_sep_in_open_is_error(tmp_path, caplog):
    import logging

    from dimfort.config import DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_assume_comment_delimiters = [
  { open = "@x:y{", close = "}", sep = ":" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_assume_comment_delimiters == DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS
    assert any("must not appear" in r.message for r in caplog.records)


def test_structured_delimiters_missing_sep_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import StructuredPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "{{", close = "}}" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_assume_comment_delimiters == (
        StructuredPatternEntry(open="@unit_assume{", close="}", sep=":"),
    )
    assert any("sep" in r.message for r in caplog.records)
