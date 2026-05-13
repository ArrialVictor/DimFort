"""Live tests for the LFortran subprocess wrapper.

These tests need an actual ``lfortran`` binary. They auto-skip when
none is found, so the suite stays runnable in environments without it
(including CI until LFortran is installed there).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core import lfortran as lf


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


HELLO = """\
program hello
  implicit none
  real :: v
  v = 1.0
  print *, v
end program hello
"""

DECLIST = """\
program declist
  implicit none
  real :: a, b, c
  a = 1.0
  b = 2.0
  c = 3.0
end program declist
"""


def test_version_string_parses():
    v = lf.version()
    assert v
    # Looks like a dotted version number.
    parts = v.split(".")
    assert all(p.isdigit() for p in parts[:2])


def test_dump_ast_returns_dict(tmp_path: Path):
    src = tmp_path / "hello.f90"
    src.write_text(HELLO)
    ast = lf.dump_tree(src, "ast")
    assert isinstance(ast, dict)


def test_dump_asr_returns_dict_with_variables(tmp_path: Path):
    src = tmp_path / "hello.f90"
    src.write_text(HELLO)
    asr = lf.dump_tree(src, "asr")
    variables = [n for n in lf.walk(asr) if isinstance(n, dict) and n.get("node") == "Variable"]
    assert any(n["fields"].get("name") == "v" for n in variables)


def test_declaration_list_yields_one_variable_per_name(tmp_path: Path):
    src = tmp_path / "declist.f90"
    src.write_text(DECLIST)
    asr = lf.dump_tree(src, "asr")
    names = sorted(
        n["fields"]["name"]
        for n in lf.walk(asr)
        if isinstance(n, dict) and n.get("node") == "Variable"
    )
    assert names == ["a", "b", "c"]


def test_dump_tree_raises_on_bad_source(tmp_path: Path):
    src = tmp_path / "broken.f90"
    src.write_text("this is not fortran\n")
    with pytest.raises(lf.LFortranError) as exc:
        lf.dump_tree(src, "asr")
    assert exc.value.returncode != 0
