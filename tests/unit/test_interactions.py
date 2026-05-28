"""Tests for the on-demand ``interactions`` query + X001 conflict detection.

See ``docs/design/interaction-points.md``. Fixtures are inline Fortran in the
house style; each test runs the real workset pipeline then queries a symbol.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core import unit_config  # noqa: F401 — populate DEFAULT_TABLE
from dimfort.core.interactions import (
    CONTRIBUTES,
    DECLARES,
    REQUIRES,
    USES,
    collect_interactions,
)
from dimfort.core.multifile import check_files


def _report(tmp_path: Path, src: str, symbol: str, **kw):
    f = tmp_path / "m.f90"
    f.write_text(src)
    workset = check_files([f])
    return collect_interactions(workset, symbol, **kw)


def _kinds_at(report, line):
    return {p.kind for p in report.points if p.line == line}


def _unit_at(report, line):
    for p in report.points:
        if p.line == line:
            return p.unit_str
    return None


# ---------------------------------------------------------------------------
# Constraint classification
# ---------------------------------------------------------------------------


def test_additive_sibling_pins_required_unit(tmp_path):
    # invtau_phaserelax shape (#019): `x + y` with y known pins x.
    src = (
        "subroutine s(x, y, z)\n"
        "  real :: x\n"
        "  real :: y  !< @unit{1/s}\n"
        "  real :: z  !< @unit{1/s}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert REQUIRES in _kinds_at(report, 5)
    assert _unit_at(report, 5) == "s⁻¹"


def test_additive_term_with_coefficient_pins_through_product(tmp_path):
    # dzfice shape (#017/#020): `coeff*x` inside a sum whose target is known.
    src = (
        "subroutine s(zdqs, zqsi, dzfice)\n"
        "  real :: zdqs    !< @unit{1}\n"
        "  real :: zqsi    !< @unit{1}\n"
        "  real :: dzfice\n"
        "  zdqs = zqsi + zqsi*dzfice\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "dzfice")
    # zqsi*dzfice must be {1} (sum target), zqsi is {1} ⇒ dzfice required {1}.
    assert REQUIRES in _kinds_at(report, 5)
    assert _unit_at(report, 5) == "1"


def test_literal_anchors_sum_without_lhs_annotation(tmp_path):
    # The `1.0` literal pins the sum to {1} even though `denom` is unannotated.
    src = (
        "subroutine s(dzfice, denom, lcp)\n"
        "  real :: dzfice\n"
        "  real :: denom\n"
        "  real :: lcp   !< @unit{K}\n"
        "  denom = 1.0 - lcp*dzfice\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "dzfice")
    assert _unit_at(report, 5) == "K⁻¹"


def test_literal_anchor_survives_unresolvable_sibling_term(tmp_path):
    # `1.0 + junk - lcp*x`: the literal pins the whole sum to {1} even though
    # the sibling term `junk` is unannotated. Before the additive-flatten fix,
    # _resolve of the entire sibling `(1.0 + junk)` failed on `junk` and `x`
    # fell through to Unconstrained; now `x` is pinned to {1}/{K} = {K⁻¹}.
    src = (
        "subroutine s(x, lcp, junk, denom)\n"
        "  real :: x\n"
        "  real :: lcp    !< @unit{K}\n"
        "  real :: junk\n"
        "  real :: denom\n"
        "  denom = 1.0 + junk - lcp*x\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    reqs = [p for p in report.points if p.kind == REQUIRES]
    assert reqs and reqs[0].unit_str == "K⁻¹"


def test_assignment_lhs_is_contributes(tmp_path):
    src = (
        "subroutine s(x, a, b)\n"
        "  real :: x\n"
        "  real :: a  !< @unit{m}\n"
        "  real :: b  !< @unit{s}\n"
        "  x = a / b\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert CONTRIBUTES in _kinds_at(report, 5)
    assert _unit_at(report, 5) == "m·s⁻¹"


def test_call_argument_requires_param_unit(tmp_path):
    src = (
        "module m\n"
        "contains\n"
        "  subroutine consume(p)\n"
        "    real, intent(in) :: p  !< @unit{m/s}\n"
        "  end subroutine\n"
        "  subroutine s(v)\n"
        "    real :: v\n"
        "    call consume(v)\n"
        "  end subroutine\n"
        "end module\n"
    )
    report = _report(tmp_path, src, "v")
    line = 8  # `call consume(v)`
    assert REQUIRES in _kinds_at(report, line)
    assert _unit_at(report, line) == "m·s⁻¹"


def test_array_element_access_is_an_occurrence(tmp_path):
    # x(i) parses as a call_expression; it must still count as a read of x.
    src = (
        "subroutine s(x, y, z, i)\n"
        "  integer :: i\n"
        "  real :: x(10)\n"
        "  real :: y(10)  !< @unit{1/s}\n"
        "  real :: z(10)  !< @unit{1/s}\n"
        "  z(i) = x(i) + y(i)\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert REQUIRES in _kinds_at(report, 6)
    assert _unit_at(report, 6) == "s⁻¹"


def test_bare_multiplicative_read_is_uses_not_requires(tmp_path):
    # x in `z = x*w` with z unannotated and no anchor ⇒ no equality constraint.
    src = (
        "subroutine s(x, w, z)\n"
        "  real :: x\n"
        "  real :: w\n"
        "  real :: z\n"
        "  z = x * w\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert _kinds_at(report, 5) == {USES}
    assert _unit_at(report, 5) == "?"


# ---------------------------------------------------------------------------
# Conflict detection (X001)
# ---------------------------------------------------------------------------


def test_conflict_detected_across_two_required_sites(tmp_path):
    src = (
        "subroutine s(zdqs, zqsi, dzfice, denom, lcp)\n"
        "  real :: zdqs    !< @unit{1}\n"
        "  real :: zqsi    !< @unit{1}\n"
        "  real :: dzfice\n"
        "  real :: denom\n"
        "  real :: lcp     !< @unit{K}\n"
        "  zdqs = zqsi + zqsi*dzfice\n"
        "  denom = 1.0 - lcp*dzfice\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "dzfice")
    assert len(report.conflicts) == 1
    c = report.conflicts[0]
    assert c.diagnostic.code == "X001"
    assert {c.site.line, c.reference.line} == {7, 8}


def test_repeated_use_on_one_line_reports_one_conflict(tmp_path):
    # dzfice appears twice on the :669-style line; emit a single X001.
    src = (
        "subroutine s(zdqs, a, b, dzfice)\n"
        "  real :: zdqs    !< @unit{1}\n"
        "  real :: a       !< @unit{1}\n"
        "  real :: b       !< @unit{1}\n"
        "  real :: dzfice  !< @unit{1/K}\n"
        "  zdqs = a*dzfice - b*dzfice\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "dzfice")
    line6 = [c for c in report.conflicts if c.site.line == 6]
    assert len(line6) == 1


def test_literal_init_write_adopts_declared_unit_no_conflict(tmp_path):
    # `x = 0.0` is unit-agnostic (R4.4 autocast); `dimfort check` stays silent,
    # so the interactions query must NOT manufacture an X001 against x's
    # declared unit. The write adopts the declared unit, not dimensionless {1}.
    src = (
        "subroutine s(x, y)\n"
        "  real :: x  !< @unit{1/K}\n"
        "  real :: y  !< @unit{1/K}\n"
        "  x = 0.0\n"
        "  y = x\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert report.conflicts == ()
    writes = [p for p in report.points if p.kind == CONTRIBUTES]
    assert writes and writes[0].unit_str == "K⁻¹"


def test_literal_init_to_unannotated_var_makes_no_claim(tmp_path):
    # `x = 0.0` with x unannotated claims nothing, so a read that requires a
    # specific unit must not conflict with the literal write.
    src = (
        "subroutine s(x, y)\n"
        "  real :: x\n"
        "  real :: y  !< @unit{m/s}\n"
        "  x = 0.0\n"
        "  y = x\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert report.conflicts == ()


def test_no_conflict_when_constraints_agree(tmp_path):
    src = (
        "subroutine s(x, y, z)\n"
        "  real :: x\n"
        "  real :: y  !< @unit{1/s}\n"
        "  real :: z  !< @unit{1/s}\n"
        "  z = x + y\n"
        "  y = z + x\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert report.conflicts == ()


def test_declared_unit_conflicts_with_use_site(tmp_path):
    # When the symbol IS annotated but a use-site demands a different dim.
    src = (
        "subroutine s(x, y, z)\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y  !< @unit{1/s}\n"
        "  real :: z  !< @unit{1/s}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert any(c.diagnostic.code == "X001" for c in report.conflicts)


def test_unknown_unit_never_conflicts(tmp_path):
    # A use-site whose unit can't be resolved must not produce a false X001.
    src = (
        "subroutine s(x, y, z)\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y\n"          # unannotated → sibling unit unknown
        "  real :: z\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    assert report.conflicts == ()


# ---------------------------------------------------------------------------
# Scope handling (finding #018: same name, different routines = different vars)
# ---------------------------------------------------------------------------


def test_conflict_does_not_cross_scope_boundary(tmp_path):
    # `t` is {m/s} in one routine and demanded {K} in another — but they are
    # different variables, so no X001.
    src = (
        "module m\n"
        "contains\n"
        "  subroutine a(t, o)\n"
        "    real :: t  !< @unit{m/s}\n"
        "    real :: o  !< @unit{m/s}\n"
        "    o = t + o\n"
        "  end subroutine\n"
        "  subroutine b(t, k)\n"
        "    real :: t\n"
        "    real :: k  !< @unit{K}\n"
        "    k = t + k\n"
        "  end subroutine\n"
        "end module\n"
    )
    report = _report(tmp_path, src, "t")
    assert report.conflicts == ()


def test_scope_filter_restricts_points(tmp_path):
    src = (
        "module m\n"
        "contains\n"
        "  subroutine a(t, o)\n"
        "    real :: t\n"
        "    real :: o  !< @unit{m/s}\n"
        "    o = t + o\n"
        "  end subroutine\n"
        "  subroutine b(t, k)\n"
        "    real :: t\n"
        "    real :: k  !< @unit{K}\n"
        "    k = t + k\n"
        "  end subroutine\n"
        "end module\n"
    )
    report = _report(tmp_path, src, "t", scope="b")
    assert {p.scope for p in report.points} == {"b"}
    assert all(p.scope == "b" for p in report.points)


def test_declares_point_present_for_annotated_symbol(tmp_path):
    src = (
        "subroutine s(x, y)\n"
        "  real :: x  !< @unit{m/s}\n"
        "  real :: y  !< @unit{m/s}\n"
        "  y = x\n"
        "end subroutine\n"
    )
    report = _report(tmp_path, src, "x")
    decls = [p for p in report.points if p.kind == DECLARES]
    assert len(decls) == 1
    assert decls[0].unit_str == "m·s⁻¹"
