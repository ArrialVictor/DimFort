"""Tests for the core per-line coverage projection.

Covers ``dimfort.core.coverage.project_file`` and the aggregate
helpers. The LSP wire-format wrapper has its own test module under
``test_lsp_coverage.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _clean_src(tmp_path: Path) -> Path:
    """A short, fully-annotated routine with a dimensionally clean body."""
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


def _mixed_src(tmp_path: Path) -> Path:
    """A routine with an H002 (operand dimension mismatch) on line 5."""
    f = tmp_path / "mixed.f90"
    f.write_text(
        "subroutine mixed(x, y, z)\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y  !< @unit{s}\n"
        "  real :: z  !< @unit{m}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    return f


def _u005_src(tmp_path: Path) -> Path:
    """A routine where one operand is unannotated; should fire U005."""
    f = tmp_path / "u005.f90"
    f.write_text(
        "subroutine u005(x, y, z)\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y\n"
        "  real :: z  !< @unit{m}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    return f


# ---------------------------------------------------------------------------
# project_file
# ---------------------------------------------------------------------------


def test_project_file_clean_routine_paints_declarations_and_use_sites_green(tmp_path: Path):
    """A fully-clean routine: declarations and the assignment line all green."""
    from dimfort.core.coverage import project_file
    from dimfort.core.multifile import check_files

    src = _clean_src(tmp_path)
    result = check_files([src])
    statuses = project_file(src.resolve(), result)

    # Lines 2, 3, 4 are declarations carrying @unit{} annotations.
    assert statuses.get(2) == "green"
    assert statuses.get(3) == "green"
    assert statuses.get(4) == "green"
    # Line 5 is the assignment `z = x + y`: identifiers x, y, z all annotated.
    assert statuses.get(5) == "green"
    # Lines 1 and 6 (subroutine / end subroutine) have no annotated
    # identifiers and no diagnostics: out of scope.
    assert 1 not in statuses
    assert 6 not in statuses


def test_project_file_dimension_mismatch_paints_red(tmp_path: Path):
    """H002 owns the assignment line: must paint red, not green."""
    from dimfort.core.coverage import project_file
    from dimfort.core.multifile import check_files

    src = _mixed_src(tmp_path)
    result = check_files([src])
    statuses = project_file(src.resolve(), result)

    # Declarations stay green (their @unit{} parsed fine).
    assert statuses.get(2) == "green"
    assert statuses.get(3) == "green"
    assert statuses.get(4) == "green"
    # Assignment fires H002 and must paint red, beating green.
    assert statuses.get(5) == "red"


def test_project_file_u005_propagates_yellow_to_use_sites(tmp_path: Path):
    """U005 fires on the declaration line; the projection propagates
    yellow to every line that references the unannotated name.

    Counter-intuitive behaviour we are deliberately avoiding: removing
    an annotation should NOT make use sites of the now-unannotated
    variable look "better" (green from the other annotated identifiers
    on the same line). U005 propagation keeps the use sites yellow
    until the annotation is restored.
    """
    from dimfort.core.coverage import project_file
    from dimfort.core.multifile import check_files

    src = _u005_src(tmp_path)
    result = check_files([src])
    statuses = project_file(src.resolve(), result)

    # Annotated declarations on lines 2 and 4 stay green.
    assert statuses.get(2) == "green"
    assert statuses.get(4) == "green"
    # The unannotated declaration of y on line 3 fires U005 → yellow.
    assert statuses.get(3) == "yellow"
    # The assignment line uses x (annotated), y (unannotated), z
    # (annotated). Yellow wins via U005 propagation to use sites.
    assert statuses.get(5) == "yellow"


def test_project_file_removing_annotation_does_not_make_use_site_greener(
    tmp_path: Path,
):
    """Regression for the qa.f90 observation 2026-06-06.

    Originally ``bogus = c_sound * t`` fires H001 (bogus is kg, RHS
    resolves to m/s · s = m). Removing ``@unit{s}`` from ``t`` makes
    the checker unable to compute the RHS, so H001 no longer fires.
    Without U005 propagation, the line would fall back to green via
    the other annotated identifiers (bogus, c_sound) — a clearly wrong
    signal ("removing an annotation made the line look fine"). With
    propagation, the line stays yellow because it references the
    now-unannotated t.
    """
    from dimfort.core.coverage import project_file
    from dimfort.core.multifile import check_files

    f_with = tmp_path / "with.f90"
    f_with.write_text(
        "subroutine demo(c_sound, t, bogus)\n"
        "  real :: c_sound  !< @unit{m/s}\n"
        "  real :: t        !< @unit{s}\n"
        "  real :: bogus    !< @unit{kg}\n"
        "  bogus = c_sound * t\n"
        "end subroutine\n"
    )
    f_without = tmp_path / "without.f90"
    f_without.write_text(
        "subroutine demo(c_sound, t, bogus)\n"
        "  real :: c_sound  !< @unit{m/s}\n"
        "  real :: t\n"
        "  real :: bogus    !< @unit{kg}\n"
        "  bogus = c_sound * t\n"
        "end subroutine\n"
    )
    res_with = check_files([f_with])
    res_without = check_files([f_without])

    s_with = project_file(f_with.resolve(), res_with)
    s_without = project_file(f_without.resolve(), res_without)

    # With the annotation, the assignment line is red (H001 fires).
    assert s_with.get(5) == "red"
    # Without the annotation, H001 cannot fire. The use line must
    # NOT become green — U005 propagation keeps it yellow.
    assert s_without.get(5) == "yellow", (
        f"removing the annotation made line 5 look like {s_without.get(5)!r}; "
        f"U005 propagation should keep it yellow"
    )


def test_project_file_no_workset_entry_returns_empty(tmp_path: Path):
    """A path not in the workset projects to an empty status map."""
    from dimfort.core.coverage import project_file
    from dimfort.core.multifile import check_files

    src = _clean_src(tmp_path)
    result = check_files([src])
    other = tmp_path / "not-checked.f90"
    statuses = project_file(other.resolve(), result)
    assert statuses == {}


def test_project_file_no_annotations_returns_only_diagnostic_lines(tmp_path: Path):
    """A file with no annotations contributes no green lines, but any
    diagnostics still paint their tier."""
    from dimfort.core.coverage import project_file
    from dimfort.core.multifile import check_files

    f = tmp_path / "noann.f90"
    f.write_text(
        "subroutine noann(x)\n"
        "  real :: x\n"
        "  x = x + 1.0\n"
        "end subroutine\n"
    )
    result = check_files([f])
    statuses = project_file(f.resolve(), result)

    # No @unit{} → no green paint. Any diagnostic that fires (H010 on
    # the assignment because RHS is a bare literal, etc.) still paints
    # its tier; pure-no-decoration is also acceptable. Either way: no
    # green.
    assert "green" not in statuses.values()


def test_project_file_worst_tier_wins_on_overlapping_diagnostics(tmp_path: Path):
    """When a single line carries two diagnostics whose tiers differ,
    the worse tier wins (worst-of-children semantics)."""
    # H002 (operand dimension mismatch) on the same line as H010
    # (hint-level fire) — exercise the ``red > yellow`` step in the
    # _TIER_ORDER ranking by constructing a Diagnostic directly and
    # feeding it to a synthetic WorksetResult.
    from dimfort.core.coverage import project_file
    from dimfort.core.diagnostics import Diagnostic, Position, Severity
    from dimfort.core.multifile import WorksetResult

    path = (tmp_path / "synthetic.f90").resolve()
    result = WorksetResult()
    pos_a = Position(line=5, column=1)
    pos_b = Position(line=5, column=10)
    result.diagnostics[path] = [
        Diagnostic(
            file=str(path), start=pos_a, end=pos_b,
            severity=Severity.WARNING, code="H010", message="hint",
        ),
        Diagnostic(
            file=str(path), start=pos_a, end=pos_b,
            severity=Severity.ERROR, code="H002", message="dim mismatch",
        ),
    ]
    statuses = project_file(path, result)
    # Worst-wins: red beats yellow on line 5.
    assert statuses.get(5) == "red"


# ---------------------------------------------------------------------------
# Aggregate helpers
# ---------------------------------------------------------------------------


def test_aggregate_file_counts_each_tier(tmp_path: Path):
    """Tier counts must match the per-line status distribution."""
    from dimfort.core.coverage import aggregate_file

    statuses = {
        2: "green",
        3: "green",
        4: "yellow",
        5: "red",
        7: "blue",
    }
    path = Path(tmp_path) / "x.f90"
    cov = aggregate_file(path, statuses, total_lines=10)

    assert cov.ok == 2
    assert cov.warn == 1
    assert cov.fire == 1
    assert cov.unparsed == 1
    # total 10, in-scope 5 → out is 5.
    assert cov.out == 5
    assert cov.coverage_pct == pytest.approx(40.0)


def test_aggregate_file_all_out_of_scope_yields_zero_pct(tmp_path: Path):
    """A file with no in-scope lines reports 0.0% coverage."""
    from dimfort.core.coverage import aggregate_file

    cov = aggregate_file(tmp_path / "x.f90", statuses={}, total_lines=20)
    assert cov.ok == 0
    assert cov.warn == 0
    assert cov.fire == 0
    assert cov.unparsed == 0
    assert cov.out == 20
    assert cov.coverage_pct == 0.0


def test_aggregate_workset_sums_per_file_tiers(tmp_path: Path):
    """Workset aggregate sums the per-file counts and recomputes pct."""
    from dimfort.core.coverage import FileCoverage, aggregate_workset

    rows = [
        FileCoverage(path=tmp_path / "a.f90", ok=10, warn=2, fire=0, unparsed=0, out=5),
        FileCoverage(path=tmp_path / "b.f90", ok=4, warn=0, fire=1, unparsed=0, out=2),
    ]
    ws = aggregate_workset(rows)

    assert ws.ok == 14
    assert ws.warn == 2
    assert ws.fire == 1
    assert ws.unparsed == 0
    assert ws.out == 7
    # 14 / (14 + 2 + 1 + 0) = 14/17 ≈ 82.4
    assert ws.coverage_pct == pytest.approx(82.4)
    # Per-file rows preserved in order.
    assert len(ws.files) == 2
    assert ws.files[0].path.name == "a.f90"
    assert ws.files[1].path.name == "b.f90"
