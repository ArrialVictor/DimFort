"""M6 tests for polymorphism polish:

- Hover signature rendering prefixes ``∀ 'a.`` for polymorphic functions.
- Concrete signatures unchanged.
- Self-recursive polymorphic functions check cleanly (verifies that
  M3/M4's per-call-site fresh instantiation already handles recursion
  with no extra fixpoint pass).
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core.multifile import check_files
from dimfort.core.symbols import FuncSig
from dimfort.core.units import parse
from dimfort.lsp.hover_render import _sig_render_md


def _materialise(tmp_path: Path, name: str, body: str) -> Path:
    src = tmp_path / name
    src.write_text(body)
    return src


def _diags(result, file: Path) -> list:
    return list(result.diagnostics.get(file.resolve(), []))


# ---------------------------------------------------------------------------
# Hover signature rendering


def test_concrete_signature_has_no_quantifier():
    sig = FuncSig(
        arg_names=("x", "y"),
        arg_units=(parse("m"), parse("s")),
        return_unit=parse("m/s"),
        is_subroutine=False,
    )
    rendered = _sig_render_md("foo", sig)
    assert "∀" not in rendered
    assert rendered.startswith("foo(")


def test_polymorphic_signature_has_quantifier_prefix():
    sig = FuncSig(
        arg_names=("x", "y"),
        arg_units=(parse("'a"), parse("'a")),
        return_unit=parse("'a"),
        is_subroutine=False,
    )
    rendered = _sig_render_md("avg", sig)
    assert rendered.startswith("∀ 'a.")
    assert "avg(" in rendered


def test_polymorphic_signature_two_tyvars_two_quantifiers():
    sig = FuncSig(
        arg_names=("m", "v", "p"),
        arg_units=(parse("'a"), parse("'b"), parse("'a*'b")),
        return_unit=None,
        is_subroutine=True,
    )
    rendered = _sig_render_md("momentum", sig)
    # Sorted order — 'a before 'b.
    assert rendered.startswith("∀ 'a. ∀ 'b.")


def test_polymorphic_signature_mixed_with_concrete_slots():
    """A signature with both tyvar and concrete slots — only the tyvar
    drives the quantifier prefix."""
    sig = FuncSig(
        arg_names=("x", "c", "y"),
        arg_units=(parse("'a"), parse("kg"), parse("'a")),
        return_unit=None,
        is_subroutine=True,
    )
    rendered = _sig_render_md("scaled_avg", sig)
    assert rendered.startswith("∀ 'a.")


# ---------------------------------------------------------------------------
# Recursion


def test_self_recursive_polymorphic_function_checks_cleanly(tmp_path: Path):
    """A polymorphic function that calls itself with the same-typed arg
    must not fire any diagnostic. M3/M4 already do per-call-site fresh
    instantiation, so self-recursion needs no extra fixpoint pass."""
    src = _materialise(tmp_path, "rec.f90",
        "module mod\n"
        "contains\n"
        "  recursive subroutine f(x, depth, y)\n"
        "    real, intent(in)    :: x      !< @unit{'a}\n"
        "    integer, intent(in) :: depth\n"
        "    real, intent(out)   :: y      !< @unit{'a}\n"
        "    real                :: tmp    !< @unit{'a}\n"
        "    if (depth > 0) then\n"
        "      call f(x, depth - 1, tmp)\n"
        "      y = tmp\n"
        "    else\n"
        "      y = x\n"
        "    end if\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    poly_codes = [d.code for d in diags if d.code in ("H020", "H023")]
    assert not poly_codes, [(d.code, d.message) for d in diags]


def test_recursive_call_with_wrong_unit_fires_h020(tmp_path: Path):
    """Negative case: even in a recursive call, a unit mismatch at the
    recursive site fires H020 — confirms the call-site dispatch runs
    on recursive calls just like external ones."""
    src = _materialise(tmp_path, "rec.f90",
        "module mod\n"
        "contains\n"
        "  recursive subroutine f(x, y)\n"
        "    real, intent(in)    :: x      !< @unit{'a}\n"
        "    real, intent(out)   :: y      !< @unit{'a}\n"
        "    real                :: bad    !< @unit{kg}\n"
        "    real                :: out_m  !< @unit{m}\n"
        "    call f(bad, out_m)\n"
        "    y = x\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H020" in codes, [(d.code, d.message) for d in diags]


def test_hover_clean_polymorphic_call_has_no_expected_trailer(tmp_path: Path):
    """A clean polymorphic call hover (every actual unifies cleanly
    with the formal tyvar) must render each arg row as bare ``unit 🟢``
    — no ``(expected 'a)`` row tail, no 🟡 demote. Mirrors the panel-
    side test ``test_panel_clean_polymorphic_call_has_no_expected_trailer``;
    the parallel ``_render_ast_tree`` path applies the same tyvar gate
    so the call hover, short hover, and CLI ``--trace`` output all
    agree the call is clean."""
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp import server
    from dimfort.lsp.hover import _render_ast_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

    src = _materialise(
        tmp_path, "poly_hover_clean.f90",
        "module m\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine f\n"
        "  subroutine caller_clean(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{m}\n"
        "    real, intent(out) :: b  !< @unit{m}\n"
        "    call f(a, b)\n"
        "  end subroutine caller_clean\n"
        "end module m\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    calls = [n for n in _ts.walk(tree.root_node)
             if n.type == "subroutine_call"]

    result = check_files([src])
    with server.state.last_result_lock:
        saved_result, server.state.last_result = server.state.last_result, result
    try:
        ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
        rows: list = []
        _render_ast_tree(
            calls[-1], ctx, source,
            prefix="", is_last=True, is_root=True, rows=rows,
        )
        arg_rows = [r for r in rows[1:] if "├──" in r[0] or "└──" in r[0]]
        assert len(arg_rows) >= 2, rows
        a_row = next(r for r in arg_rows if "a" in r[0])
        b_row = next(r for r in arg_rows if "b" in r[0])
        # Bare unit; no trailer; 🟢 marker.
        assert a_row[1] == "m", a_row
        assert b_row[1] == "m", b_row
        assert a_row[2] == "🟢", a_row
        assert b_row[2] == "🟢", b_row
        assert a_row[3] == "", a_row
        assert b_row[3] == "", b_row
    finally:
        with server.state.last_result_lock:
            server.state.last_result = saved_result


def test_hover_h020_arg_row_renders_collides_trailer(tmp_path: Path):
    """The detailed-hover tree (rendered by ``_render_ast_tree``) must
    surface H020's spec-faithful row form on every conflicting arg:
    unit column = ``'a = <actual>`` (the binding the slot would push),
    row tail = ``(collides with arg N)``, marker = 🔴. Regression pin:
    fix #2 updated the panel-side ``_build_expression_tree`` but the
    parallel ``_render_ast_tree`` was left rendering the generic
    ``(expected 'a)`` trailer at 🟡 — surfaces in VSCode's call hover,
    short hover, and CLI ``--trace`` output. See
    docs/design/shipped/polymorphic-units.md §H020."""
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp import server
    from dimfort.lsp.hover import _render_ast_tree
    from dimfort.lsp.tree_access import _build_ts_ctx

    src = _materialise(
        tmp_path, "poly_hover.f90",
        "module m\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine f\n"
        "  subroutine caller(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{kg}\n"
        "    real, intent(out) :: b  !< @unit{m}\n"
        "    call f(a, b)\n"
        "  end subroutine caller\n"
        "end module m\n"
    )
    source = src.read_bytes()
    tree = _ts.parse_text(source)
    resolved = src.resolve()
    calls = [n for n in _ts.walk(tree.root_node)
             if n.type == "subroutine_call"]

    result = check_files([src])
    with server.state.last_result_lock:
        saved_result, server.state.last_result = server.state.last_result, result
    try:
        ctx = _build_ts_ctx(result, source, str(resolved), path=resolved)
        rows: list = []
        _render_ast_tree(
            calls[-1], ctx, source,
            prefix="", is_last=True, is_root=True, rows=rows,
        )
        # rows are (label, unit, mark, extra) tuples.
        # Skip the root (the call itself); look at the immediate arg rows.
        arg_rows = [r for r in rows[1:] if "├──" in r[0] or "└──" in r[0]]
        assert len(arg_rows) >= 2, rows
        a_row = next(r for r in arg_rows if "a" in r[0])
        b_row = next(r for r in arg_rows if "b" in r[0])
        # Unit column carries the bare binding ``'a = <actual>``.
        assert a_row[1] == "'a = kg", a_row
        assert b_row[1] == "'a = m", b_row
        # Marker hard-pinned to 🔴 — the polymorphism conflict owns
        # each arg row directly, strictly stronger than the 🟡 demote
        # that an ``(expected …)`` trailer would trigger.
        assert a_row[2] == "🔴", a_row
        assert b_row[2] == "🔴", b_row
        # Row tail is the spec's ``(collides with arg N)`` form,
        # parallel to ``(expected …)`` / ``(assumed: …)`` for other
        # consistency surfaces.
        assert a_row[3] == "(collides with arg 2)", a_row
        assert b_row[3] == "(collides with arg 1)", b_row
    finally:
        with server.state.last_result_lock:
            server.state.last_result = saved_result
