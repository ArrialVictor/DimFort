"""Tests for the encoding-tolerant Fortran source reader and its
direct consumers (scan_file, has_module, modules_provided).

LMDZ ships files with Latin-1 byte sequences in French comments. Any
helper that text-scans a source file must survive them without
raising UnicodeDecodeError; otherwise the LSP pipeline crashes the
moment it touches such a file.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core._source_io import read_text
from dimfort.core.annotations import scan_file
from dimfort.core.lfortran import has_module, modules_provided


# `é` (0xe9) is valid Latin-1 but an invalid standalone UTF-8 byte.
_LATIN1_SAMPLE = b"module foo\n! commentaire en fran\xe9ais\nend module foo\n"


def test_read_text_handles_utf8(tmp_path: Path):
    p = tmp_path / "u.f90"
    p.write_text("module ok\nend module ok\n", encoding="utf-8")
    assert "module ok" in read_text(p)


def test_read_text_falls_back_to_latin1(tmp_path: Path):
    p = tmp_path / "l.f90"
    p.write_bytes(_LATIN1_SAMPLE)
    out = read_text(p)
    assert "module foo" in out
    # The é round-trips as the Latin-1 character (not the UTF-8
    # replacement char) because we decode lossless via Latin-1.
    assert "franéais" in out


def test_has_module_tolerates_latin1(tmp_path: Path):
    p = tmp_path / "l.f90"
    p.write_bytes(_LATIN1_SAMPLE)
    assert has_module(p) is True


def test_modules_provided_tolerates_latin1(tmp_path: Path):
    p = tmp_path / "l.f90"
    p.write_bytes(_LATIN1_SAMPLE)
    assert modules_provided(p) == ["foo"]


def test_scan_file_tolerates_latin1(tmp_path: Path):
    p = tmp_path / "l.f90"
    p.write_bytes(
        b"!> @brief commentaire en fran\xe7ais\n"
        b"module foo\n"
        b"  real :: x  !< @unit{m}\n"
        b"end module foo\n"
    )
    result = scan_file(p)
    # If we got here, the scan didn't crash; verify the @unit was found.
    assert len(result.annotations) == 1
    assert result.annotations[0].unit_text == "m"
