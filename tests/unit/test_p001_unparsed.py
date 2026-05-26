"""P001 marks regions tree-sitter could not parse — DimFort makes no unit
guarantee there. INFO severity (blue squiggle); suppressible via override.
See docs/design/unparsed-regions.md."""
from __future__ import annotations

from pathlib import Path

from dimfort.core.diagnostics import Severity, set_severity_overrides
from dimfort.core.multifile import check_files

# A snippet tree-sitter recovers from but leaves an ERROR region in: the
# surrounding declaration/assignment still parse, the ``do = = =`` line does not.
_BAD = (
    "subroutine s\n"               # 1
    "  real :: a  !< @unit{m}\n"   # 2 (valid)
    "  do = = =\n"                 # 3 (unparseable)
    "  a = 1.0\n"                  # 4
    "end subroutine\n"
)


def _p001(result, path: Path):
    return [d for d in result.diagnostics.get(path.resolve(), []) if d.code == "P001"]


def test_p001_marks_unparsed_region(tmp_path: Path):
    src = tmp_path / "bad.f90"
    src.write_text(_BAD)
    result = check_files([src])
    p = _p001(result, src)
    assert len(p) == 1, [d.code for d in result.diagnostics.get(src.resolve(), [])]
    assert p[0].severity == Severity.INFO
    assert p[0].start.line == 3  # the unparseable line
    assert "parse" in p[0].message.lower()


def test_p001_absent_for_valid_file(tmp_path: Path):
    src = tmp_path / "ok.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: a  !< @unit{m}\n"
        "  a = 1.0\n"
        "end subroutine\n"
    )
    result = check_files([src])
    assert _p001(result, src) == []


def test_p001_suppressible_via_override(tmp_path: Path):
    src = tmp_path / "bad.f90"
    src.write_text(_BAD)
    set_severity_overrides({"P001": "off"})
    try:
        result = check_files([src])
        assert _p001(result, src) == []
    finally:
        set_severity_overrides({})
