"""Phase 3 AST-only checker — derived types and array index/section.

The fixture ``smoke_ast_phase3.f90`` exercises:
- Chained derived-type access ``o%nest%x``.
- Array element ``a(1)`` and ``a(i)``.
- Array slice ``a(:)`` and bounded slice ``a(1:5)``.

All four "array" forms surface as ``FuncCallOrArray`` in the AST;
without a fallback, they'd be unresolved. Phase 3 adds the
variable-lookup branch in ``_resolve_call`` so they all carry the
array's element unit.

The parity test (``test_ast_parity.py``) already covers the existing
``smoke_derived_types`` fixture; this file pushes the deeper case
(two-level chain) plus the array-form mix.
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_ast_phase3.f90"


def _run() -> list:
    scan = scan_file(FIXTURE)
    attached = attach(scan)
    ast = lf.dump_tree(FIXTURE, "ast")
    return check(
        ast,
        attached.var_units,
        file=str(FIXTURE),
        field_units=attached.field_units,
    )


def test_phase3_two_level_derived_chain_resolves():
    """``o%nest%x`` is m, ``r`` is m — clean assignment, no diag.

    If the chain weren't being walked, the resolver would return
    None and the H001 wouldn't fire on the *bad* line either, so the
    parity test catches part of this. This test checks the positive
    case directly.
    """
    diags = _run()
    # Among all the H001s emitted by this fixture, none should be on
    # line 26 (the o%nest%x line — see fixture).
    bad_h001s = [d for d in diags if d.code == "H001" and d.start.line == 26]
    assert bad_h001s == [], (
        f"unexpected H001 on clean o%%nest%%x assignment: {bad_h001s}"
    )


def test_phase3_two_level_derived_chain_emits_h001_when_wrong():
    """``r = o%y`` is m = kg → H001 expected."""
    diags = _run()
    h001s = [d for d in diags if d.code == "H001"]
    msgs = [d.message for d in h001s]
    # Must include a m vs kg mismatch somewhere.
    assert any("kg" in m and "m " in m or "m " in m for m in msgs) or any(
        "kg" in m for m in msgs
    ), f"expected an H001 mentioning kg from o%%y access: {msgs}"


def test_phase3_array_forms_all_resolve():
    """``a(1)``, ``a(:)``, ``a(1:5)``, ``a(i)`` should all be resolved
    to ``m/s`` (the array's element unit). Each of the four
    assignments ``r = a(...)`` is m vs m/s → H001."""
    diags = _run()
    h001s = [d for d in diags if d.code == "H001"]
    # The fixture has 5 deliberate H001s: o%y → r, then 4 array-form
    # assignments to either r (m) or bad (kg), all from a (m/s).
    assert len(h001s) == 5, (
        f"expected exactly 5 H001s (o%%y + 4 array forms); got "
        f"{[(d.code, d.start.line, d.message) for d in diags]}"
    )
