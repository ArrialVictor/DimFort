"""Regression guard: cross-file bare-name leak in the workset merge.

Two files each declare a local ``w`` with different units. Without
proper file-level scoping in ``ast_multifile``, the workset-wide
``merged_var_units`` table would let one file's ``w`` annotation
shadow the other's, producing false-positive H001s on what is
locally-clean code. This test pins the fix down.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401
from dimfort.core.ast_multifile import check_files_ast


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "multifile_scope"


def test_no_cross_file_bare_name_leak():
    """Each file must check against its own `w` annotation, not a
    sibling's. Both files have *clean* local assignments; the workset
    must produce zero H-diagnostics."""
    result = check_files_ast(
        [FIXTURES / "file_a.f90", FIXTURES / "file_b.f90"]
    )
    h_codes = Counter(
        d.code
        for diags in result.diagnostics.values()
        for d in diags
        if d.code.startswith("H")
    )
    assert h_codes == Counter(), (
        f"unexpected H-diagnostics under file-level scoping: "
        f"{ {p.name: [(d.code, d.message) for d in diags] for p, diags in result.diagnostics.items() if diags} }"
    )
