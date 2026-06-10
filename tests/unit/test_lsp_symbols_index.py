"""Workset-wide goto-def name index (audit #12).

Covers the index builder and the multi-match / use-clause filter
contract added to ``definition.resolve``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pygls")

from dimfort.core.multifile import check_files
from dimfort.core.workspace_index import scan_workspace
from dimfort.lsp import server as _server
from dimfort.lsp.symbols_index import build_symbols_index


def _drive(files: list[Path]):
    result = check_files(files)
    result.symbols_by_name_lc = build_symbols_index(result.trees)
    # Build the workspace use-graph too so definition.resolve's
    # visibility filter has data to consult — without it, the filter
    # falls back to "show all candidates" and use-clause narrowing
    # can't be exercised.
    workspace_root = files[0].parent
    with _server.state.workspace_index_lock:
        _server.state.workspace_index = scan_workspace([workspace_root])
    with _server.state.last_result_lock:
        _server.state.last_result = result
    return result


def _goto(uri: str, line_0: int, col_0: int):
    from lsprotocol import types as lsp
    params = lsp.DefinitionParams(
        text_document=lsp.TextDocumentIdentifier(uri=uri),
        position=lsp.Position(line=line_0, character=col_0),
    )
    return _server._definition(None, params)


def test_index_contains_module_function_var_entries(tmp_path: Path) -> None:
    """Every declaration kind surfaces with the right ``kind`` tag."""
    f = tmp_path / "sample.f90"
    f.write_text(
        "module mymod\n"
        "  real :: x_var\n"
        "contains\n"
        "  subroutine sub_a\n"
        "  end subroutine\n"
        "  function fn_b()\n"
        "    real :: fn_b\n"
        "  end function\n"
        "end module\n"
    )
    result = _drive([f])
    kinds = {
        name: {e.kind for e in entries}
        for name, entries in result.symbols_by_name_lc.items()
    }
    assert "module" in kinds["mymod"]
    assert "callable" in kinds["sub_a"]
    assert "callable" in kinds["fn_b"]
    assert "var" in kinds["x_var"]


def test_index_is_case_insensitive(tmp_path: Path) -> None:
    """Differently-cased names collapse onto one lower-cased key."""
    f = tmp_path / "case.f90"
    f.write_text(
        "module CamelCase\n"
        "  real :: MixedVar\n"
        "end module\n"
    )
    result = _drive([f])
    assert "camelcase" in result.symbols_by_name_lc
    assert "mixedvar" in result.symbols_by_name_lc


def test_index_collects_duplicates_across_files(tmp_path: Path) -> None:
    """A name declared in two files yields two SymbolEntry records."""
    a = tmp_path / "a.f90"
    a.write_text(
        "module mod_a\n"
        "  real :: shared_name\n"
        "end module\n"
    )
    b = tmp_path / "b.f90"
    b.write_text(
        "module mod_b\n"
        "  real :: shared_name\n"
        "end module\n"
    )
    result = _drive([a, b])
    entries = result.symbols_by_name_lc["shared_name"]
    files = {e.file for e in entries}
    assert a.resolve() in files
    assert b.resolve() in files


def test_goto_def_returns_multiple_when_visibility_filter_passes(
    tmp_path: Path,
) -> None:
    """Ambiguous name visible via more than one use yields a multi-match list."""
    pa = tmp_path / "phys_a.f90"
    pa.write_text(
        "module phys_a\n"
        "  real :: pte\n"
        "end module\n"
    )
    pb = tmp_path / "phys_b.f90"
    pb.write_text(
        "module phys_b\n"
        "  real :: pte\n"
        "end module\n"
    )
    drv = tmp_path / "drv.f90"
    drv.write_text(
        "subroutine drv\n"
        "  use phys_a\n"
        "  use phys_b\n"
        "  real :: x\n"
        "  x = pte\n"          # line 5
        "end subroutine\n"
    )
    _drive([pa, pb, drv])
    uri = drv.resolve().as_uri()
    # Cursor on ``pte`` at line 5, column 7 (0-based).
    locs = _goto(uri, line_0=4, col_0=7)
    # Both phys_a and phys_b declare pte and both are used by drv —
    # both should surface.
    assert locs is not None
    assert len(locs) == 2


def test_goto_def_unrelated_module_filtered_out(tmp_path: Path) -> None:
    """A same-named declaration in an unused module is filtered out."""
    used = tmp_path / "used.f90"
    used.write_text(
        "module used_mod\n"
        "  real :: pte\n"
        "end module\n"
    )
    unused = tmp_path / "unused.f90"
    unused.write_text(
        "module unused_mod\n"
        "  real :: pte\n"
        "end module\n"
    )
    drv = tmp_path / "drv.f90"
    drv.write_text(
        "subroutine drv\n"
        "  use used_mod\n"
        "  real :: x\n"
        "  x = pte\n"          # line 4
        "end subroutine\n"
    )
    _drive([used, unused, drv])
    uri = drv.resolve().as_uri()
    locs = _goto(uri, line_0=3, col_0=7)
    assert locs is not None
    assert len(locs) == 1
    assert locs[0].uri.endswith("used.f90")
