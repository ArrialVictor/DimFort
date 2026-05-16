"""Unit tests for the tree-sitter Fortran wrapper.

Pure-Python, no LFortran dependency. The tests below split into four
groups; the docstrings explain why each group exists.
"""
from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from dimfort.core import ts_parser as ts


# ---------------------------------------------------------------------------
# Group 1 — Basic parse + walk
# Smoke tests for each public entry point. They look redundant but each
# exercises a different input shape (str / bytes / file path); deleting
# any of them removes coverage for that shape.

def test_parse_text_returns_tree():
    """``parse_text`` accepts ``str`` and returns a clean ``translation_unit`` tree."""
    tree = ts.parse_text("module m\nend module\n")
    assert tree.root_node.type == "translation_unit"
    assert not tree.root_node.has_error


def test_parse_text_accepts_bytes():
    # The wrapper should accept raw file bytes without a str round-trip,
    # because callers reading files want to avoid the decode.
    tree = ts.parse_text(b"subroutine s\nend subroutine\n")
    assert not tree.root_node.has_error


def test_parse_file(tmp_path: Path):
    """``parse_file`` accepts a ``Path`` and reads the source itself."""
    f = tmp_path / "x.f90"
    f.write_text("program p\nend program\n")
    tree = ts.parse_file(f)
    assert tree.root_node.type == "translation_unit"


def test_walk_is_preorder():
    # `walk` must yield the root first, then descend depth-first.
    # The rest of the codebase (and the LFortran walker we're replacing)
    # relies on this ordering.
    tree = ts.parse_text("module m\n  integer :: x\nend module\n")
    types = [n.type for n in ts.walk(tree.root_node)]
    assert types[0] == "translation_unit"
    assert "module" in types
    assert "variable_declaration" in types
    # `module` appears before `variable_declaration` (parent before child)
    assert types.index("module") < types.index("variable_declaration")


# ---------------------------------------------------------------------------
# Group 2 — Position conversion
# tree-sitter exposes 0-based (row, col). DimFort uses 1-based everywhere
# (LSP convention, editor convention, compiler-diagnostic convention).
# `position_for` is the single point of conversion; if it ever returns
# 0-based by accident, every downstream diagnostic gets the wrong line.

def test_position_for_is_one_based_at_origin():
    # The corner case: a node starting at (row=0, col=0) must come out
    # as (line=1, column=1), not (0, 0).
    tree = ts.parse_text("module m\nend module\n")
    module_node = next(n for n in ts.walk(tree.root_node) if n.type == "module")
    pos = ts.position_for(module_node)
    assert pos.line == 1
    assert pos.column == 1


def test_position_for_offset_indentation():
    # Non-origin case: a decl on line 2, indented by 2 spaces, must
    # come out as (line=2, column=3) — the column counts the indent.
    tree = ts.parse_text("module m\n  integer :: x\nend module\n")
    decl = next(n for n in ts.walk(tree.root_node) if n.type == "variable_declaration")
    pos = ts.position_for(decl)
    assert pos.line == 2
    assert pos.column == 3


def test_end_position_exclusive_one_based():
    # `end_position_for` reports the position *just past* the last
    # character of the node. For ``module m\nend module\n``, the
    # `module` node ends after ``end module`` on line 2, column 11
    # (1-based: ``end module`` is 10 chars + 1).
    tree = ts.parse_text("module m\nend module\n")
    module_node = next(n for n in ts.walk(tree.root_node) if n.type == "module")
    end = ts.end_position_for(module_node)
    # `module m\nend module\n` — the module node includes the trailing
    # newline, so the end position is the start of line 3, column 1.
    assert end.line == 3
    assert end.column == 1


# ---------------------------------------------------------------------------
# Group 3 — Comments and &-continuation positions
# This is the headline reason for switching parsers. LFortran 0.63
# collapses each &-continued statement to 2 reported lines, and the
# drift accumulates across the file. We rely on tree-sitter's positions
# being byte-exact. If any of these regress, the whole annotation-
# attachment story stops working.

