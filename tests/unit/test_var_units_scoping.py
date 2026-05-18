"""Per-subroutine scoping of ``@unit{...}`` annotations.

When two routines in one file declare same-named arguments with
different units, attach must key them by ``(scope, name)`` so they
don't alias. U-conflict still fires for genuine intra-scope
re-declarations.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core import unit_config  # noqa: F401  — installs DEFAULT_TABLE
from dimfort.core.annotations import scan_text
from dimfort.core.attach import attach
from dimfort.core.multifile import check_files


def _attach(src: str):
    return attach(scan_text(src))


def test_attach_two_subroutines_same_name_different_units():
    """No cross-routine collision; both annotations kept independently."""
    src = (
        "subroutine foo(pte)\n"
        "  real :: pte  !< @unit{m/s}\n"
        "end subroutine\n"
        "subroutine bar(pte)\n"
        "  real :: pte  !< @unit{K/s}\n"
        "end subroutine\n"
    )
    res = _attach(src)
    assert res.conflicts == []
    assert res.var_units_by_scope[("foo", "pte")] == "m/s"
    assert res.var_units_by_scope[("bar", "pte")] == "K/s"
    # Flat view keeps first-seen-wins for back-compat callers.
    assert res.var_units["pte"] == "m/s"


def test_attach_intra_scope_conflict_still_fires():
    """Same scope re-declaring ``x`` with two units is a real U-conflict."""
    src = (
        "subroutine foo\n"
        "  real :: x  !< @unit{m/s}\n"
        "  real :: x  !< @unit{K}\n"
        "end subroutine\n"
    )
    res = _attach(src)
    assert len(res.conflicts) == 1
    c = res.conflicts[0]
    assert c.variable == "x"
    assert c.first_unit == "m/s"
    assert c.second_unit == "K"


def test_attach_module_local_shadows_inside_routine():
    """Module-level ``x: K`` + subroutine-local ``x: m/s`` → no conflict;
    the per-scope table records both so the resolver can pick the
    routine's annotation from inside it.
    """
    src = (
        "module m\n"
        "  real :: x  !< @unit{K}\n"
        "contains\n"
        "  subroutine foo\n"
        "    real :: x  !< @unit{m/s}\n"
        "  end subroutine\n"
        "end module\n"
    )
    res = _attach(src)
    assert res.conflicts == []
    assert res.var_units_by_scope[(None, "x")] == "K"
    assert res.var_units_by_scope[("foo", "x")] == "m/s"


def test_checker_signatures_use_per_scope_units(tmp_path: Path):
    """Two subroutines, same arg name, different units → each callee
    signature reflects its OWN annotation. No spurious H004 on the
    correctly-typed call.
    """
    src = (
        "subroutine emit_m(pte)\n"
        "  real :: pte  !< @unit{m/s}\n"
        "end subroutine\n"
        "subroutine emit_k(pte)\n"
        "  real :: pte  !< @unit{K/s}\n"
        "end subroutine\n"
        "subroutine driver\n"
        "  real :: a  !< @unit{m/s}\n"
        "  real :: b  !< @unit{K/s}\n"
        "  call emit_m(a)\n"
        "  call emit_k(b)\n"
        "end subroutine\n"
    )
    f = tmp_path / "scoping.f90"
    f.write_text(src)
    result = check_files([f])
    diags = result.diagnostics[f]
    # No U-conflict across routines, no H004 on the correctly-typed calls.
    codes = [d.code for d in diags]
    assert "U-conflict" not in codes, codes
    assert "H004" not in codes, [(d.code, d.message) for d in diags]
    # Both signatures collected with their local units.
    sigs = result.signatures
    assert sigs["emit_m"].arg_units[0] is not None
    assert sigs["emit_k"].arg_units[0] is not None
    assert sigs["emit_m"].arg_units[0] != sigs["emit_k"].arg_units[0]


def test_checker_h004_fires_when_call_actually_mismatches(tmp_path: Path):
    """Sanity: scoping doesn't disable H004 — passing a K/s value to
    a routine whose param is m/s still trips it.
    """
    src = (
        "subroutine emit_m(pte)\n"
        "  real :: pte  !< @unit{m/s}\n"
        "end subroutine\n"
        "subroutine emit_k(pte)\n"
        "  real :: pte  !< @unit{K/s}\n"
        "end subroutine\n"
        "subroutine driver\n"
        "  real :: b  !< @unit{K/s}\n"
        "  call emit_m(b)\n"
        "end subroutine\n"
    )
    f = tmp_path / "scoping_h004.f90"
    f.write_text(src)
    result = check_files([f])
    codes = [d.code for d in result.diagnostics[f]]
    assert "H004" in codes, codes
