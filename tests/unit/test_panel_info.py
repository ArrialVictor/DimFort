"""Tests for the dimfort/panelInfo structured-data builders.

Covers ``_build_expression_tree``, ``_build_routine_vars``, and
``_find_expression_root``. The full LSP request handler is tested
implicitly via the Nvim client during the two-session usage trial
(see ``docs/design/panel-info.md``).
"""
from __future__ import annotations

from pathlib import Path

import pytest

# pygls is an optional dependency (the LSP server is under
# ``dimfort[lsp]``); skip the panel-info tests gracefully when it's
# not installed.
pygls = pytest.importorskip("pygls")  # noqa: F841


def test_marker_token():
    from dimfort.lsp.server import _marker_token
    assert _marker_token("🟢") == "ok"
    assert _marker_token("🟡") == "warn"
    assert _marker_token("🔴") == "error"
    assert _marker_token("?") == "warn"  # unknown → warn


def _materialise(tmp_path: Path) -> Path:
    src = tmp_path / "panel.f90"
    # Doxygen-style ``!<`` is what DimFort's scanner accepts as an
    # annotation comment. Plain ``!`` is a regular comment and would
    # not produce a unit annotation.
    src.write_text(
        "subroutine s\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y  !< @unit{s}\n"
        "  real :: z\n"
        "  z = x * y\n"
        "end subroutine\n"
    )
    return src


def test_find_expression_root_picks_smallest_enclosing(tmp_path: Path):
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.server import _find_expression_root

    src = _materialise(tmp_path)
    tree = _ts.parse_text(src.read_bytes())
    # ``x`` is at line 5, col 8 (1-based) — should resolve to the
    # identifier node, not the enclosing math expression.
    node = _find_expression_root(tree, 5, 8)
    assert node is not None
    assert node.type == "identifier"


def test_find_expression_root_returns_none_off_expression(tmp_path: Path):
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.server import _find_expression_root

    src = _materialise(tmp_path)
    tree = _ts.parse_text(src.read_bytes())
    # Line 1, col 1 is ``s`` in ``subroutine s`` — not an expression.
    node = _find_expression_root(tree, 1, 1)
    assert node is None


def test_build_expression_tree_shape(tmp_path: Path):
    """Build a structured tree for ``z = x * y`` and confirm the
    shape: an assignment with two operand children and a math RHS.
    """
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.server import _build_expression_tree, _build_ts_ctx

    src = _materialise(tmp_path)
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)

    # Find the assignment_statement on line 5.
    asn = None
    for n in _ts.walk(tree.root_node):
        if n.type == "assignment_statement":
            asn = n
            break
    assert asn is not None

    payload = _build_expression_tree(asn, ctx, source)
    assert payload is not None
    assert "label" in payload
    assert "unit" in payload
    assert "marker" in payload
    assert "ruleId" in payload
    assert "children" in payload
    # The assignment must have at least two children (lhs + rhs).
    assert len(payload["children"]) >= 2


def test_assignment_with_matching_units_marks_ok(tmp_path: Path):
    """Regression: an assignment whose RHS is a function call returning
    the LHS's unit should show 🟢, not 🟡. Previous panel logic used
    ``_node_trace_mark`` on the assignment node which produced 🟡; the
    fix routes through ``_assignment_homogeneity`` instead."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.server import _build_expression_tree, _build_ts_ctx

    src = tmp_path / "match.f90"
    src.write_text(
        "function f(t) result(d)\n"
        "  real, intent(in) :: t  !< @unit{s}\n"
        "  real             :: d  !< @unit{m}\n"
        "  d = 0.0\n"
        "end function f\n"
        "subroutine s\n"
        "  real :: d  !< @unit{m}\n"
        "  real :: t  !< @unit{s}\n"
        "  d = f(t)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)

    # Pick the d = f(t) assignment in the subroutine (the 2nd assignment).
    asns = [n for n in _ts.walk(tree.root_node) if n.type == "assignment_statement"]
    target_asn = asns[-1]  # last one is d = f(t)
    payload = _build_expression_tree(target_asn, ctx, source)
    assert payload is not None
    assert payload["marker"] == "ok"


def test_panel_marker_matches_assignment_homogeneity(tmp_path: Path):
    """All three render sites must derive their marker from the verdict.
    Touches the regression spotted during the walkthrough: panel was
    showing 🟡 while hover showed 🟢."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.server import (
        _VERDICT_TO_MARKER,
        _build_expression_tree,
        _build_ts_ctx,
        _marker_token,
    )

    src = tmp_path / "vmatch.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: a  !< @unit{m}\n"
        "  real :: b  !< @unit{m}\n"
        "  a = b\n"
        "end subroutine\n"
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)

    asn = next(
        n for n in _ts.walk(tree.root_node) if n.type == "assignment_statement"
    )
    lhs, rhs = ts_checker._assignment_sides(asn)
    verdict, _, _ = ts_checker._assignment_homogeneity(lhs, rhs, ctx, source)
    panel_payload = _build_expression_tree(asn, ctx, source)
    expected_marker = _marker_token(_VERDICT_TO_MARKER[verdict])
    assert panel_payload["marker"] == expected_marker


def test_build_routine_vars_lists_each_declared_name(tmp_path: Path):
    """The routine-vars list must include every declared name in the
    enclosing routine, with the right ``unit`` and ``kind`` per entry."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.server import _build_routine_vars, _smallest_enclosing_routine

    src = _materialise(tmp_path)
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    attached = result.attachments[resolved]
    scan_decls = scan_file(src).declarations

    # Pick a position inside the subroutine body.
    routine = _smallest_enclosing_routine(tree, 5, 5)
    assert routine is not None

    vars_list = _build_routine_vars(routine, scan_decls, attached, source)
    by_name = {row["name"]: row for row in vars_list}
    assert set(by_name) == {"x", "y", "z"}
    assert by_name["x"]["unit"] == "m"
    assert by_name["x"]["kind"] == "annotated"
    assert by_name["y"]["unit"] == "s"
    assert by_name["z"]["unit"] is None
    assert by_name["z"]["kind"] == "unannotated"
    # Lines must be 1-based source line numbers.
    assert by_name["x"]["line"] == 2
