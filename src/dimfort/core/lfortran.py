"""LFortran subprocess wrapper.

DimFort consumes LFortran's AST and ASR as JSON. AST keeps comments
(``EOLComment`` nodes) which annotations live in; ASR carries the
resolved semantic information we use for type-checking. Two invocations
per file is acceptable cost.

LFortran is alpha software; failures on third-party code are expected.
Errors are returned as data (:class:`LFortranError`) so callers can
record them as diagnostics rather than crash.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_CONDA_PATH = Path.home() / "miniconda3" / "envs" / "lfortran" / "bin" / "lfortran"

TreeMode = Literal["ast", "asr"]


class LFortranNotFound(RuntimeError):
    """Raised when no usable ``lfortran`` binary can be located."""


@dataclass(frozen=True)
class LFortranError(Exception):
    """Non-zero exit from LFortran with stderr captured."""

    path: Path
    mode: TreeMode
    returncode: int
    stderr: str

    def __str__(self) -> str:
        return (
            f"lfortran {self.mode} failed on {self.path} "
            f"(rc={self.returncode}): {self.stderr.strip()}"
        )


def find_lfortran(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Locate the ``lfortran`` binary.

    Resolution order:
      1. ``explicit`` argument (when given).
      2. ``$LFORTRAN_BIN`` environment variable.
      3. ``lfortran`` on ``$PATH``.
      4. ``~/miniconda3/envs/lfortran/bin/lfortran`` (conda default).

    Raises :class:`LFortranNotFound` if none of these resolve to an
    existing executable.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("LFORTRAN_BIN")
    if env:
        candidates.append(Path(env))
    on_path = shutil.which("lfortran")
    if on_path:
        candidates.append(Path(on_path))
    candidates.append(DEFAULT_CONDA_PATH)
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    raise LFortranNotFound(
        "lfortran not found; install from conda-forge or set $LFORTRAN_BIN"
    )


_VERSION_RE = re.compile(r"LFortran version:\s*(\S+)")


def version(lfortran: str | os.PathLike[str] | None = None) -> str:
    """Return the LFortran version string (e.g. ``"0.63.0"``)."""
    binary = find_lfortran(lfortran)
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True
    )
    m = _VERSION_RE.search(result.stdout)
    return m.group(1) if m else result.stdout.strip().splitlines()[0]


def dump_tree(
    path: str | os.PathLike[str],
    mode: TreeMode,
    *,
    lfortran: str | os.PathLike[str] | None = None,
    implicit_interface: bool = False,
) -> dict:
    """Run ``lfortran --show-<mode> --json`` on ``path`` and return the dict.

    Raises :class:`LFortranError` if LFortran exits non-zero.
    """
    binary = find_lfortran(lfortran)
    flag = {"ast": "--show-ast", "asr": "--show-asr"}[mode]
    cmd = [str(binary), flag, "--json"]
    if implicit_interface:
        cmd.append("--implicit-interface")
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise LFortranError(
            path=Path(path),
            mode=mode,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return json.loads(result.stdout)


def load_trees(
    path: str | os.PathLike[str],
    *,
    lfortran: str | os.PathLike[str] | None = None,
    implicit_interface: bool = False,
) -> tuple[dict, dict]:
    """Return ``(ast, asr)`` for ``path``."""
    return (
        dump_tree(path, "ast", lfortran=lfortran, implicit_interface=implicit_interface),
        dump_tree(path, "asr", lfortran=lfortran, implicit_interface=implicit_interface),
    )


def walk(node: object):
    """Yield every dict node in an AST/ASR tree in document order."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)
