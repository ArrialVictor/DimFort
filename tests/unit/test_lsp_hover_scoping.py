"""Hover resolves identifiers honouring their enclosing routine scope.

Two routines in one file declaring same-named parameters with
different units used to confuse the bare-identifier hover path
(``merged_var_units`` is first-seen-wins). The scope-aware lookup
keeps each routine's annotation distinct.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pygls")

from dimfort.core import unit_config  # noqa: F401
from dimfort.core.multifile import check_files
from dimfort.lsp import code_action as _code_action
from dimfort.lsp import hover as _hover
from dimfort.lsp import server as _server


def _drive_hover(file: Path, line_1based: int, col_1based: int):
    """Populate ``state.last_result`` and dispatch the full hover pipeline:
    specific hover first, then the expression-context fallback —
    mirroring ``_hover``'s logic without pygls.
    """
    result = check_files([file])
    with _server.state.last_result_lock:
        _server.state.last_result = result
    uri = file.resolve().as_uri()
    mode = _server._features.hover
    try:
        hit = _hover._resolve_hover(uri, line_1based, col_1based, None, hover_mode=mode)
        if hit is None:
            hit = _hover._expression_hover_for(uri, line_1based, col_1based, hover_mode=mode)
        return hit
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_hover_picks_per_routine_unit(tmp_path: Path):
    """Hover on ``pte`` inside ``orodrag`` vs ``orolift`` shows each
    routine's own annotation, not the first-seen across the file."""
    src = (
        "subroutine orodrag(pte)\n"           # line 1
        "  real :: pte  !< @unit{m/s}\n"      # line 2 — declared m/s
        "  pte = 0.0\n"                       # line 3 — usage
        "end subroutine\n"                    # line 4
        "subroutine orolift(pte)\n"           # line 5
        "  real :: pte  !< @unit{K/s}\n"      # line 6 — declared K/s
        "  pte = 0.0\n"                       # line 7 — usage
        "end subroutine\n"                    # line 8
    )
    f = tmp_path / "scoped_hover.f90"
    f.write_text(src)

    # Hover the ``pte`` on line 3 (inside orodrag) — column points at
    # the first character of the name (1-based).
    res_m = _drive_hover(f, line_1based=3, col_1based=3)
    assert res_m is not None
    text_m, _ = res_m
    assert "m·s⁻¹" in text_m, text_m
    assert "K" not in text_m, text_m

    # Hover the ``pte`` on line 7 (inside orolift).
    res_k = _drive_hover(f, line_1based=7, col_1based=3)
    assert res_k is not None
    text_k, _ = res_k
    assert "K" in text_k, text_k


def test_hover_trace_section_when_enabled(tmp_path: Path):
    """With trace_hover on, hovering inside an assignment appends a trace block."""
    src = (
        "subroutine s\n"
        "  real :: p1   !< @unit{Pa}\n"
        "  real :: p2   !< @unit{Pa}\n"
        "  real :: r    !< @unit{LOG(Pa^2)}\n"
        "  r = log(p1) + log(p2)\n"
        "end subroutine\n"
    )
    f = tmp_path / "trace.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        # Hover on `r` (column 3 of line 5) — inside the assignment.
        hit = _drive_hover(f, 5, 3)
        assert hit is not None
        text, _ = hit
        # The hover for `r` shows its unit; the trace section appends
        # the rule-chain that produced the RHS unit.
        result = check_files([f])
        with _server.state.last_result_lock:
            _server.state.last_result = result
        try:
            extra = _hover._trace_section_for(f.resolve().as_uri(), 5, 3)
        finally:
            with _server.state.last_result_lock:
                _server.state.last_result = None
        assert extra is not None
        assert "Unit-algebra trace" in extra
        # ASCII tree connectors signal the new tree layout
        assert "├──" in extra or "└──" in extra
        # The trace shows each subexpression's resolved unit; rule IDs
        # were dropped (debug-only; not useful to physicists).
        assert "log(p1)" in extra
        assert "LOG(kg·m⁻¹·s⁻²)" in extra  # log(Pa) → LOG(SI(Pa))
        assert "R3." not in extra and "R5." not in extra
    finally:
        _server._features.hover = "short"


