"""Tests for the dimfort/panelInfo structured-data builders.

Covers ``_build_expression_tree``, ``_build_scope_vars``, and
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
    from dimfort.lsp.markers import _marker_token
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
    from dimfort.lsp.tree_nav import _find_expression_root

    src = _materialise(tmp_path)
    tree = _ts.parse_text(src.read_bytes())
    # ``x`` is at line 5, col 8 (1-based) — should resolve to the
    # identifier node, not the enclosing math expression.
    node = _find_expression_root(tree, 5, 8)
    assert node is not None
    assert node.type == "identifier"


def test_find_expression_root_returns_none_off_expression(tmp_path: Path):
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _find_expression_root

    src = _materialise(tmp_path)
    tree = _ts.parse_text(src.read_bytes())
    # Line 1, col 1 is ``s`` in ``subroutine s`` — not an expression.
    node = _find_expression_root(tree, 1, 1)
    assert node is None


def test_find_expression_root_skips_unparsed_region(tmp_path: Path):
    """In a region tree-sitter couldn't parse, the recovered node is malformed
    (bleeds in adjacent lines), so the panel must show no expression tree there
    rather than a confident-but-wrong one. The clean LHS identifier still
    resolves."""
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _find_expression_root

    src = (
        b"subroutine s\n"               # 1
        b"  real :: a  !< @unit{m}\n"   # 2
        b"  a = * / +\n"                # 3 unparseable RHS
        b"  a = 1.0\n"                  # 4 (keeps the routine parseable)
        b"end subroutine\n"
    )
    tree = _ts.parse_text(src)
    # Cursor on the malformed RHS (the ``*``) → suppressed.
    assert _find_expression_root(tree, 3, 7) is None
    # Cursor on the LHS ``a`` → still resolves (clean identifier).
    lhs = _find_expression_root(tree, 3, 3)
    assert lhs is not None and lhs.type == "identifier"


def test_build_expression_tree_shape(tmp_path: Path):
    """Build a structured tree for ``z = x * y`` and confirm the
    shape: an assignment with two operand children and a math RHS.
    """
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

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
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

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


def test_panel_scale_marker_reflects_s001(tmp_path: Path):
    """A dimension-clean but scale-mismatched assignment (hPa = Pa) shows
    🟡 in the panel when scale checking is on, and the +/- site too — but
    stays 🟢 with scale off (markers consider dimension only)."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp import server
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

    src = tmp_path / "scale_panel.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: play  !< @unit{Pa}\n"
        "  real :: phpa  !< @unit{hPa}\n"
        "  real :: psum  !< @unit{Pa}\n"
        "  phpa = play\n"
        "  psum = play + phpa\n"
        "end subroutine\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()

    def _markers(scale_on: bool):
        result = check_files([src], scale_mode=scale_on)
        saved = server.state.scale_mode
        server.state.scale_mode = scale_on
        # Markers read diagnostics from state.last_result (keyed by ctx.file).
        with server.state.last_result_lock:
            saved_result = server.state.last_result
            server.state.last_result = result
        try:
            ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
            asns = [n for n in _ts.walk(tree.root_node)
                    if n.type == "assignment_statement"]
            # phpa = play (direct scale mismatch at the assignment).
            assign_marker = _build_expression_tree(asns[0], ctx, source)["marker"]
            # psum = play + phpa: the `+` child mismatches, and the parent
            # assignment (Pa = Pa, clean on its own) must inherit it.
            plus_payload = _build_expression_tree(asns[1], ctx, source)
            plus_marker = plus_payload["children"][-1]["marker"]
        finally:
            server.state.scale_mode = saved
            with server.state.last_result_lock:
                server.state.last_result = saved_result
        return assign_marker, plus_marker, plus_payload["marker"]

    # Scale on: direct mismatch, the + node, and the propagated parent.
    assert _markers(scale_on=True) == ("warn", "warn", "warn")
    # Scale off: dimension-only, everything clean.
    assert _markers(scale_on=False) == ("ok", "ok", "ok")


