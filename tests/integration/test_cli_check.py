"""End-to-end test of `dimfort check` via the in-process entry point.

Calls ``cli.main()`` directly with synthetic argv rather than spawning
a subprocess — the surface we care about is the exit code and stdout
text, both of which are the same as the real CLI.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.cli import main

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_check_clean_file_returns_zero(capsys):
    """A fixture with valid units and no mismatches exits 0."""
    rc = main(["check", str(FIXTURES / "smoke_basic.f90"), "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0, f"expected exit 0, got {rc}; output:\n{out}"


def test_check_h001_file_returns_one_and_reports(capsys):
    """An H001-triggering fixture exits 1 and prints the diagnostic."""
    rc = main(["check", str(FIXTURES / "smoke_check.f90"), "--no-color"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "H001" in out
    assert "smoke_check.f90" in out


def test_check_quiet_suppresses_output(capsys):
    """``--quiet`` keeps the exit code but suppresses stdout."""
    rc = main(
        ["check", str(FIXTURES / "smoke_check.f90"), "--no-color", "--quiet"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert out == ""


def test_check_missing_file_returns_two(capsys):
    """A missing file is a usage error → exit 2 with an stderr message."""
    rc = main(["check", "/nonexistent/path.f90", "--no-color"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "path not found" in err


def test_check_directory_walks_fortran_sources(tmp_path, capsys):
    """A directory argument is walked recursively for Fortran files."""
    (tmp_path / "sub").mkdir()
    bad = tmp_path / "sub" / "bad.f90"
    bad.write_text(
        "program p\n"
        "  real :: m  !< @unit{kg}\n"
        "  real :: v  !< @unit{m/s}\n"
        "  m = v\n"
        "end program p\n"
    )
    (tmp_path / "readme.txt").write_text("not fortran")
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "H001" in out
    assert "bad.f90" in out


def test_check_directory_with_no_sources_returns_two(tmp_path, capsys):
    """A directory containing no Fortran files is a usage error."""
    (tmp_path / "readme.txt").write_text("nothing")
    rc = main(["check", str(tmp_path), "--no-color"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no Fortran sources" in err


def test_check_summary_emits_per_file_counts(tmp_path, capsys):
    """`--summary` prints a per-file H/U breakdown after diagnostics."""
    f = tmp_path / "bad.f90"
    f.write_text(
        "program p\n"
        "  real :: m  !< @unit{kg}\n"
        "  real :: v  !< @unit{m/s}\n"
        "  m = v\n"
        "end program p\n"
    )
    rc = main(["check", str(f), "--no-color", "--summary"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Summary" in out
    assert "1 H" in out
    assert "file(s)" in out


def test_bracket_pattern_attaches_to_all_multivar_names(tmp_path, capsys):
    """End-to-end (spec §6, Q1-unified): a configured `[`/`]` pattern
    on a multi-variable declaration attaches to every name; no U022."""
    (tmp_path / "dimfort.toml").write_text(
        '[parser.unit_comments]\n'
        'unit = [\n'
        '  { open = "@unit{", close = "}" },\n'
        '  { open = "[",      close = "]" },\n'
        ']\n'
    )
    (tmp_path / "src.f90").write_text(
        "subroutine s\n"
        "  real :: a, b, c   ! [m/s]\n"
        "end subroutine\n"
    )
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert "U022" not in out
    assert rc == 0


def test_u021_fires_on_disagreeing_pattern_captures(tmp_path, capsys):
    """Spec §8.2: two patterns matching the same comment with
    different capture text → U021 WARNING; the first-listed capture
    is the one that attaches."""
    (tmp_path / "dimfort.toml").write_text(
        '[parser.unit_comments]\n'
        'unit = [\n'
        '  { open = "@unit{", close = "}" },\n'
        '  { open = "[",      close = "]" },\n'
        ']\n'
    )
    (tmp_path / "src.f90").write_text(
        "subroutine s\n"
        "  real :: v   !< wind speed [m/s] @unit{kg}\n"
        "end subroutine\n"
    )
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert "U021" in out, out
    assert "'kg'" in out
    assert "'m/s'" in out
    assert rc == 0


def test_u021_silent_on_identical_captures(tmp_path, capsys):
    """Spec §8.2: identical captures across patterns produce no
    diagnostic."""
    (tmp_path / "dimfort.toml").write_text(
        '[parser.unit_comments]\n'
        'unit = [\n'
        '  { open = "@unit{", close = "}" },\n'
        '  { open = "[",      close = "]" },\n'
        ']\n'
    )
    (tmp_path / "src.f90").write_text(
        "subroutine s\n"
        "  real :: v   !< @unit{m/s} also [m/s]\n"
        "end subroutine\n"
    )
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert "U021" not in out
    assert rc == 0


def test_u023_fires_on_at_unit_on_assignment(tmp_path, capsys):
    """End-to-end: ``!< @unit{m/s}`` on an assignment statement is
    wrong-kind. The orphan reroutes to U023 with the right hint."""
    (tmp_path / "src.f90").write_text(
        "subroutine s\n"
        "  real :: v\n"
        "  v = 1.0   !< @unit{m/s}\n"
        "end subroutine\n"
    )
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert "U023" in out, out
    assert "@unit_assume" in out or "@unit_affine_conversion" in out
    assert "U006" not in out
    assert rc == 0


def test_u023_fires_on_assume_on_declaration(tmp_path, capsys):
    """End-to-end: ``!< @unit_assume`` on a declaration is dropped
    and surfaced as U023."""
    (tmp_path / "src.f90").write_text(
        "subroutine s\n"
        "  real :: v   !< @unit_assume{m/s: legacy fit}\n"
        "end subroutine\n"
    )
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert "U023" in out, out
    assert "@unit_assume" in out
    assert rc == 0


def test_u002_includes_suggested_rewrite_for_digit_suffix(tmp_path, capsys):
    """End-to-end: ``@unit{m2/s}`` is unparseable; the rewrite
    detector suggests ``m^2/s`` and the U002 message includes
    'did you mean'."""
    (tmp_path / "src.f90").write_text(
        "subroutine s\n"
        "  real :: a   !< @unit{m2/s}\n"
        "end subroutine\n"
    )
    rc = main(["check", str(tmp_path), "--no-color"])
    out = capsys.readouterr().out
    assert "U002" in out, out
    assert "did you mean" in out
    assert "'m^2/s'" in out
    assert rc == 1


def test_lsp_extra_missing_message_pygls():
    """A missing-pygls ImportError yields a friendly install-fix message."""
    from dimfort.cli import _lsp_extra_missing_message

    exc = ImportError("No module named 'pygls'", name="pygls")
    msg = _lsp_extra_missing_message(exc)
    assert msg is not None
    assert "pipx install 'dimfort[lsp]'" in msg
    assert "pip install 'dimfort[lsp]'" in msg
    assert "pygls" in msg


def test_lsp_extra_missing_message_lsprotocol():
    from dimfort.cli import _lsp_extra_missing_message

    exc = ImportError("No module named 'lsprotocol'", name="lsprotocol")
    assert _lsp_extra_missing_message(exc) is not None


def test_lsp_extra_missing_message_nested_module_path():
    """``from lsprotocol import types`` fails with name='lsprotocol.types'."""
    from dimfort.cli import _lsp_extra_missing_message

    exc = ImportError(
        "No module named 'lsprotocol.types'", name="lsprotocol.types",
    )
    assert _lsp_extra_missing_message(exc) is not None


def test_lsp_extra_missing_message_unrelated_returns_none():
    """An unrelated ImportError (e.g., a real bug in our code) returns
    None so the caller re-raises the bare traceback rather than masking
    a genuine bug with a misleading install-fix message."""
    from dimfort.cli import _lsp_extra_missing_message

    exc = ImportError("No module named 'totally_unrelated'", name="totally_unrelated")
    assert _lsp_extra_missing_message(exc) is None


def test_lsp_extra_missing_message_no_name_attr_returns_none():
    """Defensive: an ImportError without ``name`` set (uncommon) shouldn't
    crash the helper, just decline to claim it's our case."""
    from dimfort.cli import _lsp_extra_missing_message

    exc = ImportError("something opaque")
    assert _lsp_extra_missing_message(exc) is None