def test_hover_no_trace_section_when_disabled(tmp_path: Path):
    """With trace_hover off (default), the trace section is not appended."""
    src = (
        "subroutine s\n"
        "  real :: p   !< @unit{Pa}\n"
        "  real :: lp  !< @unit{LOG(Pa)}\n"
        "  lp = log(p)\n"
        "end subroutine\n"
    )
    f = tmp_path / "no_trace.f90"
    f.write_text(src)
    assert _server._features.hover == "short"
    hit = _drive_hover(f, 4, 3)
    assert hit is not None
    text, _ = hit
    assert "Unit-algebra trace" not in text


def test_h010_extract_to_parameter_action(tmp_path: Path):
    """The D1.5 implicit-cast diagnostic offers an 'Extract literal to
    PARAMETER' quick action that edits two ranges: the literal use-site
    becomes the new symbol name, and a typed PARAMETER declaration is
    inserted after the existing declarations."""
    pygls_lsp = pytest.importorskip("lsprotocol.types")
    src = (
        "subroutine demo\n"
        "  real :: speed   !< @unit{m/s}\n"
        "  real :: result  !< @unit{m/s}\n"
        "  result = 1. + speed\n"
        "end subroutine\n"
    )
    f = tmp_path / "qf.f90"
    f.write_text(src)
    result = check_files([f])
    with _server.state.last_result_lock:
        _server.state.last_result = result
    try:
        # Mock a pygls workspace doc with .lines
        class _Doc:
            def __init__(self, text: str):
                self.lines = text.splitlines(keepends=True)
        h010 = next(
            d for d in result.diagnostics[f.resolve()]
            if d.code == "H010" and "Implicit cast" in d.message
        )
        # Construct a CodeActionParams-shaped object from the real diag.
        diag_range = pygls_lsp.Range(
            start=pygls_lsp.Position(line=h010.start.line - 1, character=h010.start.column - 1),
            end=pygls_lsp.Position(line=h010.end.line - 1, character=h010.end.column - 1),
        )
        # Hand-build a minimal CodeActionParams: text_document, range, context.
        text_doc = pygls_lsp.TextDocumentIdentifier(uri=f.resolve().as_uri())
        diag_lsp = pygls_lsp.Diagnostic(
            range=diag_range, message=h010.message, code=h010.code,
            severity=pygls_lsp.DiagnosticSeverity.Warning,
        )
        ctx = pygls_lsp.CodeActionContext(diagnostics=[diag_lsp])
        cap = pygls_lsp.CodeActionParams(
            text_document=text_doc, range=diag_range, context=ctx,
        )
        actions = _code_action._h010_extract_to_parameter_actions(cap, _Doc(src), f.resolve())
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None

    assert len(actions) == 1
    action = actions[0]
    assert "Extract literal '1.'" in action.title
    assert "m·s⁻¹" in action.title
    # The action is delegated to the extension as a Command so VSCode
    # can prompt the user for the parameter name before applying the
    # edits. The args carry everything needed for the two-edit refactor.
    assert action.edit is None
    cmd = action.command
    assert cmd is not None
    assert cmd.command == "dimfort.extractToParameter"
    args = cmd.arguments
    assert args[0] == f.resolve().as_uri()
    # arg slots: uri, range_start, range_end, insert_line, indent,
    # literal_text, target_unit, default_name
    assert args[5] == "1."
    assert args[6] == "m/s"
    assert args[7].startswith("c_h010_")


def test_hover_marks_intrinsic_default_on_integer(tmp_path: Path):
    """Hover on a bare ``integer :: i`` (implicit dim'less default) shows
    the *(implicit — INTEGER default)* suffix; an explicitly-annotated
    integer does not."""
    src = (
        "subroutine s\n"
        "  integer :: ig2\n"                # implicit default
        "  integer :: epoch   !< @unit{s}\n" # explicit
        "  ig2 = 1\n"
        "  epoch = 0\n"
        "end subroutine\n"
    )
    f = tmp_path / "hover_int.f90"
    f.write_text(src)
    hit_ig = _drive_hover(f, 4, 3)
    assert hit_ig is not None
    text_ig, _ = hit_ig
    assert "implicit" in text_ig and "INTEGER default" in text_ig

    hit_ep = _drive_hover(f, 5, 3)
    assert hit_ep is not None
    text_ep, _ = hit_ep
    assert "implicit" not in text_ep
    assert "INTEGER default" not in text_ep