def test_panel_marker_matrix_diagnostic_driven(tmp_path: Path):
    """Behaviour-preserving matrix for the diagnostic-driven markers:
    dimension mismatch → 🔴 (via H001), offset mismatch → 🟡 (via S002),
    clean → 🟢. Pins that dimension markers still work (now sourced from
    the diagnostic, not re-derived) and that S002 surfaces."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp import server
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

    src = tmp_path / "matrix.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: x   !< @unit{m}\n"
        "  real :: t   !< @unit{s}\n"
        "  real :: tk  !< @unit{K}\n"
        "  real :: tc  !< @unit{degC}\n"
        "  x = t\n"          # dimension mismatch  → H001 → error 🔴
        "  tk = tc\n"        # offset mismatch     → S002 → warn  🟡
        "  x = x\n"          # clean               → 🟢
        "end subroutine\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    asns = [n for n in _ts.walk(tree.root_node)
            if n.type == "assignment_statement"]

    result = check_files([src], scale_mode=True)
    with server.state.last_result_lock:
        saved_result, server.state.last_result = server.state.last_result, result
    saved_mode, server.state.scale_mode = server.state.scale_mode, True
    try:
        ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
        marks = [_build_expression_tree(a, ctx, source)["marker"] for a in asns]
    finally:
        server.state.scale_mode = saved_mode
        with server.state.last_result_lock:
            server.state.last_result = saved_result

    assert marks == ["error", "warn", "ok"]


def test_panel_marker_s003_is_error(tmp_path: Path):
    """An invalid ``@unit_affine_conversion`` directive (S003) colours the
    assignment 🔴; a valid one emits no diagnostic and stays 🟢."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp import server
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

    src = tmp_path / "s003_panel.f90"
    src.write_text(
        "module m\n"
        " real, parameter :: RTT = 273.15  !< @unit{K}\n"
        " contains\n"
        "  subroutine s()\n"
        "   real :: t_k  !< @unit{K}\n"
        "   real :: t_c  !< @unit{degC}\n"
        "   t_k = t_c - RTT  !< @unit_affine_conversion{degC -> K}\n"  # wrong dir → S003 🔴
        "   t_k = t_c + RTT  !< @unit_affine_conversion{degC -> K}\n"  # valid → 🟢
        "  end subroutine\n end module\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    asns = [n for n in _ts.walk(tree.root_node)
            if n.type == "assignment_statement"]

    result = check_files([src], scale_mode=True)
    with server.state.last_result_lock:
        saved_result, server.state.last_result = server.state.last_result, result
    saved_mode, server.state.scale_mode = server.state.scale_mode, True
    try:
        ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
        marks = [_build_expression_tree(a, ctx, source)["marker"] for a in asns]
    finally:
        server.state.scale_mode = saved_mode
        with server.state.last_result_lock:
            server.state.last_result = saved_result

    assert marks == ["error", "ok"]


def test_panel_info_diagnostics_for_cursor_line(tmp_path: Path):
    """_panel_info exposes the diagnostics on the cursor line so the panel
    can show *why* a node is marked. Empty array on a clean line."""
    from types import SimpleNamespace

    from dimfort.core.multifile import check_files
    from dimfort.lsp import server

    src = tmp_path / "diag_panel.f90"
    src.write_text(
        "module m\n"
        "  real :: t_k  !< @unit{K}\n"
        "  real :: t_c  !< @unit{degC}\n"
        "contains\n"
        "  subroutine s()\n"
        "    t_k = t_c\n"   # line 6: S002 (K vs degC)
        "    t_k = t_k\n"   # line 7: clean
        "  end subroutine s\n"
        "end module m\n"
    )
    resolved = src.resolve()
    text = src.read_text()
    result = check_files([src], scale_mode=True)

    class _Doc:
        source = text

    ls = SimpleNamespace(
        workspace=SimpleNamespace(get_text_document=lambda _uri: _Doc())
    )

    def _diags_at(line_1based: int):
        with server.state.last_result_lock:
            saved_result, server.state.last_result = server.state.last_result, result
        saved_mode, server.state.scale_mode = server.state.scale_mode, True
        try:
            return server._panel_info(ls, {
                "textDocument": {"uri": resolved.as_uri()},
                "position": {"line": line_1based - 1, "character": 6},
            })["diagnostics"]
        finally:
            server.state.scale_mode = saved_mode
            with server.state.last_result_lock:
                server.state.last_result = saved_result

    line6 = _diags_at(6)
    assert [d["code"] for d in line6] == ["S002"]
    assert line6[0]["severity"] == "warning"
    assert "Offset mismatch" in line6[0]["message"]
    assert _diags_at(7) == []   # clean line


