"""Tests for the workspace-aware module index + resolver."""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from dimfort.core.workspace_index import (
    UseRef,
    extract_calls,
    extract_modules,
    extract_top_level_procedures,
    extract_uses,
    resolve_workset,
    scan_workspace,
    update_index,
)

# ---------------------------------------------------------------------------
# Source extraction
# ---------------------------------------------------------------------------


class TestExtractModules:
    def test_single_module(self):
        src = "module foo\nend module foo\n"
        assert extract_modules(src) == ("foo",)

    def test_module_procedure_is_not_a_declaration(self):
        # `module procedure …` inside an interface block is NOT a new
        # module — the regex must exclude it.
        src = dedent("""
            module foo
              interface bar
                module procedure baz
              end interface bar
            end module foo
        """)
        assert extract_modules(src) == ("foo",)

    def test_multiple_modules_in_one_file(self):
        src = "module a\nend module a\nmodule b\nend module b\n"
        assert extract_modules(src) == ("a", "b")

    def test_case_insensitive_lowercased_output(self):
        assert extract_modules("MODULE Foo\nend module Foo") == ("foo",)


class TestExtractUses:
    def test_plain_use(self):
        assert extract_uses("use comvert_mod") == (
            UseRef("comvert_mod", None, ()),
        )

    def test_use_with_only(self):
        u = extract_uses("use comvert_mod, only: bp, ap")[0]
        assert u.module == "comvert_mod"
        assert u.only == ("bp", "ap")
        assert u.renames == ()

    def test_use_with_rename(self):
        u = extract_uses("use M, only: local => remote, plain")[0]
        assert u.module == "m"
        assert u.only == ("local", "plain")
        assert u.renames == (("local", "remote"),)

    def test_use_intrinsic_modifier(self):
        u = extract_uses("use, intrinsic :: iso_fortran_env")[0]
        assert u.module == "iso_fortran_env"

    def test_use_double_colon(self):
        u = extract_uses("use :: M, only: x")[0]
        assert u.module == "m"
        assert u.only == ("x",)

    def test_use_in_comment_ignored(self):
        # `!` inside a string is not a comment marker (string-aware
        # comment stripper); but `use` *inside* a comment is.
        src = "use real_module\n! use fake_module"
        assert extract_uses(src) == (
            UseRef("real_module", None, ()),
        )

    def test_use_continued_across_lines(self):
        src = "use M, only: a, &\n        b, c"
        u = extract_uses(src)[0]
        assert u.only == ("a", "b", "c")


# ---------------------------------------------------------------------------
# Workspace scan + index update
# ---------------------------------------------------------------------------


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dedent(body))
    return p


def test_scan_workspace_indexes_module_files(tmp_path):
    _write(tmp_path, "m_one.f90", "module one\nend module one")
    _write(tmp_path, "m_two.f90", "module two\nuse one\nend module two")
    idx = scan_workspace([tmp_path])
    assert set(idx.modules) == {"one", "two"}
    assert idx.modules["one"].name == "m_one.f90"
    # Each scanned file gets a uses entry (possibly empty).
    assert len(idx.uses_by_file) == 2
    uses_of_two = next(u for p, u in idx.uses_by_file.items() if p.name == "m_two.f90")
    assert uses_of_two == (UseRef("one", None, ()),)


def test_scan_workspace_respects_include_suffixes(tmp_path):
    _write(tmp_path, "ok.f90", "module ok\nend module ok")
    _write(tmp_path, "skipme.txt", "module txt\nend module txt")
    idx = scan_workspace([tmp_path])
    assert set(idx.modules) == {"ok"}


def test_scan_workspace_excludes_patterns(tmp_path):
    _write(tmp_path, "src/m_keep.f90", "module keep\nend module keep")
    _write(tmp_path, "build/m_drop.f90", "module drop\nend module drop")
    idx = scan_workspace([tmp_path], exclude_patterns=("build/*",))
    assert set(idx.modules) == {"keep"}


def test_scan_workspace_walks_subdirs(tmp_path):
    _write(tmp_path, "a/m_a.f90", "module a\nend module a")
    _write(tmp_path, "b/c/m_c.f90", "module c\nend module c")
    idx = scan_workspace([tmp_path])
    assert set(idx.modules) == {"a", "c"}