def _drive_trace_hover(file: Path, line_1based: int, col_1based: int):
    """Populate ``state.last_result`` and dispatch ``_expression_hover_for`` directly."""
    result = check_files([file])
    with _server.state.last_result_lock:
        _server.state.last_result = result
    uri = file.resolve().as_uri()
    try:
        return _hover._expression_hover_for(
            uri, line_1based, col_1based, hover_mode=_server._features.hover,
        )
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_trace_hover_inside_call_argument(tmp_path: Path):
    """Cursor inside a subroutine-call argument expression renders a
    trace tree rooted at that argument, with the neutral 🟡 marker."""
    src = (
        "subroutine demo\n"
        "  real :: p1   !< @unit{Pa}\n"
        "  real :: p2   !< @unit{Pa}\n"
        "  call foo(p1 + p2)\n"
        "end subroutine\n"
    )
    f = tmp_path / "call_arg.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        # Column 14 sits inside `p1 + p2` (the argument).
        hit = _drive_trace_hover(f, 4, 14)
        assert hit is not None
        text, _ = hit
        # Header now reflects the worst row — all clean → 🟢.
        assert "🟢 DimFort" in text
        assert "p1 + p2" in text
        # Rule IDs are no longer rendered in tree rows.
        assert "R4." not in text
    finally:
        _server._features.hover = "short"


def test_trace_hover_inside_if_condition(tmp_path: Path):
    """Cursor inside an IF condition traces the relational expression."""
    src = (
        "subroutine demo\n"
        "  real :: p1   !< @unit{Pa}\n"
        "  real :: p2   !< @unit{Pa}\n"
        "  if (p1 + p2 > 0.0) then\n"
        "  end if\n"
        "end subroutine\n"
    )
    f = tmp_path / "if_cond.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        # Column 9 sits inside `p1 + p2 > 0.0`. Markers are diagnostic-
        # driven (docs/design/markers.md) and the checker does NOT emit for
        # relational operand mismatches — so the comparison carries no
        # consistency diagnostic and the marker is 🟡 (no unit / not
        # checked), not a re-derived 🔴. Restoring a backed 🔴 here is the
        # deferred relational-emission enhancement (markers.md §6.1).
        hit = _drive_trace_hover(f, 4, 9)
        assert hit is not None
        text, _ = hit
        assert "🟡 DimFort" in text
        assert "p1" in text and "p2" in text
    finally:
        _server._features.hover = "short"


def test_trace_hover_inside_do_bound(tmp_path: Path):
    """Cursor inside a DO loop bound expression traces that bound."""
    src = (
        "subroutine demo\n"
        "  integer :: i\n"
        "  integer :: n\n"
        "  do i = 1, n + 1\n"
        "  end do\n"
        "end subroutine\n"
    )
    f = tmp_path / "do_bound.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        # Column 15 sits inside `n + 1`. Both operands are integer
        # default dim'less → 🟢.
        hit = _drive_trace_hover(f, 4, 15)
        assert hit is not None
        text, _ = hit
        assert "🟢 DimFort" in text
    finally:
        _server._features.hover = "short"


