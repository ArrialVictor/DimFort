"""Phase 1 AST-only checker — H002 / H003 / H004 / Pow / intrinsics.

End-to-end on ``tests/fixtures/smoke_ast_phase1.f90``. The fixture
intentionally exercises:

- A user function ``squared(side: m) -> m^2`` with ``side ** 2``.
- A user subroutine ``bump(x: m, factor: 1)``.
- A correct call ``area = squared(len)`` (no diag).
- A bad call ``call bump(kg_x, dim_x)`` -> H004 (arg 1 unit mismatch).
- A bad add ``len + kg_x`` -> H002.
- A bad intrinsic call ``sin(len)`` -> H003.
- A clean ``len = sqrt(area)`` (sqrt transforms m^2 -> m).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401
from dimfort.core.annotations import scan_file
from dimfort.core.ast_checker import check
from dimfort.core.attach import attach


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_ast_phase1.f90"


def _run() -> list:
    scan = scan_file(FIXTURE)
    attached = attach(scan)
    ast = lf.dump_tree(FIXTURE, "ast")
    return check(ast, attached.var_units, file=str(FIXTURE))


def test_phase1_emits_exactly_h002_h003_h004():
    diags = _run()
    codes = sorted(d.code for d in diags)
    # Three expected mismatches; nothing else should fire.
    assert codes == ["H002", "H003", "H004"], (
        f"unexpected diagnostic set: {[(d.code, d.message) for d in diags]}"
    )


def test_phase1_h004_names_the_offending_function():
    diags = _run()
    h004 = next(d for d in _run() if d.code == "H004")
    assert "bump" in h004.message
    # arg 1 is the unit-mismatched one (kg_x vs expected m).
    assert "argument 1" in h004.message


def test_phase1_h003_names_the_intrinsic():
    h003 = next(d for d in _run() if d.code == "H003")
    assert "sin" in h003.message


def test_phase1_sqrt_resolves_cleanly():
    """`len = sqrt(area)` is m = sqrt(m^2) = m — no H001 expected."""
    diags = _run()
    h001s = [d for d in diags if d.code == "H001"]
    assert h001s == [], f"sqrt should be transparent to unit checks: {h001s}"


def test_phase1_pow_resolves_cleanly():
    """``squared`` body assigns ``side ** 2`` to ``out (m^2)`` — should
    produce no diagnostic on that assignment."""
    diags = _run()
    # If Pow weren't resolved, the H001 on the squared() body would fire
    # because side**2 would be unknown and the LHS would be m^2.
    # We expect exactly the three known mismatches; nothing else.
    codes = [d.code for d in diags]
    assert codes.count("H001") == 0
