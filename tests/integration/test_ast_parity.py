"""Parity tests: AST-only checker must produce the same diagnostic
codes as the ASR-driven checker on the existing test fixtures.

This is the regression net for the AST-only branch: if it starts to
drift from the ASR results on shared fixtures, something in the new
resolver regressed. We compare *code sets* rather than full Diagnostic
objects — the AST checker's source positions live in the AST
coordinate system, which may differ in column from the ASR positions,
and we don't want to test that here.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401
from dimfort.core import ast_checker
from dimfort.core import checker as asr_checker
from dimfort.core.annotations import scan_file
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


FIXTURES_DIR = Path(__file__).parents[1] / "fixtures"


def _diag_counts(diags) -> Counter:
    return Counter(d.code for d in diags)


def _run_both(fixture: Path) -> tuple[Counter, Counter]:
    scan = scan_file(fixture)
    attached = attach(scan)
    ast = lf.dump_tree(fixture, "ast")
    asr = lf.dump_tree(fixture, "asr")

    ast_diags = ast_checker.check(ast, attached.var_units, file=str(fixture))
    asr_diags = asr_checker.check(
        asr, attached.var_units, file=str(fixture), ast=ast
    )
    return _diag_counts(ast_diags), _diag_counts(asr_diags)


@pytest.mark.parametrize("name", [
    "smoke_check.f90",          # H001 only
    "smoke_intrinsics.f90",     # H003
    "smoke_functions.f90",      # H001 + H004 (single-file `use`)
])
def test_ast_parity_with_asr(name):
    """For Phase 1-supported fixtures, AST and ASR diagnostic-code
    multisets must agree.
    """
    fx = FIXTURES_DIR / name
    ast_counts, asr_counts = _run_both(fx)
    # Compare only the H-series — U-series are emitted upstream of
    # both checkers (in attach/scan), so they're equal by construction
    # and irrelevant here.
    ast_h = Counter({k: v for k, v in ast_counts.items() if k.startswith("H")})
    asr_h = Counter({k: v for k, v in asr_counts.items() if k.startswith("H")})
    assert ast_h == asr_h, (
        f"diag drift on {name}: AST={ast_h}, ASR={asr_h}"
    )