def test_comments_are_first_class_nodes_with_positions():
    # PRE block comment on line 3 and trailing POST comment on line 4
    # must both appear as `comment` nodes with their correct lines.
    src = textwrap.dedent("""\
        module m
          implicit none
          !> doc above
          real :: x !< trailing
        end module
    """)
    tree = ts.parse_text(src)
    comments = [n for n in ts.walk(tree.root_node) if n.type == "comment"]
    lines = sorted(ts.position_for(c).line for c in comments)
    assert lines == [3, 4]


def test_continuation_decl_spans_correct_rows():
    # The case LFortran fails at. `real :: a, & \n b, c` must produce
    # a single variable_declaration whose start is on the first line
    # and whose end is on the continued line — without drifting any
    # subsequent declaration's line number.
    src = "module m\n  real :: a, & !< inline\n          b, c\nend module\n"
    tree = ts.parse_text(src)
    decl = next(n for n in ts.walk(tree.root_node) if n.type == "variable_declaration")
    start = ts.position_for(decl)
    end = ts.end_position_for(decl)
    assert start.line == 2
    assert end.line == 3


def test_continuation_comments_keep_their_physical_lines():
    # When two `!<` comments appear on different physical lines of
    # the same continued declaration, each must keep its own line.
    # This is what lets the attach stage tell "annotation on the
    # first line" from "annotation on the last line".
    src = "module m\n  real :: a, & !< inline\n          b, c  !< last\nend module\n"
    tree = ts.parse_text(src)
    comments_by_line = {
        ts.position_for(c).line: ts.node_text(c, src.encode())
        for c in ts.walk(tree.root_node) if c.type == "comment"
    }
    assert "inline" in comments_by_line[2]
    assert "last" in comments_by_line[3]


# ---------------------------------------------------------------------------
# Group 4 — Error tolerance
# tree-sitter's value proposition over LFortran: a syntax error
# anywhere in the file does not abort parsing of the rest. The error
# is localised to an ERROR node and the surrounding tree is preserved.
# DimFort needs this because real-world Fortran (especially F77-in-F90)
# is full of constructs LFortran rejects outright.

def test_clean_source_reports_no_error():
    """On a well-formed file, ``has_error`` is ``False`` and ``error_nodes`` is empty."""
    tree = ts.parse_text("module m\nend module\n")
    assert not ts.has_error(tree)
    assert list(ts.error_nodes(tree)) == []


def test_broken_section_does_not_abort_rest_of_file():
    # Half-finished declaration in the middle. tree-sitter should
    # report has_error=True and localise the failure — the subroutine
    # statement itself and the `end subroutine` must still appear in
    # the tree (not collapsed into one giant ERROR span).
    src = "subroutine s\n  integer :: \n  integer :: x\nend subroutine\n"
    tree = ts.parse_text(src)
    assert ts.has_error(tree)
    assert any(e for e in ts.error_nodes(tree))
    types = {n.type for n in ts.walk(tree.root_node)}
    assert "subroutine_statement" in types
    assert "end_subroutine_statement" in types


def test_node_text_decodes_utf8():
    # LMDZ source has French comments. The wrapper must decode them
    # without raising on multi-byte characters.
    src = "! French: éclair\nmodule m\nend module\n".encode("utf-8")
    tree = ts.parse_text(src)
    comments = [n for n in ts.walk(tree.root_node) if n.type == "comment"]
    assert "éclair" in ts.node_text(comments[0], src)


# ---------------------------------------------------------------------------
# Group 5 — CPP shim
# tree-sitter has no built-in CPP. The shim runs the system `cpp` and
# parses the expanded text. The line-map exists to remap positions in
# the expanded tree back to the original source — without it,
# diagnostics on a file with #ifdef blocks would point to wrong lines.
#
# Skipped on platforms without `cpp` (CI without a C toolchain).

CPP_AVAILABLE = shutil.which("cpp") is not None


