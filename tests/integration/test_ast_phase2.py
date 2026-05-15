"""Phase 2 AST-only checker — cross-file ``use`` resolution.

The classic two-file fixture `tests/fixtures/multifile/main.f90 +
geo.f90`. ``main.f90`` `use`s ``geo``, calls ``box_area`` (a function)
and ``scale`` (a subroutine), and contains intentional mismatches.

Phase 2 must:
- See ``geo``'s exports from ``main``'s perspective via ``use geo``.
- Resolve the call ``box_area(s)`` against geo's signature, propagate
  the m^2 return.
- Resolve the subroutine call ``call scale(v, bad_r)`` and emit H004
  when the second arg's unit doesn't match.
- Emit H001 on ``bad_a = box_area(s)`` (kg vs m^2).

This is the smallest meaningful cross-file test. Bigger worksets land
once Phase 3+ refinements settle.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401
from dimfort.core.ast_multifile import check_files_ast
from dimfort.core.multifile import check_files


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "multifile"
GEO = FIXTURES / "geo.f90"
MAIN = FIXTURES / "main.f90"


def _h_codes(diags) -> Counter:
    return Counter(d.code for d in diags if d.code.startswith("H"))


def test_phase2_resolves_cross_file_function_call():
    """``main.f90``'s ``box_area`` call resolves through ``use geo``
    and the H001 on ``bad_a = box_area(s)`` fires."""
    result = check_files_ast([GEO, MAIN])
    main_diags = result.diagnostics.get(MAIN.resolve(), [])
    codes = sorted(d.code for d in main_diags)
    # Same expected set as the ASR pipeline.
    assert "H001" in codes, f"missing H001 on bad_a assignment: {main_diags}"
    assert "H004" in codes, f"missing H004 on scale call: {main_diags}"


def test_phase2_parity_with_asr_on_multifile():
    """The H-series multiset must match the ASR pipeline's, file by
    file. Workset-wide identity is the strongest regression guard."""
    ast_result = check_files_ast([GEO, MAIN])
    asr_result = check_files([GEO, MAIN])
    for path in (GEO.resolve(), MAIN.resolve()):
        assert _h_codes(ast_result.diagnostics.get(path, [])) == _h_codes(
            asr_result.diagnostics.get(path, [])
        ), (
            f"H-series drift on {path.name}: "
            f"AST={_h_codes(ast_result.diagnostics.get(path, []))}, "
            f"ASR={_h_codes(asr_result.diagnostics.get(path, []))}"
        )


def test_phase2_order_independent():
    """Workset order must not affect outcomes — same property as the
    ASR pipeline."""
    fwd = check_files_ast([GEO, MAIN])
    rev = check_files_ast([MAIN, GEO])
    for path in (GEO.resolve(), MAIN.resolve()):
        assert _h_codes(fwd.diagnostics.get(path, [])) == _h_codes(
            rev.diagnostics.get(path, [])
        )


def test_phase2_missing_module_yields_u007():
    """A workset that omits a module a consumer ``use``s must emit
    U007 on the consumer."""
    result = check_files_ast([MAIN])  # geo deliberately missing
    main_diags = result.diagnostics.get(MAIN.resolve(), [])
    u007s = [d for d in main_diags if d.code == "U007"]
    assert any("geo" in d.message.lower() for d in u007s), (
        f"expected U007 referencing 'geo': {main_diags}"
    )
