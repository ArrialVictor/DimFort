"""End-to-end test of `dimfort check` via the in-process entry point.

We call ``cli.main()`` directly with synthetic argv rather than spawning
a subprocess — the surface we care about is the exit code and stdout
text, which are the same.

Skipped when ``lfortran`` is not available.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.cli import main
from dimfort.core import lfortran as lf


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_check_clean_file_returns_zero(capsys):
    rc = main(["check", str(FIXTURES / "smoke_basic.f90"), "--no-color"])
    out = capsys.readouterr().out
    assert rc == 0, f"expected exit 0, got {rc}; output:\n{out}"


def test_check_h001_file_returns_one_and_reports(capsys):
    rc = main(["check", str(FIXTURES / "smoke_check.f90"), "--no-color"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "H001" in out
    assert "smoke_check.f90" in out


def test_check_quiet_suppresses_output(capsys):
    rc = main(
        ["check", str(FIXTURES / "smoke_check.f90"), "--no-color", "--quiet"]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert out == ""


def test_check_missing_file_returns_two(capsys):
    rc = main(["check", "/nonexistent/path.f90", "--no-color"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "file not found" in err