def test_call_hover_short_renders_root_and_arg_rows(tmp_path: Path):
    """Short call hover: root row is the whole call expression
    (`name(args)` for subroutines, `name(args) : ret` for functions)
    with the overall verdict marker; one child row per actual argument
    labelled by the source expression. Layout matches the side panel's
    Expression tree — both surfaces share :func:`_render_ast_tree`."""
    src = (
        "module m\n"
        "contains\n"
        "  subroutine foo(a, b)\n"
        "    real, intent(in) :: a   !< @unit{Pa}\n"
        "    real, intent(in) :: b   !< @unit{Pa}\n"
        "  end subroutine\n"
        "  subroutine demo\n"
        "    real :: p1   !< @unit{Pa}\n"
        "    real :: p2   !< @unit{Pa}\n"
        "    call foo(p1, p2 + p1)\n"
        "  end subroutine\n"
        "end module\n"
    )
    f = tmp_path / "call_short.f90"
    f.write_text(src)
    _server._features.hover = "short"
    try:
        # Column 10 sits on `foo` (the callee) on line 10.
        hit = _drive_hover(f, 10, 10)
        assert hit is not None
        text, _ = hit
        # Root row: full call as written. Subroutine — no `: ret` block.
        assert "call foo(p1, p2 + p1)" in text
        # Both positional args render as child rows.
        assert "p1" in text
        assert "p2 + p1" in text
        # Old "Signature ◂ Call" two-column layout is gone, and the
        # earlier `name: (…) → ret` header line is too.
        assert "Signature" not in text
        assert "◂" not in text
        assert "foo: (" not in text
        # Subroutine calls have no return unit by structure, so the
        # root row's unit column renders `-` (the structural-no-unit
        # glyph) — distinct from `?` which is reserved for *unknown*
        # units. All args matching → header 🟢. Subroutine_call is in
        # _NO_UNIT_NODE_TYPES, so its resolution-axis base is 🟢 (a
        # clean subroutine is not "unresolved").
        assert "🟢 DimFort" in text
        assert " : -" in text or "  -  " in text  # the structural-no-unit glyph
        assert " : ?" not in text  # never `?` for subroutine roots
    finally:
        _server._features.hover = "short"


def test_call_hover_function_root_carries_return_unit(tmp_path: Path):
    """Function call root row carries `name(args) : ret`; subroutines
    drop the `: ret` block. Regression guard for the defensive
    callee-strip that used to swallow the first argument."""
    src = (
        "module m\n"
        "contains\n"
        "  function dynamic_pressure(rho, v) result(p)\n"
        "    real, intent(in) :: rho   !< @unit{kg/m^3}\n"
        "    real, intent(in) :: v     !< @unit{m/s}\n"
        "    real :: p                 !< @unit{kg/(m*s^2)}\n"
        "    p = 0.5 * rho * v * v\n"
        "  end function\n"
        "  subroutine demo\n"
        "    real :: rho   !< @unit{kg/m^3}\n"
        "    real :: v     !< @unit{m/s}\n"
        "    real :: p     !< @unit{kg/(m*s^2)}\n"
        "    p = dynamic_pressure(rho, v)\n"
        "  end subroutine\n"
        "end module\n"
    )
    f = tmp_path / "call_func.f90"
    f.write_text(src)
    _server._features.hover = "short"
    try:
        # `dynamic_pressure` on line 13 starts at column 9.
        hit = _drive_hover(f, 13, 9)
        assert hit is not None
        text, _ = hit
        # Root row carries the return unit attached to the call expression.
        assert "dynamic_pressure(rho, v)" in text
        assert "kg·m⁻¹·s⁻²" in text
        # Both positional args show up as children (regression guard).
        assert "rho" in text
        # Find a child-row line for `v` specifically (avoid matching `v`
        # inside `rho` etc.).
        assert any(
            line.lstrip().startswith(("├── v", "└── v"))
            for line in text.splitlines()
        )
        assert "🟢 DimFort" in text
    finally:
        _server._features.hover = "short"


def test_call_hover_mismatch_paints_arg_yellow_and_call_red(tmp_path: Path):
    """Mismatched actual: the arg row carries `(expected …)` and gets
    the 🟡-on-expected marker override (the expression itself resolved
    cleanly), while the enclosing call row paints 🔴 because H004 fires
    on it. Header marker rolls up to 🔴."""
    src = (
        "module m\n"
        "contains\n"
        "  subroutine foo(a)\n"
        "    real, intent(in) :: a   !< @unit{Pa}\n"
        "  end subroutine\n"
        "  subroutine demo\n"
        "    real :: t   !< @unit{s}\n"
        "    call foo(t)\n"
        "  end subroutine\n"
        "end module\n"
    )
    f = tmp_path / "call_mismatch.f90"
    f.write_text(src)
    _server._features.hover = "short"
    try:
        hit = _drive_hover(f, 8, 10)
        assert hit is not None
        text, _ = hit
        # Header marker is 🔴 (call row owns H004).
        assert "🔴 DimFort" in text
        # Arg row carries `(expected …)` with Pa rendered SI-form.
        assert "(expected kg·m⁻¹·s⁻²)" in text
        # Arg row is 🟡 (resolved cleanly to `s` here, but disagrees
        # with the formal). The 🔴 sits on the call row above it.
        arg_line = next(
            line for line in text.splitlines()
            if "(expected kg·m⁻¹·s⁻²)" in line
        )
        assert "🟡" in arg_line
        assert "🔴" not in arg_line
    finally:
        _server._features.hover = "short"


