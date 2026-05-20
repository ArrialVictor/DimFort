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
from dimfort.lsp import server as _server


def _drive_hover(file: Path, line_1based: int, col_1based: int):
    """Populate ``_last_result`` and dispatch ``_resolve_hover`` directly.

    Bypasses pygls so we can exercise the resolver against a real
    workset without spinning a client/server pair.
    """
    result = check_files([file])
    with _server._last_result_lock:
        _server._last_result = result
    uri = file.resolve().as_uri()
    try:
        return _server._resolve_hover(uri, line_1based, col_1based, None)
    finally:
        with _server._last_result_lock:
            _server._last_result = None


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
    assert "m / s" in text_m or "m/s" in text_m or "ᐟs" in text_m, text_m
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
    _server._features.trace_hover = True
    try:
        # Hover on `r` (column 3 of line 5) — inside the assignment.
        hit = _drive_hover(f, 5, 3)
        assert hit is not None
        text, _ = hit
        # The hover for `r` shows its unit; the trace section appends
        # the rule-chain that produced the RHS unit.
        result = check_files([f])
        with _server._last_result_lock:
            _server._last_result = result
        try:
            extra = _server._trace_section_for(f.resolve().as_uri(), 5, 3)
        finally:
            with _server._last_result_lock:
                _server._last_result = None
        assert extra is not None
        assert "Unit-algebra trace" in extra
        assert "R3.1" in extra  # LOG fires
        assert "R5.1" in extra  # log homomorphism
        # ASCII tree connectors signal the new tree layout
        assert "├──" in extra or "└──" in extra
    finally:
        _server._features.trace_hover = False


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
    assert _server._features.trace_hover is False
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
    with _server._last_result_lock:
        _server._last_result = result
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
        actions = _server._h010_extract_to_parameter_actions(cap, _Doc(src), f.resolve())
    finally:
        with _server._last_result_lock:
            _server._last_result = None

    assert len(actions) == 1
    action = actions[0]
    assert "Extract literal '1.'" in action.title
    assert "m/s" in action.title
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
