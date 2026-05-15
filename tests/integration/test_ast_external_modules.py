"""Verify that ``external_modules`` is honoured by the AST pipeline.

Intrinsic Fortran modules (``iso_fortran_env``) and external libraries
(``netcdf``, ``mpi``, …) must not surface as U007 just because they
aren't declared anywhere in the workspace. The ASR pipeline filters
them out during dep resolution; the AST pipeline must do the same in
``apply_use_clauses``.
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


def test_external_module_skipped_no_u007(tmp_path):
    src = tmp_path / "uses_intrinsic.f90"
    src.write_text(
        "module m\n"
        "  use, intrinsic :: iso_fortran_env, only: int64\n"
        "  use netcdf\n"
        "  implicit none\n"
        "end module\n"
    )
    result = check_files_ast(
        [src],
        external_modules=frozenset({"iso_fortran_env", "netcdf"}),
    )
    diags = result.diagnostics.get(src.resolve(), [])
    u007s = [d for d in diags if d.code == "U007"]
    assert u007s == [], (
        f"external modules should be silently skipped; got {u007s}"
    )


def test_unlisted_external_still_u007s(tmp_path):
    """Sanity check the negative case: a module that is genuinely
    missing from the workset and NOT on the allowlist should still
    surface as U007."""
    src = tmp_path / "uses_unknown.f90"
    src.write_text(
        "module m\n"
        "  use mystery_mod\n"
        "  implicit none\n"
        "end module\n"
    )
    result = check_files_ast([src], external_modules=frozenset({"netcdf"}))
    diags = result.diagnostics.get(src.resolve(), [])
    codes = Counter(d.code for d in diags)
    assert codes["U007"] == 1, (
        f"expected one U007 for 'mystery_mod'; got {diags}"
    )
    assert any("mystery_mod" in d.message for d in diags if d.code == "U007")