def test_scan_handles_latin1_encoded_sources(tmp_path):
    """Many legacy Fortran codebases ship files with non-UTF-8 byte
    sequences in comments. The scanner must not crash."""
    p = tmp_path / "latin.f90"
    # `é` (0xe9) in Latin-1 — not valid UTF-8 as a standalone byte.
    p.write_bytes(b"module foo\n! commentaire en fran\xe9ais\nend module foo\n")
    idx = scan_workspace([tmp_path])
    assert set(idx.modules) == {"foo"}
    assert idx.scan_failures == {}


def test_update_index_replaces_previous_scan_for_one_file(tmp_path):
    p = _write(tmp_path, "m.f90", "module old\nend module old")
    idx = scan_workspace([tmp_path])
    assert "old" in idx.modules
    # Rewrite the file with a different module name and an in-memory
    # buffer (LSP didChange case — disk and buffer can disagree).
    update_index(idx, p, new_text="module fresh\nend module fresh\n")
    assert "old" not in idx.modules
    assert "fresh" in idx.modules


# ---------------------------------------------------------------------------
# Workset resolution
# ---------------------------------------------------------------------------


def test_resolve_includes_dependencies_topo_order(tmp_path):
    a = _write(tmp_path, "a.f90", "module a\nend module a")
    b = _write(tmp_path, "b.f90", "module b\nuse a\nend module b")
    c = _write(tmp_path, "c.f90", "module c\nuse b\nend module c")
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [c])
    assert res.compile_order == (a.resolve(), b.resolve(), c.resolve())
    assert res.unresolved == ()


def test_resolve_unresolved_module_recorded(tmp_path):
    f = _write(tmp_path, "f.f90", "module f\nuse missing\nend module f")
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [f])
    assert res.unresolved == ((f.resolve(), "missing"),)
    # Compile order still contains f (we can try to compile it; LFortran
    # will complain at that point — that's the right place).
    assert f.resolve() in res.compile_order


def test_resolve_external_modules_silently_dropped(tmp_path):
    f = _write(tmp_path, "f.f90", "module f\nuse ioipsl\nend module f")
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [f], external_modules=frozenset({"ioipsl"}))
    assert res.unresolved == ()
    assert "ioipsl" in res.external


def test_resolve_handles_cycles_without_infinite_loop(tmp_path):
    _write(tmp_path, "x.f90", "module x\nuse y\nend module x")
    y = _write(tmp_path, "y.f90", "module y\nuse x\nend module y")
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [y])
    # Both files appear; their relative order in a cycle isn't defined
    # but the resolver must terminate.
    assert {p.name for p in res.compile_order} == {"x.f90", "y.f90"}


def test_resolve_module_using_itself_is_not_a_cycle(tmp_path):
    # Rare but legal — a `use` inside a module that names itself
    # (e.g. for namespacing). Shouldn't be treated as a dependency.
    f = _write(tmp_path, "f.f90", "module f\nuse f, only: x\nend module f")
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [f])
    assert res.compile_order == (f.resolve(),)


# ---------------------------------------------------------------------------
# Top-level procedure extraction + external-procedure resolution (F77 idiom)
# ---------------------------------------------------------------------------


class TestExtractTopLevelProcedures:
    def test_top_level_subroutine(self):
        src = "subroutine foo()\nend subroutine\n"
        assert extract_top_level_procedures(src) == ("foo",)

    def test_module_contained_subroutine_is_NOT_top_level(self):
        # `contains`-style routines belong to the module; the
        # workspace index reaches them via the module export path.
        src = dedent("""
            module m
            contains
              subroutine inner()
              end subroutine
            end module m
        """)
        assert extract_top_level_procedures(src) == ()

    def test_function_with_type_prefix(self):
        src = "real function compute(x)\nend function\n"
        assert extract_top_level_procedures(src) == ("compute",)

    def test_subroutine_after_module_block(self):
        src = dedent("""
            module m
            contains
              subroutine inner()
              end subroutine
            end module m

            subroutine outer()
            end subroutine
        """)
        assert extract_top_level_procedures(src) == ("outer",)

    def test_case_insensitive_lowercased_output(self):
        src = "SUBROUTINE FLUMASS(a, b)\nend subroutine FLUMASS\n"
        assert extract_top_level_procedures(src) == ("flumass",)

    def test_subroutine_in_comment_ignored(self):
        src = "! subroutine commented_out()\nend subroutine\n"
        assert extract_top_level_procedures(src) == ()