def test_call_hover_detailed_expands_computed_args(tmp_path: Path):
    """Detailed mode: a computed actual arg expands a sub-tree under
    its row showing the operand chain."""
    src = (
        "module m\n"
        "contains\n"
        "  subroutine foo(a, b)\n"
        "    real, intent(in) :: a   !< @unit{Pa}\n"
        "    real, intent(in) :: b   !< @unit{Pa}\n"
        "  end subroutine\n"
        "  subroutine demo\n"
        "    real :: p1   !< @unit{Pa}\n"
        "    real :: p2   !< @unit{Pa}\n"
        "    call foo(p1, p2 + p1)\n"
        "  end subroutine\n"
        "end module\n"
    )
    f = tmp_path / "call_detailed.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        hit = _drive_hover(f, 10, 10)
        assert hit is not None
        text, _ = hit
        # The computed arg `p2 + p1` shows up as a row, and its tree of
        # operands is rendered underneath.
        assert "p2 + p1" in text
        assert "├──" in text or "└──" in text
    finally:
        _server._features.hover = "short"


def test_signature_hover_collapses_to_header_with_unannotated_slot(
    tmp_path: Path,
):
    """Cursor on a *definition* header (no call) renders ONLY the
    dimensional signature line. Unannotated formal slots render as `?`
    and trigger the 🟡 header marker."""
    src = (
        "module m\n"
        "contains\n"
        "  function ek(v) result(e)\n"
        "    real, intent(in) :: v          !< @unit{m/s}\n"
        "    real :: e\n"  # unannotated return
        "    e = 0.5 * v * v\n"
        "  end function\n"
        "end module\n"
    )
    f = tmp_path / "sig_hover.f90"
    f.write_text(src)
    _server._features.hover = "short"
    try:
        # `ek` on line 3 starts at column 12.
        hit = _drive_hover(f, 3, 12)
        assert hit is not None
        text, _ = hit
        # Single-line signature header, with `?` flagging the unannotated
        # return slot.
        assert "ek(m·s⁻¹) : ?" in text
        assert "🟡 DimFort" in text
        # No per-arg row table — the pure-signature hover collapses to
        # just the header.
        assert "├──" not in text and "└──" not in text
        assert "◂" not in text
    finally:
        _server._features.hover = "short"


def test_expression_detailed_assignment_marks_root_row(tmp_path: Path):
    """Detailed mode: the root (assignment) row carries its verdict marker
    on the row itself — matching the side panel — not only in the header."""
    src = (
        "subroutine demo\n"
        "  real :: c_sound  !< @unit{m/s}\n"
        "  real :: t        !< @unit{s}\n"
        "  real :: bogus    !< @unit{kg}\n"
        "  bogus = c_sound * t\n"
        "end subroutine\n"
    )
    f = tmp_path / "asn_detailed.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        hit = _drive_hover(f, 5, 9)  # on `=`
        assert hit is not None
        text, _ = hit
        # The root row (assignment label, not a ├──/└── child and not the
        # bold header) must carry the 🔴 marker on the row.
        root_rows = [
            ln for ln in text.splitlines()
            if "bogus = c_sound * t" in ln and "DimFort" not in ln
        ]
        assert root_rows and "🔴" in root_rows[0], text
    finally:
        _server._features.hover = "short"


