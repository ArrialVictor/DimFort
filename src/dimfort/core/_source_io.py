"""Encoding-tolerant Fortran source reader.

LMDZ and many other legacy Fortran codebases ship files containing
Latin-1 byte sequences in comments (French / German). A naive
``Path.read_text()`` crashes with ``UnicodeDecodeError`` on those.

This module's :func:`read_text` always tries UTF-8 first and falls
back to Latin-1, which losslessly decodes *any* byte sequence. The
ASCII identifiers DimFort's scanners care about (module names, use
statements, keywords) survive either decoding intact; only comment
prose might render oddly if displayed, which we never do.
"""
from __future__ import annotations

import os
from pathlib import Path

FORTRAN_EXTS: frozenset[str] = frozenset({
    ".f90", ".F90", ".f95", ".F95",
    ".f03", ".F03", ".f08", ".F08",
})


def discover_fortran_files(roots: list[Path]) -> list[Path]:
    """Recursively collect Fortran source files under ``roots``.

    Files passed directly are accepted regardless of extension match
    (the user named them explicitly); directories are walked for
    files whose suffix is in :data:`FORTRAN_EXTS`. Output is sorted
    for determinism.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if root.is_file():
            resolved = root.resolve()
            if resolved not in seen:
                seen.add(resolved)
                out.append(resolved)
            continue
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in FORTRAN_EXTS:
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
    out.sort()
    return out


def read_text(path: str | os.PathLike[str]) -> str:
    """Read a Fortran source file, tolerating non-UTF-8 encodings."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")
