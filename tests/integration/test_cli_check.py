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
    assert "file not found" in err
