
from dimfort.config import (
    CONFIG_FILENAME,
    DimfortConfig,
    find_config,
    load_config,
)


def test_no_config_returns_empty(tmp_path):
    """No ``dimfort.toml`` anywhere → an empty :class:`DimfortConfig`."""
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

    Projects whose ``dimfort.toml`` predates the rename should not
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
    """``[units] file`` is resolved relative to the dimfort.toml directory.

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
# [parser.unit_comments] — 0.2.7 nested namespace
# ---------------------------------------------------------------------------


def test_unit_comments_default_when_unset(tmp_path):
    """No section → defaults preserve canonical forms + ship nonunit filters."""
    from dimfort.config import (
        DEFAULT_NONUNIT_AFFINE_PATTERNS,
        DEFAULT_NONUNIT_ASSUME_PATTERNS,
        DEFAULT_NONUNIT_PATTERNS,
        DEFAULT_UNIT_AFFINE_PATTERNS,
        DEFAULT_UNIT_ASSUME_PATTERNS,
        DEFAULT_UNIT_PATTERNS,
    )
    (tmp_path / CONFIG_FILENAME).write_text("[parser]\n")
    cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == DEFAULT_UNIT_PATTERNS
    assert cfg.unit_comments.nonunit == DEFAULT_NONUNIT_PATTERNS
    assert cfg.unit_comments.unit_assume == DEFAULT_UNIT_ASSUME_PATTERNS
    assert cfg.unit_comments.nonunit_assume == DEFAULT_NONUNIT_ASSUME_PATTERNS
    assert cfg.unit_comments.unit_affine == DEFAULT_UNIT_AFFINE_PATTERNS
    assert cfg.unit_comments.nonunit_affine == DEFAULT_NONUNIT_AFFINE_PATTERNS


def test_unit_comments_custom_entries(tmp_path):
    from dimfort.config import StructuredPatternEntry, UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "{{",           close = "}}", sep = "::" },
]
unit_affine = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
""")
    cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == (
        UnitPatternEntry(open="@unit{", close="}"),
        UnitPatternEntry(open="[", close="]"),
    )
    assert cfg.unit_comments.unit_assume == (
        StructuredPatternEntry(open="@unit_assume{", close="}", sep=":"),
        StructuredPatternEntry(open="{{", close="}}", sep="::"),
    )
    assert cfg.unit_comments.unit_affine == (
        StructuredPatternEntry(open="@unit_affine_conversion{", close="}", sep="->"),
    )


def test_unit_comments_unit_explicit_empty_logs_and_defaults(tmp_path, caplog):
    """An explicitly empty ``unit`` list falls back to the default."""
    import logging

    from dimfort.config import DEFAULT_UNIT_PATTERNS
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit = []
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == DEFAULT_UNIT_PATTERNS
    assert any("explicitly empty" in r.message for r in caplog.records)


def test_unit_comments_unit_missing_required_field_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == (UnitPatternEntry(open="@unit{", close="}"),)
    assert any("close" in r.message for r in caplog.records)


def test_unit_comments_unit_unknown_key_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[", close = "]", sep = ":" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == (UnitPatternEntry(open="@unit{", close="}"),)
    assert any("unknown key" in r.message for r in caplog.records)


def test_unit_comments_unit_duplicate_entries_dropped(tmp_path, caplog):
    import logging

    from dimfort.config import UnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "@unit{", close = "}" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == (UnitPatternEntry(open="@unit{", close="}"),)
    assert any("duplicate" in r.message for r in caplog.records)


def test_unit_comments_assume_sep_in_open_is_error(tmp_path, caplog):
    import logging

    from dimfort.config import DEFAULT_UNIT_ASSUME_PATTERNS
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit_assume = [
  { open = "@x:y{", close = "}", sep = ":" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit_assume == DEFAULT_UNIT_ASSUME_PATTERNS
    assert any("must not appear" in r.message for r in caplog.records)


def test_unit_comments_assume_missing_sep_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import StructuredPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
unit_assume = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "{{", close = "}}" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit_assume == (
        StructuredPatternEntry(open="@unit_assume{", close="}", sep=":"),
    )
    assert any("sep" in r.message for r in caplog.records)


def test_unit_comments_nonunit_custom_entries(tmp_path):
    from dimfort.config import NonUnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
nonunit = [
  { open = "@nonunit{", close = "}" },
  { open = "(", close = ")", regex = "^\\\\d{4}$" },
]
""")
    cfg = load_config(tmp_path)
    assert cfg.unit_comments.nonunit == (
        NonUnitPatternEntry(open="@nonunit{", close="}"),
        NonUnitPatternEntry(open="(", close=")", regex=r"^\d{4}$"),
    )


def test_unit_comments_nonunit_explicit_empty_overrides_default(tmp_path):
    """Empty ``nonunit`` is a valid override (opt out of shipped filters)."""
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
nonunit = []
""")
    cfg = load_config(tmp_path)
    assert cfg.unit_comments.nonunit == ()


def test_unit_comments_nonunit_invalid_regex_drops_entry(tmp_path, caplog):
    import logging

    from dimfort.config import NonUnitPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
nonunit = [
  { open = "@nonunit{", close = "}" },
  { open = "(", close = ")", regex = "[unterminated" },
]
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.nonunit == (NonUnitPatternEntry(open="@nonunit{", close="}"),)
    assert any("invalid" in r.message for r in caplog.records)


def test_unit_comments_nonunit_assume_with_optional_sep(tmp_path):
    from dimfort.config import NonStructuredPatternEntry
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_comments]
nonunit_assume = [
  { open = "@unit_assume{", close = "}" },
  { open = "@unit_assume{", close = "}", sep = ":", regex = "^0\\\\s*:" },
]
""")
    cfg = load_config(tmp_path)
    assert cfg.unit_comments.nonunit_assume == (
        NonStructuredPatternEntry(
            open="@unit_assume{", close="}", sep=None, regex=None,
        ),
        NonStructuredPatternEntry(
            open="@unit_assume{", close="}", sep=":", regex=r"^0\s*:",
        ),
    )


def test_legacy_flat_keys_warn_and_ignored(tmp_path, caplog):
    """Pre-0.2.7 ``unit_comment_delimiters`` at ``[parser]`` warns; new defaults apply."""
    import logging

    from dimfort.config import DEFAULT_UNIT_PATTERNS
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser]
unit_comment_delimiters = [{ open = "[", close = "]" }]
""")
    with caplog.at_level(logging.WARNING, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_comments.unit == DEFAULT_UNIT_PATTERNS
    assert any(
        "renamed" in r.message and "unit_comment_delimiters" in r.message
        for r in caplog.records
    )


def test_malformed_toml_sets_load_error(tmp_path):
    """Audit fix: a malformed dimfort.toml must set ``load_error``
    so the CLI can return exit 2 per the documented contract.
    The previous behaviour silently logged + returned empty config —
    breaking the cli.md promise that invalid config exits with 2."""
    (tmp_path / "dimfort.toml").write_text("this is = not valid [ toml\n")
    cfg = load_config(tmp_path)
    assert cfg.load_error is not None
    assert cfg.config_path == tmp_path / "dimfort.toml"


def test_well_formed_toml_load_error_is_none(tmp_path):
    """Sanity: a parseable dimfort.toml must leave load_error as None
    so the CLI keeps its exit 0 / 1 paths intact."""
    (tmp_path / "dimfort.toml").write_text("[project]\nsrc_paths = []\n")
    cfg = load_config(tmp_path)
    assert cfg.load_error is None