def test_assignment_short_hover_reflects_nested_scale(tmp_path: Path):
    """Hovering the ``=`` of ``psum = play + phpa`` must surface the nested
    scale mismatch (🟡 default) when scale is on — the two-sided verdict
    (Pa vs Pa) is clean on its own. Scale off → 🟢."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp import server
    from dimfort.lsp.hover import _render_assignment_short
    from dimfort.lsp.tree_access import _build_ts_ctx
    from dimfort.lsp.tree_nav import _interesting_children

    src = tmp_path / "scale_short.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: play  !< @unit{Pa}\n"
        "  real :: phpa  !< @unit{hPa}\n"
        "  real :: psum  !< @unit{Pa}\n"
        "  psum = play + phpa\n"
        "end subroutine\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    asn = next(n for n in _ts.walk(tree.root_node)
               if n.type == "assignment_statement")
    kids = _interesting_children(asn)
    lhs, rhs = kids[0], kids[-1]

    def _marker(scale_on: bool) -> str:
        result = check_files([src], scale_mode=scale_on)
        saved = server.state.scale_mode
        server.state.scale_mode = scale_on
        # Markers read diagnostics from state.last_result (keyed by ctx.file).
        with server.state.last_result_lock:
            saved_result = server.state.last_result
            server.state.last_result = result
        try:
            ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
            ctx.var_types.update(ts_checker.collect_var_types(tree, source))
            text, _ = _render_assignment_short(asn, lhs, rhs, ctx, source)
        finally:
            server.state.scale_mode = saved
            with server.state.last_result_lock:
                server.state.last_result = saved_result
        return text.split(" DimFort")[0].replace("**", "").strip()

    assert _marker(scale_on=True) == "🟡"
    assert _marker(scale_on=False) == "🟢"


def test_panel_marker_matches_assignment_homogeneity(tmp_path: Path):
    """All three render sites must derive their marker from the verdict.
    Touches the regression spotted during the walkthrough: panel was
    showing 🟡 while hover showed 🟢."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.markers import _marker_token
    from dimfort.lsp.server import _VERDICT_TO_MARKER
    from dimfort.lsp.tree_access import _build_ts_ctx

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
    verdict, _, _ = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    panel_payload = _build_expression_tree(asn, ctx, source)
    expected_marker = _marker_token(_VERDICT_TO_MARKER[verdict])
    assert panel_payload["marker"] == expected_marker


