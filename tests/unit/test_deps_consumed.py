"""Tests for per-file deps_consumed recording (cache step 5)."""
from __future__ import annotations

from pathlib import Path

from dimfort.core.multifile import check_files
from dimfort.core.symbols import deps_consumed_from_uses
from dimfort.core.workspace_index import UseRef


def test_helper_excludes_unresolved_and_external():
    uses = (
        UseRef(module="a_mod", only=None, renames=()),
        UseRef(module="b_mod", only=None, renames=()),
        UseRef(module="nc", only=None, renames=()),
        UseRef(module="missing_mod", only=None, renames=()),
    )
    deps = deps_consumed_from_uses(
        uses,
        unresolved=frozenset({"missing_mod"}),
        external_modules=frozenset({"nc"}),
    )
    assert deps == frozenset({"a_mod", "b_mod"})


def test_helper_normalises_module_name_case():
    uses = (UseRef(module="MyMod", only=None, renames=()),)
    deps = deps_consumed_from_uses(uses, frozenset(), frozenset())
    assert deps == frozenset({"mymod"})


def test_helper_empty_when_no_uses():
    deps = deps_consumed_from_uses((), frozenset(), frozenset())
    assert deps == frozenset()


def test_check_files_populates_deps_consumed(tmp_path: Path):
    # Two-file workspace: consumer uses producer.
    producer = tmp_path / "producer.f90"
    producer.write_text(
        "module producer_mod\n"
        "  real :: x  ! @unit{m/s}\n"
        "end module\n"
    )
    consumer = tmp_path / "consumer.f90"
    consumer.write_text(
        "subroutine s\n"
        "  use producer_mod\n"
        "  real :: y  ! @unit{m/s}\n"
        "  y = x\n"
        "end subroutine\n"
    )
    result = check_files([producer, consumer])
    # Producer has no `use` clauses → empty set.
    assert result.deps_consumed[producer] == frozenset()
    # Consumer consumed producer_mod.
    assert result.deps_consumed[consumer] == frozenset({"producer_mod"})


def test_check_files_excludes_unresolved_use(tmp_path: Path):
    consumer = tmp_path / "consumer.f90"
    consumer.write_text(
        "subroutine s\n"
        "  use ghost_mod\n"
        "  real :: y\n"
        "end subroutine\n"
    )
    result = check_files([consumer])
    # ghost_mod is unresolved → not in deps_consumed.
    assert result.deps_consumed[consumer] == frozenset()


def test_check_files_excludes_external_modules(tmp_path: Path):
    consumer = tmp_path / "consumer.f90"
    consumer.write_text(
        "subroutine s\n"
        "  use netcdf\n"
        "  real :: y\n"
        "end subroutine\n"
    )
    result = check_files([consumer], external_modules=frozenset({"netcdf"}))
    assert result.deps_consumed[consumer] == frozenset()