@pytest.mark.skipif(not CPP_AVAILABLE, reason="system cpp not available")
class TestCppShim:

    def test_active_branch_yields_both_decls(self, tmp_path: Path):
        # With -DISO defined, the #ifdef ISO block is included, so
        # both `integer :: a` and `integer :: b` appear in the tree.
        f = tmp_path / "x.F90"
        f.write_text(textwrap.dedent("""\
            subroutine s
              integer :: a
            #ifdef ISO
              integer :: b
            #endif
            end subroutine
        """))
        pre = ts.parse_with_cpp(f, defines=["ISO"])
        decls = [n for n in ts.walk(pre.tree.root_node)
                 if n.type == "variable_declaration"]
        assert len(decls) == 2

    def test_inactive_branch_is_excluded(self, tmp_path: Path):
        # Without -DISO, the #ifdef block is removed; only `a` remains.
        f = tmp_path / "x.F90"
        f.write_text(textwrap.dedent("""\
            subroutine s
              integer :: a
            #ifdef ISO
              integer :: b
            #endif
            end subroutine
        """))
        pre = ts.parse_with_cpp(f, defines=[])
        decls = [n for n in ts.walk(pre.tree.root_node)
                 if n.type == "variable_declaration"]
        assert len(decls) == 1

    def test_include_path_resolves_referenced_header(self, tmp_path: Path):
        # #include "stub.h" must pull in declarations from the stub
        # when its directory is on `include_paths`. This is the
        # mechanism LMDZ uses for `.intfb.h` shims.
        stubs = tmp_path / "stubs"
        stubs.mkdir()
        (stubs / "stub.h").write_text("integer :: from_stub\n")
        f = tmp_path / "x.F90"
        f.write_text("subroutine s\n#include \"stub.h\"\nend subroutine\n")
        pre = ts.parse_with_cpp(f, include_paths=[stubs])
        decls = [n for n in ts.walk(pre.tree.root_node)
                 if n.type == "variable_declaration"]
        assert any("from_stub" in ts.node_text(d, pre.expanded_text)
                   for d in decls)

    def test_missing_include_raises_cpp_failed(self, tmp_path: Path):
        # When an #include can't be resolved, the shim must surface
        # the cpp failure as a typed exception so the caller can
        # downgrade to U007 rather than crashing.
        f = tmp_path / "x.F90"
        f.write_text("#include \"nonexistent.h\"\n")
        with pytest.raises(ts.CppFailedError):
            ts.parse_with_cpp(f)

    def test_line_map_back_to_source(self, tmp_path: Path):
        # After cpp expansion, an expanded-line number must map back
        # to the original source-line number. Here `integer :: a` is
        # on source line 2; after cpp (`-P` keeps blank lines from
        # markers, so the expanded layout may shift slightly) the map
        # must still recover the source line of 2.
        f = tmp_path / "x.F90"
        f.write_text(textwrap.dedent("""\
            subroutine s
              integer :: a
              integer :: b
            end subroutine
        """))
        pre = ts.parse_with_cpp(f)
        decl = next(n for n in ts.walk(pre.tree.root_node)
                    if n.type == "variable_declaration")
        expanded_line = ts.position_for(decl).line
        assert pre.source_line(expanded_line) == 2

    def test_line_map_survives_ifdef_drop(self, tmp_path: Path):
        # Regression for the drift bug: when ``#ifdef X`` removes
        # lines, the line_map must keep node positions pointing at
        # the *source* line of the surviving content, not the
        # post-stripping line. Here ``integer :: keep`` is on source
        # line 5; after preprocessing strips the ``#ifdef OFF`` block
        # (lines 2-4), it appears on expanded line 1. The map must
        # recover source line 5.
        f = tmp_path / "x.F90"
        f.write_text(textwrap.dedent("""\
            subroutine s
            #ifdef OFF
              integer :: dropped
            #endif
              integer :: keep
            end subroutine
        """))
        pre = ts.parse_with_cpp(f)
        decl = next(
            n for n in ts.walk(pre.tree.root_node)
            if n.type == "variable_declaration"
        )
        expanded_line = ts.position_for(decl).line
        # `keep` is on source line 5 of the original file
        assert pre.source_line(expanded_line) == 5