class TestExtractCalls:
    def test_simple_call(self):
        src = "subroutine s\n  call foo(x)\nend subroutine\n"
        assert extract_calls(src) == ("foo",)

    def test_deduplicates_repeated_calls(self):
        src = dedent("""
            subroutine s
              call foo(x)
              call bar(y)
              call foo(z)
            end subroutine
        """)
        assert extract_calls(src) == ("foo", "bar")

    def test_case_insensitive(self):
        src = "subroutine s\n  CALL Foo(x)\nend subroutine\n"
        assert extract_calls(src) == ("foo",)

    def test_call_in_comment_ignored(self):
        src = "subroutine s\n  ! call foo(x)\n  call bar(y)\nend subroutine\n"
        assert extract_calls(src) == ("bar",)


def test_workspace_index_records_top_level_procedure(tmp_path):
    # External procedure: a top-level subroutine, no enclosing MODULE.
    _write(
        tmp_path, "flumass.f90",
        "subroutine flumass(a, b)\nend subroutine flumass\n",
    )
    # Module-contained procedure: should NOT appear in `procedures`.
    _write(
        tmp_path, "inner_mod.f90",
        "module inner_mod\ncontains\nsubroutine helper()\nend subroutine\nend module inner_mod\n",
    )
    idx = scan_workspace([tmp_path])
    assert "flumass" in idx.procedures
    assert idx.procedures["flumass"].name == "flumass.f90"
    assert "helper" not in idx.procedures


def test_resolve_pulls_in_external_procedure_callee(tmp_path):
    # Caller in dyn3d/, callee (external SUBROUTINE) in dyn3d_common/.
    # No USE statement between them — this is the F77-vintage pattern.
    callee = _write(
        tmp_path, "dyn3d_common/flumass.f90",
        "subroutine flumass(a, b)\nend subroutine flumass\n",
    )
    caller = _write(
        tmp_path, "dyn3d/caldyn.f90",
        "subroutine caldyn\n  call flumass(x, y)\nend subroutine\n",
    )
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [caller])
    # Callee file must appear in the workset so signatures collect and
    # goto-def can find the definition.
    assert callee.resolve() in res.compile_order
    assert caller.resolve() in res.compile_order


def test_resolve_does_not_pull_in_module_contained_procedure(tmp_path):
    # ``foo`` is defined inside a module — workset-resolution should NOT
    # treat a CALL to it as a procedure-index hit. The module gets
    # pulled in only if the caller does ``use``.
    _write(
        tmp_path, "m.f90",
        "module m\ncontains\nsubroutine foo()\nend subroutine\nend module m\n",
    )
    caller = _write(
        tmp_path, "caller.f90",
        "subroutine c\n  call foo(x)\nend subroutine\n",
    )
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [caller])
    # m.f90 is NOT in the workset — no use-chain reaches it, and
    # `foo` doesn't appear in idx.procedures.
    assert {p.name for p in res.compile_order} == {"caller.f90"}


def test_resolve_external_procedure_chain(tmp_path):
    # caller -> external mid -> external leaf. Both externals pulled
    # transitively via successive CALL discoveries.
    leaf = _write(
        tmp_path, "leaf.f90",
        "subroutine leaf()\nend subroutine\n",
    )
    mid = _write(
        tmp_path, "mid.f90",
        "subroutine mid()\n  call leaf()\nend subroutine\n",
    )
    caller = _write(
        tmp_path, "caller.f90",
        "subroutine c\n  call mid()\nend subroutine\n",
    )
    idx = scan_workspace([tmp_path])
    res = resolve_workset(idx, [caller])
    assert {p.resolve() for p in res.compile_order} == {
        leaf.resolve(), mid.resolve(), caller.resolve(),
    }


def test_update_index_drops_old_procedure_entries(tmp_path):
    f = _write(
        tmp_path, "p.f90",
        "subroutine old_name()\nend subroutine\n",
    )
    idx = scan_workspace([tmp_path])
    assert "old_name" in idx.procedures
    # Rename the procedure in the file; rescan.
    from dimfort.core.workspace_index import update_index as _update
    f.write_text("subroutine new_name()\nend subroutine\n")
    _update(idx, f)
    assert "old_name" not in idx.procedures
    assert "new_name" in idx.procedures
