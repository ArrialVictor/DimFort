"""Tests for the ``dimfort coverage`` CLI subcommand.

Exercises the human-readable table output and the ``--json`` flag,
plus the basic usage-error paths (missing path / no Fortran sources).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _clean_src(tmp_path: Path) -> Path:
    f = tmp_path / "clean.f90"
    f.write_text(
        "subroutine clean(x, y, z)\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y  !< @unit{m}\n"
        "  real :: z  !< @unit{m}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    return f


def test_coverage_human_readable_table(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Default invocation prints a table with the per-file row and total."""
    from dimfort.cli import main

    src = _clean_src(tmp_path)
    rc = main(["coverage", "--no-color", str(src)])
    captured = capsys.readouterr().out

    assert rc == 0
    # Header column names are present.
    assert "OK" in captured
    assert "Warn" in captured
    assert "Fire" in captured
    assert "Coverage" in captured
    # Per-file row mentions the file path.
    assert "clean.f90" in captured
    # Workset total footer.
    assert "Workset total" in captured


def test_coverage_summary_omits_per_file_rows(tmp_path: Path, capsys: pytest.CaptureFixture):
    """``--summary`` prints only the total, skipping per-file rows."""
    from dimfort.cli import main

    src = _clean_src(tmp_path)
    rc = main(["coverage", "--no-color", "--summary", str(src)])
    captured = capsys.readouterr().out

    assert rc == 0
    assert "Workset total" in captured
    # The per-file row would mention clean.f90; --summary suppresses it.
    assert "clean.f90" not in captured


def test_coverage_json_output(tmp_path: Path, capsys: pytest.CaptureFixture):
    """``--json`` emits parseable JSON with the documented shape."""
    from dimfort.cli import main

    src = _clean_src(tmp_path)
    rc = main(["coverage", "--json", str(src)])
    captured = capsys.readouterr().out

    assert rc == 0
    payload = json.loads(captured)
    assert "files" in payload
    assert "total" in payload
    assert len(payload["files"]) == 1
    f = payload["files"][0]
    assert set(f.keys()) == {"path", "ok", "warn", "fire", "unparsed", "out", "coverage_pct"}
    t = payload["total"]
    assert set(t.keys()) == {"ok", "warn", "fire", "unparsed", "out", "coverage_pct"}
    # The annotated clean file should have at least one green line.
    assert t["ok"] >= 1


def test_coverage_returns_2_on_missing_path(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Documented exit-2 contract: missing path."""
    from dimfort.cli import main

    rc = main(["coverage", str(tmp_path / "does-not-exist.f90")])
    err = capsys.readouterr().err

    assert rc == 2
    assert "path not found" in err


def test_coverage_returns_2_on_no_fortran_sources(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Documented exit-2 contract: directory with no Fortran sources."""
    from dimfort.cli import main

    # An empty directory with no .f90 files inside.
    rc = main(["coverage", str(tmp_path)])
    err = capsys.readouterr().err

    assert rc == 2
    assert "no Fortran sources found" in err