def test_build_scope_vars_lists_each_declared_name(tmp_path: Path):
    """The scope-vars list must include every declared name in the
    enclosing scope, with the right ``unit`` and ``kind`` per entry."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_scope_vars
    from dimfort.lsp.tree_nav import _smallest_enclosing_scope

    src = _materialise(tmp_path)
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    attached = result.attachments[resolved]
    scan_decls = scan_file(src).declarations

    # Pick a position inside the subroutine body.
    scope = _smallest_enclosing_scope(tree, 5, 5)
    assert scope is not None
    assert scope.type == "subroutine"

    vars_list = _build_scope_vars(scope, scan_decls, attached, source)
    by_name = {row["name"]: row for row in vars_list}
    assert set(by_name) == {"x", "y", "z"}
    assert by_name["x"]["unit"] == "m"
    assert by_name["x"]["kind"] == "annotated"
    assert by_name["y"]["unit"] == "s"
    assert by_name["z"]["unit"] is None
    assert by_name["z"]["kind"] == "unannotated"
    # Lines must be 1-based source line numbers.
    assert by_name["x"]["line"] == 2


def test_find_expression_root_promotes_callee_to_call(tmp_path: Path):
    """A cursor on a call's callee name resolves to the whole
    call_expression (which carries the return unit), not the bare
    callee identifier (which has no unit and renders as a lone leaf).
    An argument identifier under the same call is left as-is."""
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _find_expression_root

    line = "  a = f(a)"
    src = (
        "subroutine s\n"
        "  real :: a  !< @unit{m}\n"
        f"{line}\n"                    # line 3: a = f(a)
        "end subroutine\n"
    )
    tree = _ts.parse_text(src.encode())

    # 1-based column of the callee 'f' and of the argument 'a'.
    callee_col = line.index("f(") + 1
    arg_col = line.index("(a)") + 2  # the 'a' inside the parens

    callee_node = _find_expression_root(tree, 3, callee_col)
    assert callee_node is not None
    assert callee_node.type == "call_expression"

    # Cursor on the argument identifier stays an identifier (not bumped).
    arg_node = _find_expression_root(tree, 3, arg_col)
    assert arg_node is not None
    assert arg_node.type == "identifier"


def test_find_expression_root_promotes_subroutine_callee_to_call(tmp_path: Path):
    """Same as the function case, for a ``call sub(...)`` statement: the
    callee promotes to the subroutine_call (which expands the argument
    tree). The leading ``call`` keyword means the callee does not start
    where the statement does, so the byte-offset check must handle it."""
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _find_expression_root

    line = "  call s2(a)"
    src = (
        "subroutine s\n"
        "  real :: a  !< @unit{m}\n"
        f"{line}\n"                    # line 3: call s2(a)
        "end subroutine\n"
    )
    tree = _ts.parse_text(src.encode())

    callee = _find_expression_root(tree, 3, line.index("s2(") + 1)
    assert callee is not None
    assert callee.type == "subroutine_call"

    # The argument identifier is left alone.
    arg = _find_expression_root(tree, 3, line.index("(a)") + 2)
    assert arg is not None
    assert arg.type == "identifier"


def test_build_expression_tree_call_includes_argument(tmp_path: Path):
    """A function call renders as a tree: the call as root (carrying the
    return unit) with its argument(s) as children, not a childless leaf."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_expression_tree
    from dimfort.lsp.tree_access import _build_ts_ctx
    from dimfort.lsp.tree_nav import _find_expression_root

    line = "  b = f(a)"
    src = tmp_path / "call.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: a   !< @unit{m}\n"
        "  real :: b   !< @unit{m}\n"
        f"{line}\n"                          # line 4: b = f(a)
        "end subroutine\n"
        "function f(x) result(y)\n"
        "  real, intent(in) :: x   !< @unit{m}\n"
        "  real             :: y   !< @unit{m}\n"
        "  y = x\n"
        "end function f\n"
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)

    call = _find_expression_root(tree, 4, line.index("f(") + 1)
    assert call is not None and call.type == "call_expression"

    payload = _build_expression_tree(call, ctx, source)
    assert payload is not None
    # The argument 'a' appears as a child (it was being stripped before).
    child_labels = [c["label"].strip() for c in payload["children"]]
    assert "a" in child_labels


