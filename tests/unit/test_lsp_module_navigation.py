"""Module hover + go-to-definition on ``use foo`` cursors.

Hover should render a summary of the module's exports; go-to-def
should jump to the matching ``module foo`` declaration in whichever
file declares it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pygls")

from dimfort.core import unit_config  # noqa: F401
from dimfort.core.multifile import check_files
from dimfort.lsp import server as _server


def _drive(files: list[Path]):
    """Run check_files + stash the result in the LSP module globals."""
    result = check_files(files)
    with _server.state.last_result_lock:
        _server.state.last_result = result
    return result


def _hover(uri: str, line_1based: int, col_1based: int):
    return _server._resolve_hover(uri, line_1based, col_1based, None)


def _goto_definition_inline(uri: str, line_0based: int, col_0based: int):
    """Call the LSP definition handler without spinning a real client.

    The handler reads its arguments via :class:`lsp.DefinitionParams`,
    so we mock it the same way pygls would.
    """
    from lsprotocol import types as lsp
    params = lsp.DefinitionParams(
        text_document=lsp.TextDocumentIdentifier(uri=uri),
        position=lsp.Position(line=line_0based, character=col_0based),
    )
    return _server._definition(None, params)


def test_module_hover_renders_exports(tmp_path: Path):
    """Hovering ``physics`` inside ``use physics`` shows its exports."""
    physics = tmp_path / "physics.f90"
    physics.write_text(
        "module physics\n"
        "  real :: g  !< @unit{m/s^2}\n"
        "  real :: omega  !< @unit{1/s}\n"
        "contains\n"
        "  subroutine accel(out)\n"
        "    real :: out  !< @unit{m/s^2}\n"
        "  end subroutine\n"
        "end module\n"
    )
    driver = tmp_path / "driver.f90"
    driver.write_text(
        "subroutine driver\n"
        "  use physics\n"
        "end subroutine\n"
    )
    try:
        _drive([physics, driver])
        uri = driver.resolve().as_uri()
        # Line 2: "  use physics" — "physics" starts at column 7 (1-based).
        res = _hover(uri, line_1based=2, col_1based=7)
        assert res is not None, "expected a hover on the module name"
        text, _ = res
        assert "module" in text.lower()
        assert "physics" in text
        assert "g" in text and "omega" in text
        assert "accel" in text
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_module_hover_includes_unannotated_vars(tmp_path: Path):
    """Hover lists both annotated and unannotated module-level vars,
    with a header counter that surfaces the gap.
    """
    physics = tmp_path / "physics.f90"
    physics.write_text(
        "module physics\n"
        "  real :: g  !< @unit{m/s^2}\n"
        "  real :: orphan\n"               # no annotation
        "  real :: another_unset\n"        # no annotation
        "end module\n"
    )
    driver = tmp_path / "driver.f90"
    driver.write_text(
        "subroutine driver\n"
        "  use physics\n"
        "end subroutine\n"
    )
    try:
        _drive([physics, driver])
        uri = driver.resolve().as_uri()
        res = _hover(uri, line_1based=2, col_1based=7)
        assert res is not None
        text, _ = res
        # Counter reflects the 1-of-3 annotation gap.
        assert "1/3 annotated" in text, text
        # Annotated entry carries the unit; orphans carry the explicit
        # "no unit annotation" tag so the gap is unmistakable.
        assert "g" in text
        assert "orphan" in text
        assert "another_unset" in text
        assert "no unit annotation" in text
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_module_hover_unresolved(tmp_path: Path):
    """A ``use`` of an unknown module reports 'not found in workset'."""
    f = tmp_path / "only_consumer.f90"
    f.write_text(
        "subroutine driver\n"
        "  use nowhere\n"
        "end subroutine\n"
    )
    try:
        _drive([f])
        uri = f.resolve().as_uri()
        res = _hover(uri, line_1based=2, col_1based=7)
        assert res is not None
        text, _ = res
        assert "not found" in text.lower()
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_goto_definition_variable(tmp_path: Path):
    """Cmd-click on a variable reference jumps to its declaration."""
    f = tmp_path / "vars.f90"
    f.write_text(
        "subroutine foo\n"          # line 1
        "  real :: pte\n"           # line 2 — declaration here
        "  pte = 1.0\n"             # line 3 — usage
        "end subroutine\n"          # line 4
    )
    try:
        _drive([f])
        uri = f.resolve().as_uri()
        # Cursor on the 'pte' usage at line 3, column 3 (0-based: line 2, col 2).
        locs = _goto_definition_inline(uri, line_0based=2, col_0based=2)
        assert locs and len(locs) == 1, locs
        loc = locs[0]
        assert loc.uri == f.resolve().as_uri()
        # The declaration's 'pte' identifier is on line 2, column 10 (0-based: 1, 10).
        assert loc.range.start.line == 1
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_goto_definition_callable(tmp_path: Path):
    """Cmd-click on a call-callee jumps to the function/sub definition."""
    f = tmp_path / "calls.f90"
    f.write_text(
        "subroutine target(x)\n"            # line 1
        "  real :: x\n"
        "end subroutine\n"
        "subroutine caller\n"
        "  call target(1.0)\n"              # line 5 — callee here
        "end subroutine\n"
    )
    try:
        _drive([f])
        uri = f.resolve().as_uri()
        # Cursor on 'target' at line 5, column 8 (0-based: line 4, col 7).
        locs = _goto_definition_inline(uri, line_0based=4, col_0based=8)
        assert locs and len(locs) == 1, locs
        loc = locs[0]
        # Declaration is at line 1 (0-based 0) — 'subroutine target'.
        assert loc.range.start.line == 0
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None


def test_goto_definition_module(tmp_path: Path):
    """Go-to-def on ``use foo`` jumps to the ``module foo`` header."""
    physics = tmp_path / "physics.f90"
    physics.write_text(
        "module physics\n"
        "  real :: g\n"
        "end module\n"
    )
    driver = tmp_path / "driver.f90"
    driver.write_text(
        "subroutine driver\n"
        "  use physics\n"
        "end subroutine\n"
    )
    try:
        _drive([physics, driver])
        uri = driver.resolve().as_uri()
        # LSP positions are 0-based; "physics" is at column 6 (0-based) of line 1.
        locs = _goto_definition_inline(uri, line_0based=1, col_0based=6)
        assert locs and len(locs) == 1
        loc = locs[0]
        assert loc.uri == physics.resolve().as_uri()
        # The target name lives on line 0 ("module physics"), column 7.
        assert loc.range.start.line == 0
        assert loc.range.start.character == 7
    finally:
        with _server.state.last_result_lock:
            _server.state.last_result = None