def test_expression_short_assignment(tmp_path: Path):
    """Short mode: cursor on `=` inside `r = a + b` renders the
    root-plus-immediate-children tree shape — same as every other
    short hover. Assignment is structural-no-unit on the root, with
    `-` in the unit column."""
    src = (
        "subroutine demo\n"
        "  real :: a   !< @unit{Pa}\n"
        "  real :: b   !< @unit{Pa}\n"
        "  real :: r   !< @unit{Pa}\n"
        "  r = a + b\n"
        "end subroutine\n"
    )
    f = tmp_path / "asn_short.f90"
    f.write_text(src)
    _server._features.hover = "short"
    # Cursor on `=` (col 5 of line 5).
    hit = _drive_hover(f, 5, 5)
    assert hit is not None
    text, _ = hit
    assert "🟢 DimFort" in text
    # `◂` is gone — all short hovers use the tree shape now.
    assert "◂" not in text
    # Both LHS and RHS are immediate children of the assignment.
    assert "r" in text and "a + b" in text
    # Root row is the assignment itself (structural-no-unit `-`).
    assert " : -" in text or "  -  " in text


def test_expression_short_assignment_mismatch_shows_expected(tmp_path: Path):
    """Assignment short hover with a homogeneity violation: the RHS row
    surfaces `(expected <lhs_unit>)` (same as a call-arg mismatch) and
    paints 🟡 from the expected-override; the root assignment row paints
    🔴 because H001 owns it."""
    src = (
        "subroutine demo\n"
        "  real :: bogus    !< @unit{kg}\n"
        "  real :: c_sound  !< @unit{m/s}\n"
        "  real :: t        !< @unit{s}\n"
        "  bogus = c_sound * t\n"
        "end subroutine\n"
    )
    f = tmp_path / "asn_expected.f90"
    f.write_text(src)
    _server._features.hover = "short"
    hit = _drive_hover(f, 5, 9)  # on `=`
    assert hit is not None
    text, _ = hit
    # Root assignment paints 🔴 because H001 owns it.
    assert "🔴 DimFort" in text
    # RHS row shows the expected-unit annotation (Pa → SI form would
    # apply if the LHS were Pa, but here LHS is kg).
    assert "(expected kg)" in text
    # The RHS row carries 🟡 from the expected-override (not 🔴).
    rhs_line = next(
        line for line in text.splitlines() if "(expected kg)" in line
    )
    assert "🟡" in rhs_line
    assert "🔴" not in rhs_line


def test_expression_detailed_assignment_mismatch_shows_expected(tmp_path: Path):
    """Detailed-mode hover for an assignment mismatch surfaces the same
    `(expected <lhs_unit>)` annotation + 🟡-on-expected demotion on
    the RHS row as the short hover and the panel — the detailed-mode
    manual row assembly used to skip the propagation."""
    src = (
        "subroutine demo\n"
        "  real :: bogus    !< @unit{kg}\n"
        "  real :: c_sound  !< @unit{m/s}\n"
        "  real :: t        !< @unit{s}\n"
        "  bogus = c_sound * t\n"
        "end subroutine\n"
    )
    f = tmp_path / "asn_expected_detailed.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        hit = _drive_hover(f, 5, 9)  # on `=`
    finally:
        _server._features.hover = "short"
    assert hit is not None
    text, _ = hit
    # Root assignment paints 🔴 because H001 owns it.
    assert "🔴 DimFort" in text
    # RHS row shows the expected-unit annotation.
    assert "(expected kg)" in text
    # The RHS row carries 🟡 from the expected-override (not 🔴).
    rhs_line = next(
        line for line in text.splitlines() if "(expected kg)" in line
    )
    assert "🟡" in rhs_line
    assert "🔴" not in rhs_line


def test_expression_short_assignment_mismatch_marker(tmp_path: Path):
    """A unit mismatch on the assignment surfaces as 🔴 in short mode."""
    src = (
        "subroutine demo\n"
        "  real :: a   !< @unit{Pa}\n"
        "  real :: r   !< @unit{K}\n"
        "  r = a\n"
        "end subroutine\n"
    )
    f = tmp_path / "asn_mismatch.f90"
    f.write_text(src)
    _server._features.hover = "short"
    hit = _drive_hover(f, 4, 5)  # on `=`
    assert hit is not None
    text, _ = hit
    assert "🔴 DimFort" in text


