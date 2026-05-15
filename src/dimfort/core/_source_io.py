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


def read_text(path: str | os.PathLike[str]) -> str:
    """Read a Fortran source file, tolerating non-UTF-8 encodings."""
    raw = Path(path).read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")
