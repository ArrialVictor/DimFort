"""Phase 0 spike: AST-only checker on the smoke_check fixture.

Reuses ``annotations.scan_file`` + ``attach`` to produce the
``var_units`` table — those are pure-Python, no ASR involvement — then
hands the AST + table to ``ast_checker.check``. Asserts the H001 we
get matches what the ASR-based checker would have produced.

If this passes on Phase 0's tiny scope, the same pattern scales up to
the full H/U series in later phases.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401  populate DEFAULT_TABLE
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


FIXTURE = Path(__file__).parents[1] / "fixtures" / "smoke_check.f90"


def test_ast_checker_finds_h001_on_smoke_check():
    """The fixture assigns ``mass / accel`` (kg*s²/m) to ``speed`` (m/s).
    AST-only checker must spot the mismatch and emit H001 — same code
    the ASR checker emits for the same fixture."""
    scan = scan_file(FIXTURE)
    attached = attach(scan)
    ast = lf.dump_tree(FIXTURE, "ast")

    diags = check(ast, attached.var_units, file=str(FIXTURE))

    h001s = [d for d in diags if d.code == "H001"]
    assert len(h001s) == 1, (
        f"expected exactly one H001 on smoke_check; got {diags}"
    )
    # H001 should land on the `speed = ...` assignment (line 13 in the
    # fixture), not the well-formed `force = ...` on line 10.
    assert h001s[0].start.line == 13, (
        f"H001 fired on wrong line: {h001s[0]}"
    )


def test_ast_checker_clean_on_correct_assignment():
    """``force = mass * accel`` is dimensionally clean — no H001."""
    scan = scan_file(FIXTURE)
    attached = attach(scan)
    ast = lf.dump_tree(FIXTURE, "ast")

    diags = check(ast, attached.var_units, file=str(FIXTURE))

    # Only the one bad assignment should diagnose; the good one shouldn't.
    assert all(
        d.start.line != 10 for d in diags
    ), f"unexpected diag on the good assignment: {diags}"