def test_expression_short_relational(tmp_path: Path):
    """Cursor on `>` inside `if (p > 0.0) then` renders the same tree
    shape as every other short hover — relational expressions are
    structural-no-unit on the root, with one row per operand."""
    src = (
        "subroutine demo\n"
        "  real :: p   !< @unit{Pa}\n"
        "  if (p > 0.0) then\n"
        "    p = 0.0\n"
        "  end if\n"
        "end subroutine\n"
    )
    f = tmp_path / "rel_short.f90"
    f.write_text(src)
    _server._features.hover = "short"
    # Cursor on `>` (col 9 of line 3).
    hit = _drive_hover(f, 3, 9)
    assert hit is not None
    text, _ = hit
    # `◂` is gone — tree shape across all hovers.
    assert "◂" not in text
    assert "p" in text and "0.0" in text
    # Relational root is structural-no-unit `-`.
    assert " : -" in text or "  -  " in text


def test_detailed_hover_assignment_with_unit_assume(tmp_path: Path):
    """Cursor on `=` of an assumed assignment, detailed mode: the
    detailed-mode path renders the assignment row + LHS leaf
    manually and then calls _render_ast_tree on the RHS. It must
    pass the assumed_overlay through so the RHS row shows the
    asserted unit + 🔵 + `(assumed: …)`, not the unresolved `?` 🟡
    (regression guard for the detailed/trace plumbing)."""
    src = (
        "subroutine s\n"
        "  real :: r       !< @unit{m}\n"
        "  real :: rho     !< @unit{kg/m^3}\n"
        "  rho = 1.e3 * 0.178 * (r * 2.0 * 1000.0)**(-0.922)"
        "   !< @unit_assume{kg/m^3 : empirical-fit Brandes2007}\n"
        "end subroutine\n"
    )
    f = tmp_path / "assumed_detailed.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        # Cursor on `=` (column 7 of line 4).
        hit = _drive_hover(f, 4, 7)
        assert hit is not None
        text, _ = hit
        # Assignment row stays clean (LHS unit matches asserted RHS).
        assert "🟢 DimFort" in text
        # RHS row carries the asserted unit, 🔵 marker, and the reason.
        assert "kg·m⁻³" in text
        assert "🔵" in text
        assert "(assumed: empirical-fit Brandes2007)" in text
        # The pre-fix bug: RHS row showed `?` 🟡 instead of the overlay.
        # The unresolved `?` may still appear DEEP inside the sub-tree
        # (on the (-0.922) leaf), but the RHS root itself must carry
        # the asserted unit.
        rhs_root_line = next(
            (line for line in text.splitlines()
             if line.lstrip().startswith("└── 1.e3 * 0.178")),
            "",
        )
        assert "kg·m⁻³" in rhs_root_line and "🔵" in rhs_root_line, rhs_root_line
    finally:
        _server._features.hover = "short"


def test_intrinsic_call_hover_uses_same_tree_shape_as_user_call(tmp_path: Path):
    """Hovering on an intrinsic callee (e.g. `log`) renders the same
    root-plus-immediate-children tree shape as a user-defined call,
    so the two surfaces look identical (just no `(expected …)` since
    intrinsics aren't in ctx.signatures)."""
    src = (
        "subroutine demo\n"
        "  real :: p   !< @unit{Pa}\n"
        "  real :: lp  !< @unit{LOG(Pa)}\n"
        "  lp = log(p)\n"
        "end subroutine\n"
    )
    f = tmp_path / "intrinsic_hover.f90"
    f.write_text(src)
    _server._features.hover = "short"
    # Cursor on `log` (col 8 of line 4).
    hit = _drive_hover(f, 4, 8)
    assert hit is not None
    text, _ = hit
    # Root row shows the call expression with its resolved unit, and a
    # child row for the actual `p` argument is rendered — matching the
    # user-call tree shape exactly.
    assert "log(p)" in text
    assert "LOG(kg·m⁻¹·s⁻²)" in text  # LOG(Pa) in SI form
    assert "├──" in text or "└──" in text
    # The bare-identifier-fallback rendering (`**log(p)** : LOG(Pa)`)
    # is gone — header is now `**🟢 DimFort**`, not the bare-identifier
    # `**🟢 DimFort**\n\n**log(p)** : …` form.
    assert "**log(p)**" not in text


