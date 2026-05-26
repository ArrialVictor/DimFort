"""Tests for the shared ``_assignment_homogeneity`` helper (R4.4).

This is the single source of truth for assignment-level marker and
autocast decisions, used by both the checker (for diagnostic emission)
and the LSP renderers (for hover / panel markers). The tests cover
every verdict branch plus the AutocastEvent emission contract.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core.diagnostics import AutocastEvent
from dimfort.core.multifile import check_files


def _find(tree, kind):
    for n in _ts.walk(tree.root_node):
        if n.type == kind:
            return n
    return None


def _materialise(tmp_path: Path, body: str) -> Path:
    src = tmp_path / "asn.f90"
    src.write_text("subroutine s\n" + body + "end subroutine\n")
    return src


def _ctx_for(src: Path, result):
    from dimfort.lsp.tree_access import _build_ts_ctx
    resolved = src.resolve()
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
    return tree, source, ctx


def test_homogeneous_returns_ok(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: a  !< @unit{m}\n"
        "  real :: b  !< @unit{m}\n"
        "  a = b\n"
    )
    result = check_files([src])
    tree, source, ctx = _ctx_for(src, result)
    asn = _find(tree, "assignment_statement")
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, lu, ru = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    assert verdict == "homogeneous"
    assert lu is not None and ru is not None


def test_mismatch_returns_mismatch(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: a  !< @unit{kg}\n"
        "  real :: b  !< @unit{m}\n"
        "  a = b\n"
    )
    result = check_files([src])
    tree, source, ctx = _ctx_for(src, result)
    asn = _find(tree, "assignment_statement")
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, _lu, _ru = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    assert verdict == "mismatch"


def test_autocast_for_bare_literal(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: t  !< @unit{s}\n"
        "  t = 2.0\n"
    )
    result = check_files([src])
    tree, source, ctx = _ctx_for(src, result)
    asn = _find(tree, "assignment_statement")
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, lu, ru = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    assert verdict == "autocast"
    # effective RHS unit equals LHS unit
    assert lu == ru


def test_autocast_for_unary_minus_literal(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: t  !< @unit{s}\n"
        "  t = -2.0\n"
    )
    result = check_files([src])
    tree, source, ctx = _ctx_for(src, result)
    asn = _find(tree, "assignment_statement")
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, _lu, _ru = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    assert verdict == "autocast"


def test_autocast_for_arithmetic_of_literals(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: t  !< @unit{s}\n"
        "  t = 2.0 * 3.14\n"
    )
    result = check_files([src])
    tree, source, ctx = _ctx_for(src, result)
    asn = _find(tree, "assignment_statement")
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, _lu, _ru = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    assert verdict == "autocast"


def test_compound_with_unitful_operand_is_not_autocast(tmp_path: Path):
    """``t = c + 2.0`` mixes a unitful identifier with a literal — R4.4
    does NOT apply (the literal is inside a compound expression, not the
    sole RHS). The existing D1.5 H010 fires elsewhere."""
    src = _materialise(tmp_path,
        "  real :: c  !< @unit{s}\n"
        "  real :: t  !< @unit{s}\n"
        "  t = c + 2.0\n"
    )
    result = check_files([src])
    tree, source, ctx = _ctx_for(src, result)
    asn = _find(tree, "assignment_statement")
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, _lu, _ru = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    # c + 2.0 resolves to s (R4.1 auto-cast inside the compound) →
    # homogeneous from the assignment's perspective.
    assert verdict == "homogeneous"


def test_autocast_event_emitted_for_bare_literal(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: t  !< @unit{s}\n"
        "  t = 2.0\n"
    )
    result = check_files([src])
    events = result.autocast_events.get(src.resolve(), [])
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, AutocastEvent)
    assert ev.context == "assignment_rhs"
    assert ev.inferred_unit == "s"
    assert ev.literal_text == "2.0"


def test_no_autocast_event_for_mismatch(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: a  !< @unit{kg}\n"
        "  real :: b  !< @unit{m}\n"
        "  a = b\n"
    )
    result = check_files([src])
    events = result.autocast_events.get(src.resolve(), [])
    assert events == []


def test_no_autocast_event_for_compound_literal(tmp_path: Path):
    src = _materialise(tmp_path,
        "  real :: c  !< @unit{s}\n"
        "  real :: t  !< @unit{s}\n"
        "  t = c + 2.0\n"
    )
    result = check_files([src])
    # ``c + 2.0`` is the autocast pattern for R4.1 (compound), not R4.4
    # (initialization). No AutocastEvent should fire here.
    events = result.autocast_events.get(src.resolve(), [])
    assert events == []


def test_multiple_autocast_events_in_one_file(tmp_path: Path):
    """Each bare-literal assignment fires its own event."""
    src = _materialise(tmp_path,
        "  real :: t  !< @unit{s}\n"
        "  real :: d  !< @unit{m}\n"
        "  t = 2.0\n"
        "  d = -5.0\n"
        "  t = 0.5 * 3.14\n"
    )
    result = check_files([src])
    events = result.autocast_events.get(src.resolve(), [])
    assert len(events) == 3
    inferred = sorted(ev.inferred_unit for ev in events)
    assert inferred == ["m", "s", "s"]
