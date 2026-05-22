"""U002 (unparseable unit annotation) must point at the offending
declaration's line, not the top of the file."""
from __future__ import annotations

from pathlib import Path

from dimfort.core.multifile import check_files


def _u002s(result, path: Path):
    return [d for d in result.diagnostics.get(path.resolve(), []) if d.code == "U002"]


def test_u002_lands_on_declaration_line(tmp_path: Path):
    line3 = "  real :: b  !< @unit{??}"
    src = tmp_path / "x.f90"
    src.write_text(
        "subroutine s\n"               # line 1
        "  real :: a  !< @unit{m}\n"    # line 2 (valid)
        f"{line3}\n"                    # line 3 (invalid → U002 here)
        "end subroutine\n"
    )
    result = check_files([src])
    u002 = _u002s(result, src)
    assert len(u002) == 1
    assert u002[0].start.line == 3
    assert "b" in u002[0].message
    # The squiggle covers the ``@unit{??}`` token itself, not a
    # zero-width point at column 0. Columns are 1-based.
    at = line3.index("@unit{") + 1
    assert u002[0].start.column == at
    assert u002[0].end.column == at + len("@unit{??}")


def test_u002_multiple_on_distinct_lines(tmp_path: Path):
    src = tmp_path / "y.f90"
    src.write_text(
        "subroutine s\n"               # 1
        "  real :: a  !< @unit{??}\n"   # 2 → U002
        "  real :: b  !< @unit{m}\n"    # 3 valid
        "  real :: c  !< @unit{$$}\n"   # 4 → U002
        "end subroutine\n"
    )
    result = check_files([src])
    lines = sorted(d.start.line for d in _u002s(result, src))
    assert lines == [2, 4]