def test_expression_short_subexpr_in_call_arg(tmp_path: Path):
    """Cursor on `+` inside a computed call argument renders the
    `+` expression as the same root-plus-immediate-children tree
    used everywhere else."""
    src = (
        "subroutine demo\n"
        "  real :: a   !< @unit{Pa}\n"
        "  real :: b   !< @unit{Pa}\n"
        "  call foo(a + b)\n"
        "end subroutine\n"
    )
    f = tmp_path / "subexpr.f90"
    f.write_text(src)
    _server._features.hover = "short"
    # Cursor on `+` inside `a + b` (col 14 of line 4). The operator
    # is more specific than the enclosing call arg, so the hover
    # renders the `a + b` tree.
    hit = _drive_hover(f, 4, 14)
    assert hit is not None
    text, _ = hit
    assert "a + b" in text  # root row label
    assert "├──" in text or "└──" in text  # child rows present
    # `◂` is gone.
    assert "◂" not in text
    # Both operands are Pa → 🟢.
    assert "🟢 DimFort" in text


def test_expression_short_assignment_skips_line_continuation(tmp_path: Path):
    """Fortran line-continuation tokens (``&``) appear as children of
    the assignment alongside the actual RHS expression. The RHS picker
    must skip them and land on the real expression, not on ``&``."""
    src = (
        "subroutine demo\n"
        "  real :: a   !< @unit{Pa}\n"
        "  real :: b   !< @unit{Pa}\n"
        "  real :: r   !< @unit{Pa}\n"
        "  r = &\n"
        "    a + b\n"
        "end subroutine\n"
    )
    f = tmp_path / "cont.f90"
    f.write_text(src)
    _server._features.hover = "short"
    # Cursor on `=` (col 5 of line 5).
    hit = _drive_hover(f, 5, 5)
    assert hit is not None
    text, _ = hit
    # The RHS in the rendered hover must be the actual expression,
    # not the continuation glyph.
    assert "a + b" in text
    assert "& : ?" not in text


def test_expression_short_numeric_literal(tmp_path: Path):
    """A bare numeric literal hover resolves to dim'less (`1`)."""
    src = (
        "subroutine demo\n"
        "  real :: p   !< @unit{Pa}\n"
        "  p = 3.14\n"
        "end subroutine\n"
    )
    f = tmp_path / "lit_short.f90"
    f.write_text(src)
    _server._features.hover = "short"
    # Cursor on `3.14` (col 8 of line 3).
    hit = _drive_hover(f, 3, 8)
    assert hit is not None
    text, _ = hit
    assert "3.14" in text
    assert "🟢 DimFort" in text


def test_trace_hover_outside_any_context_returns_none(tmp_path: Path):
    """Cursor on a declaration line (no expression context) returns None
    so the regular hover takes over."""
    src = (
        "subroutine demo\n"
        "  real :: p   !< @unit{Pa}\n"
        "  p = 0.0\n"
        "end subroutine\n"
    )
    f = tmp_path / "decl.f90"
    f.write_text(src)
    _server._features.hover = "detailed"
    try:
        # Line 2 is the declaration — no enclosing assignment or context.
        hit = _drive_trace_hover(f, 2, 11)
        assert hit is None
    finally:
        _server._features.hover = "short"


def test_hover_setting_parsed_from_init_options():
    """The single ``hover`` enum is read from initializationOptions, and
    legacy clients (traceHoverEnabled / per-surface 'detailed') still map
    onto it."""
    from types import SimpleNamespace

    from dimfort.lsp import server as _srv

    def init(opts):
        _srv._initialize(None, SimpleNamespace(
            workspace_folders=None, root_uri=None,
            initialization_options=opts))

    try:
        init({"hover": "disabled"})
        assert _srv._features.hover == "disabled"
        init({"hover": "detailed"})
        assert _srv._features.hover == "detailed"
        init({"hover": "short"})
        assert _srv._features.hover == "short"
        # Legacy back-compat: traceHoverEnabled=true -> detailed.
        init({"traceHoverEnabled": True, "hoverExpressions": "short"})
        assert _srv._features.hover == "detailed"
        # Legacy per-surface detailed -> detailed.
        init({"hoverFunctionCalls": "detailed"})
        assert _srv._features.hover == "detailed"
        # Nothing -> short.
        init({})
        assert _srv._features.hover == "short"
    finally:
        _srv._features.hover = "short"
