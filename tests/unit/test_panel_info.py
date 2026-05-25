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


def test_panel_scale_marker_reflects_s001(tmp_path: Path):
    """A dimension-clean but scale-mismatched assignment (hPa = Pa) shows
    🟡 in the panel when scale checking is on, and the +/- site too — but
    stays 🟢 with scale off (markers consider dimension only)."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp import server
    from dimfort.lsp.server import _build_expression_tree, _build_ts_ctx

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
        saved = server._scale_mode
        server._scale_mode = scale_on
        try:
            ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
        finally:
            server._scale_mode = saved
        asns = [n for n in _ts.walk(tree.root_node)
                if n.type == "assignment_statement"]
        # phpa = play (direct scale mismatch at the assignment).
        assign_marker = _build_expression_tree(asns[0], ctx, source)["marker"]
        # psum = play + phpa: the `+` child mismatches, and the parent
        # assignment (Pa = Pa, clean on its own) must inherit it.
        plus_payload = _build_expression_tree(asns[1], ctx, source)
        plus_marker = plus_payload["children"][-1]["marker"]
        return assign_marker, plus_marker, plus_payload["marker"]

    # Scale on: direct mismatch, the + node, and the propagated parent.
    assert _markers(scale_on=True) == ("warn", "warn", "warn")
    # Scale off: dimension-only, everything clean.
    assert _markers(scale_on=False) == ("ok", "ok", "ok")


def test_assignment_short_hover_reflects_nested_scale(tmp_path: Path):
    """Hovering the ``=`` of ``psum = play + phpa`` must surface the nested
    scale mismatch (🟡 default) when scale is on — the two-sided verdict
    (Pa vs Pa) is clean on its own. Scale off → 🟢."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as _ts
    from dimfort.core.multifile import check_files
    from dimfort.lsp import server
    from dimfort.lsp.server import (
        _build_ts_ctx,
        _interesting_children,
        _render_assignment_short,
    )

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
        saved = server._scale_mode
        server._scale_mode = scale_on
        try:
            ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
            ctx.var_types.update(ts_checker.collect_var_types(tree, source))
            text, _ = _render_assignment_short(asn, lhs, rhs, ctx, source)
        finally:
            server._scale_mode = saved
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


def test_build_scope_vars_lists_each_declared_name(tmp_path: Path):
    """The scope-vars list must include every declared name in the
    enclosing scope, with the right ``unit`` and ``kind`` per entry."""
    from dimfort.core import ts_parser as _ts
    from dimfort.core.annotations import scan_file
    from dimfort.core.multifile import check_files
    from dimfort.lsp.server import _build_scope_vars, _smallest_enclosing_scope

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
    from dimfort.lsp.server import _find_expression_root

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
    from dimfort.lsp.server import _find_expression_root

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
    from dimfort.lsp.server import (
        _build_expression_tree,
        _build_ts_ctx,
        _find_expression_root,
    )

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
    from dimfort.lsp.server import _build_scope_vars, _smallest_enclosing_scope

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
    from dimfort.lsp.server import _build_scope_vars, _smallest_enclosing_scope

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
    from dimfort.lsp.server import _build_scope_vars, _smallest_enclosing_scope

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
    from dimfort.lsp.server import _enclosing_scopes

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
    from dimfort.lsp.server import (
        _build_scope_vars,
        _scope_header,
        _smallest_enclosing_scope,
    )

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
