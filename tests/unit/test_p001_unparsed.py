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


def test_p001_localizes_not_whole_routine(tmp_path: Path):
    """Tree-sitter often wraps a bad statement in an outer ERROR spanning the
    whole routine; P001 must report the *innermost* error, not blue-underline
    every line of the subroutine."""
    src = tmp_path / "mid.f90"
    src.write_text(
        "subroutine s\n"                 # 1
        "  real :: a  !< @unit{m}\n"     # 2 (valid decl)
        "  a = 1.0\n"                    # 3 (valid)
        "  a = * / +\n"                  # 4 (unparseable)
        "  a = 2.0\n"                    # 5 (valid)
        "end subroutine\n"              # 6
    )
    result = check_files([src])
    p = _p001(result, src)
    assert len(p) == 1, [(d.start.line, d.end.line) for d in p]
    # The marker sits on the bad line, not at the subroutine header (line 1),
    # and doesn't span the whole routine.
    assert p[0].start.line == 4
    assert p[0].end.line <= 5


def test_p001_widens_to_swallowed_neighbor(tmp_path: Path):
    """Tree-sitter's error recovery commonly swallows the immediately-following
    clean statement into the bad statement's parse node (the parent
    assignment_statement spans both lines with ``has_error=True``). The panel
    produces a degraded Expression view on the swallowed line, so P001's range
    is widened to that statement-level ancestor — covering both lines — rather
    than just the bad line. Otherwise users see a single blue squiggle plus a
    silently-empty Expression panel on the (apparently clean) line below."""
    src = tmp_path / "swallow.f90"
    src.write_text(
        "subroutine s\n"                 # 1
        "  real :: v  !< @unit{m}\n"     # 2
        "  v = * / +\n"                  # 3 (unparseable)
        "  v = 0.0\n"                    # 4 (clean, but swallowed by tree-sitter)
        "end subroutine\n"               # 5
    )
    result = check_files([src])
    p = _p001(result, src)
    assert len(p) == 1
    assert p[0].start.line == 3
    # Widened to cover line 4 — the swallowed-by-error-recovery neighbor.
    assert p[0].end.line == 4


def test_p001_does_not_widen_when_neighbor_is_clean(tmp_path: Path):
    """When tree-sitter's recovery doesn't swallow the next statement (e.g.
    two statements follow the bad line; only the first is contaminated), P001
    widens to the swallowed one but NOT to subsequent clean lines."""
    src = tmp_path / "two_after.f90"
    src.write_text(
        "subroutine s\n"                 # 1
        "  real :: v, w  !< @unit{m}\n"  # 2
        "  v = * / +\n"                  # 3 (unparseable)
        "  v = 0.0\n"                    # 4 (swallowed)
        "  w = v + 1.0\n"                # 5 (clean)
        "end subroutine\n"               # 6
    )
    result = check_files([src])
    p = _p001(result, src)
    assert len(p) == 1
    # P001 covers 3-4, not 3-5.
    assert (p[0].start.line, p[0].end.line) == (3, 4)


def test_p001_suppressible_via_override(tmp_path: Path):
    src = tmp_path / "bad.f90"
    src.write_text(_BAD)
    set_severity_overrides({"P001": "off"})
    try:
        result = check_files([src])
        assert _p001(result, src) == []
    finally:
        set_severity_overrides({})