def test_build_scope_vars_marks_unparseable_as_error(tmp_path: Path):
    """A declaration whose ``@unit{}`` fails to parse is kind ``error``
    (🔴) — distinct from ``unannotated`` (🟡) and ``annotated`` (🟢).
    The verdict is gated on the workset's unparseable set, not merely
    on the presence of annotation text."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_scope_vars
    from dimfort.lsp.tree_nav import _smallest_enclosing_scope

    src = tmp_path / "e.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: a  !< @unit{m}\n"    # valid → annotated
        "  real :: b  !< @unit{??}\n"   # unparseable → error
        "  real :: c\n"                 # none → unannotated
        "end subroutine\n"
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    attached = result.attachments[resolved]
    scan_decls = scan_file(src).declarations

    unparseable = result.unparseable_units.get(resolved, frozenset())
    assert "b" in unparseable

    scope = _smallest_enclosing_scope(tree, 3, 5)
    assert scope is not None and scope.type == "subroutine"

    by_name = {
        row["name"]: row
        for row in _build_scope_vars(scope, scan_decls, attached, source, unparseable)
    }
    assert by_name["a"]["kind"] == "annotated"
    assert by_name["b"]["kind"] == "error"
    assert by_name["b"]["unit"] == "??"  # raw text preserved for display
    assert by_name["c"]["kind"] == "unannotated"

    # Without the set, 'b' falls back to 'annotated' (it has text) —
    # confirms 🔴 is gated on the parse verdict, not text presence.
    no_set = {
        row["name"]: row["kind"]
        for row in _build_scope_vars(scope, scan_decls, attached, source)
    }
    assert no_set["b"] == "annotated"


def test_build_scope_vars_module_level(tmp_path: Path):
    """Module-level declarations (scope = None) are listed when the
    cursor is at module scope. Nested routine decls are excluded."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_scope_vars
    from dimfort.lsp.tree_nav import _smallest_enclosing_scope

    src = tmp_path / "mod.f90"
    src.write_text(
        "module m\n"
        "  real :: alpha  !< @unit{m}\n"
        "  real :: beta   !< @unit{s}\n"
        "contains\n"
        "  subroutine s\n"
        "    real :: local_x  !< @unit{kg}\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    attached = result.attachments[resolved]
    scan_decls = scan_file(src).declarations

    # Cursor on line 2 (alpha decl) → module scope, not the subroutine.
    scope = _smallest_enclosing_scope(tree, 2, 5)
    assert scope is not None
    assert scope.type == "module"

    vars_list = _build_scope_vars(scope, scan_decls, attached, source)
    by_name = {row["name"]: row for row in vars_list}
    # Module-level decls present; the subroutine's local_x excluded.
    assert set(by_name) == {"alpha", "beta"}
    assert "local_x" not in by_name


def test_build_scope_vars_drops_half_typed_declaration(tmp_path: Path):
    """A half-typed ``real ::`` (no name yet) triggers tree-sitter error
    recovery that scavenges an identifier from the next statement. The
    scope table must not show that bogus row."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_text
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_scope_vars
    from dimfort.lsp.tree_nav import _smallest_enclosing_scope

    # Build a valid file first so the workset has attachments, then
    # scan a half-typed variant directly.
    src = tmp_path / "typing.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: t  !< @unit{s}\n"
        "  real :: d  !< @unit{m}\n"
        "  d = t\n"
        "end subroutine\n"
    )
    result = check_files([src])
    attached = result.attachments[src.resolve()]

    half_typed = (
        "subroutine s\n"
        "  real :: t  !< @unit{s}\n"
        "  real :: d  !< @unit{m}\n"
        "  real ::\n"        # mid-typing, no name yet
        "  d = t\n"
        "end subroutine\n"
    )
    source = half_typed.encode("utf-8")
    tree = _ts.parse_text(source)
    scan_decls = scan_text(half_typed).declarations
    scope = _smallest_enclosing_scope(tree, 2, 5)

    vars_list = _build_scope_vars(scope, scan_decls, attached, source)
    rows = [(v["line"], v["name"]) for v in vars_list]
    # The legit decls survive; the bogus 'd' scavenged onto line 4 does
    # NOT appear (d's only legit row is line 3).
    assert (2, "t") in rows
    assert (3, "d") in rows
    assert (4, "d") not in rows
    assert (4, "t") not in rows


def test_enclosing_scopes_stacks_module_then_subroutine(tmp_path: Path):
    """A cursor inside a module-contained subroutine yields both scopes,
    outermost (module) first."""
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _enclosing_scopes

    src = tmp_path / "nested.f90"
    src.write_text(
        "module m\n"
        "  real :: alpha  !< @unit{m}\n"
        "contains\n"
        "  subroutine s\n"
        "    real :: local_x  !< @unit{kg}\n"
        "    local_x = 0.0\n"
        "  end subroutine\n"
        "end module\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    # Cursor on line 6 (inside subroutine s, inside module m).
    scopes = _enclosing_scopes(tree, 6, 5)
    kinds = [s.type for s in scopes]
    assert kinds == ["module", "subroutine"]  # outer → inner


def test_build_scope_vars_program_level(tmp_path: Path):
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import _build_scope_vars
    from dimfort.lsp.tree_nav import _scope_header, _smallest_enclosing_scope

    src = tmp_path / "prog.f90"
    src.write_text(
        "program main\n"
        "  real :: t  !< @unit{s}\n"
        "  t = 1.0\n"
        "end program\n"
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    attached = result.attachments[resolved]
    scan_decls = scan_file(src).declarations

    scope = _smallest_enclosing_scope(tree, 2, 5)
    assert scope is not None
    assert scope.type == "program"
    header = _scope_header(scope, source)
    assert header == {"name": "main", "kind": "program"}

    vars_list = _build_scope_vars(scope, scan_decls, attached, source)
    by_name = {row["name"]: row for row in vars_list}
    assert set(by_name) == {"t"}
    assert by_name["t"]["unit"] == "s"


def test_recover_scopes_when_routine_error_wrapped(tmp_path: Path):
    """An unparseable statement collapses the whole routine into an
    ``ERROR`` node, so ``_enclosing_scopes`` finds nothing and the panel's
    Scope section would blank. ``recover_scopes`` reconstructs the scope
    from the surviving header so the declarations still list."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.expr_tree import build_scope_vars_by_span, recover_scopes
    from dimfort.lsp.tree_nav import _enclosing_scopes

    src = tmp_path / "broken.f90"
    src.write_text(
        "subroutine driver\n"          # 1
        "  real :: t  !< @unit{s}\n"    # 2
        "  real :: d  !< @unit{m}\n"    # 3
        "  t = 2.0\n"                   # 4
        "  d = t * t\n"                 # 5
        "  10 format (1x, 'broken'\n"   # 6 — unparseable last statement
        "end subroutine\n"             # 7
    )
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    attached = result.attachments[src.resolve()]
    scan_decls = scan_file(src).declarations

    # Precondition: the bug — tree-sitter wraps the routine in ERROR, so
    # there is no scope node at the cursor (line 5).
    assert _enclosing_scopes(tree, 5, 5) == []

    recovered = recover_scopes(tree, source)
    assert recovered == [("subroutine", "driver", 1, 7)]

    vars_list = build_scope_vars_by_span(0, recovered, scan_decls, attached, source)
    by_name = {row["name"]: row for row in vars_list}
    assert set(by_name) == {"t", "d"}
    assert by_name["t"]["unit"] == "s"
    assert by_name["t"]["kind"] == "annotated"
    assert by_name["d"]["unit"] == "m"


def test_recover_scopes_nested_module_excludes_routine_locals(tmp_path: Path):
    """When recovery kicks in for a module-contained routine, the module
    section lists only module-level declarations and the routine section
    only its locals — a declaration belongs to its innermost scope."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.lsp.expr_tree import build_scope_vars_by_span, recover_scopes

    src = tmp_path / "mod.f90"
    src.write_text(
        "module physics\n"                 # 1
        "  real :: g  !< @unit{m/s^2}\n"    # 2
        "contains\n"                        # 3
        "  subroutine fall(h)\n"            # 4
        "    real :: h   !< @unit{m}\n"     # 5
        "    real :: tt  !< @unit{s}\n"     # 6
        "    tt = 1.0\n"                     # 7
        "    20 format (1x, 'broken'\n"     # 8 — unparseable
        "  end subroutine\n"                # 9
        "end module\n"                       # 10
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    scan_decls = scan_file(src).declarations

    recovered = recover_scopes(tree, source)
    by_kind = {kind: (name, s, e) for (kind, name, s, e) in recovered}
    assert by_kind["module"][0] == "physics"
    assert by_kind["subroutine"][0] == "fall"

    mod_idx = next(i for i, r in enumerate(recovered) if r[0] == "module")
    sub_idx = next(i for i, r in enumerate(recovered) if r[0] == "subroutine")
    mod_vars = {
        row["name"]
        for row in build_scope_vars_by_span(mod_idx, recovered, scan_decls, None, source)
    }
    sub_vars = {
        row["name"]
        for row in build_scope_vars_by_span(sub_idx, recovered, scan_decls, None, source)
    }
    assert mod_vars == {"g"}            # routine locals excluded
    assert sub_vars == {"h", "tt"}


def _imports_scene(tmp_path: Path) -> Path:
    """Two modules in one file: `solver` `use`s `phys_constants` with an
    only-list; `viewer` whole-module-imports it."""
    src = tmp_path / "imp.f90"
    src.write_text(
        "module phys_constants\n"               # 1
        "  real :: play   !< @unit{Pa}\n"        # 2
        "  real :: grav   !< @unit{m/s^2}\n"     # 3
        "end module phys_constants\n"            # 4
        "\n"                                      # 5
        "module solver\n"                        # 6
        "  use phys_constants, only: play\n"     # 7
        "  real :: play_local  !< @unit{Pa}\n"   # 8
        "contains\n"                              # 9
        "  subroutine step()\n"                  # 10
        "    play_local = play\n"                # 11
        "  end subroutine step\n"                # 12
        "end module solver\n"                    # 13
        "\n"                                      # 14
        "module viewer\n"                        # 15
        "  use phys_constants\n"                 # 16
        "contains\n"                              # 17
        "  subroutine show()\n"                  # 18
        "    grav = 9.8\n"                        # 19
        "  end subroutine show\n"                # 20
        "end module viewer\n"                    # 21
    )
    return src


def test_imports_only_list(tmp_path: Path):
    """An `only:` import lists exactly the named symbols, with their unit
    and a cross-file nav location at the source declaration."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.imports import build_imports

    src = _imports_scene(tmp_path)
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)

    # Cursor in solver.step() (line 11). play_local is a local decl (shadow set).
    rows = build_imports(tree, source, 11, result, frozenset({"play_local"}))
    by_name = {r["name"]: r for r in rows}
    assert set(by_name) == {"play"}              # only-list = just play
    assert by_name["play"]["unit"] == "kg/(m×s²)"
    assert by_name["play"]["module"] == "phys_constants"
    assert by_name["play"]["kind"] == "annotated"
    assert by_name["play"]["line"] == 2          # source declaration line


def test_imports_whole_module(tmp_path: Path):
    """A whole-module `use` lists every exported variable."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.imports import build_imports

    src = _imports_scene(tmp_path)
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)

    # Cursor in viewer.show() (line 19) — `use phys_constants` (no only-list).
    rows = build_imports(tree, source, 19, result, frozenset())
    assert {r["name"] for r in rows} == {"play", "grav"}


def test_imports_excludes_sibling_routine_and_shadow(tmp_path: Path):
    """A name declared locally in the cursor's scope shadows the import
    (excluded); a sibling module's import does not leak."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp.imports import build_imports

    src = _imports_scene(tmp_path)
    result = check_files([src])
    source = src.read_bytes()
    tree = _ts.parse_text(source)

    # Shadow: if `play` were locally declared in scope, it must not appear.
    rows = build_imports(tree, source, 11, result, frozenset({"play", "play_local"}))
    assert {r["name"] for r in rows} == set()

    # Sibling scope: in solver.step(), viewer's whole-module import of grav
    # must not leak (solver only-imports play).
    rows = build_imports(tree, source, 11, result, frozenset({"play_local"}))
    assert "grav" not in {r["name"] for r in rows}
