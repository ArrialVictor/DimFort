"""LFortran subprocess wrapper.

DimFort consumes LFortran's AST and ASR as JSON. AST keeps comments
(``EOLComment`` nodes) which annotations live in; ASR carries the
resolved semantic information we use for type-checking. Two invocations
per file is acceptable cost.

For multi-file projects, LFortran needs ``.mod`` files for any module
referenced via ``use``. The helpers in this module's bottom half
(``compile_module``, ``compile_modules_retrying``) drive ``lfortran -c``
to produce those.

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

_MODULE_DECL_RE = re.compile(
    r"^\s*module\s+(?!procedure\b)([A-Za-z_]\w*)",
    re.IGNORECASE | re.MULTILINE,
)

DEFAULT_CONDA_PATH = Path.home() / "miniconda3" / "envs" / "lfortran" / "bin" / "lfortran"

TreeMode = Literal["ast", "asr"]


class LFortranNotFound(RuntimeError):
    """Raised when no usable ``lfortran`` binary can be located."""


@dataclass
class LFortranError(Exception):
    """Non-zero exit from LFortran with stderr captured.

    Not frozen: Python attaches ``__traceback__`` on raise, which a frozen
    dataclass disallows.
    """

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


_VERSION_BY_BINARY: dict[str, str] = {}


def cached_version(lfortran: str | os.PathLike[str] | None = None) -> str:
    """Return :func:`version`, memoised by resolved binary path.

    Callers that look up the version many times in one pipeline run
    (e.g. the per-file cache validator) avoid re-invoking ``lfortran
    --version`` for every file.
    """
    binary = str(find_lfortran(lfortran))
    cached = _VERSION_BY_BINARY.get(binary)
    if cached is not None:
        return cached
    v = version(lfortran)
    _VERSION_BY_BINARY[binary] = v
    return v


def dump_tree(
    path: str | os.PathLike[str],
    mode: TreeMode,
    *,
    lfortran: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    implicit_interface: bool = False,
    include_paths: tuple[str | os.PathLike[str], ...] = (),
) -> dict:
    """Run ``lfortran --show-<mode> --json`` on ``path`` and return the dict.

    Optional ``cwd`` is forwarded to the subprocess so that ``.mod``
    files produced by an earlier :func:`compile_module` are visible
    when this file ``use``s them.

    Raises :class:`LFortranError` if LFortran exits non-zero.
    """
    binary = find_lfortran(lfortran)
    flag = {"ast": "--show-ast", "asr": "--show-asr"}[mode]
    # --no-style-suggestions: LFortran 0.63 treats things like `.ne.` and
    # numeric kind selectors as hard errors by default. Real-world Fortran
    # (LMDZ in particular) is full of those constructs and DimFort isn't
    # in the style-policing business, so we always suppress them.
    # --cpp-infer: enable LFortran's C preprocessor for files that look
    # like they need it (`.F90` extension or `#`-directives). Without it,
    # `#ifdef`/`#define`/`#include` warnings make LFortran exit non-zero
    # and U007 fires on every preprocessed source.
    cmd = [
        str(binary), flag, "--json",
        "--no-style-suggestions", "--cpp-infer",
    ]
    if implicit_interface:
        cmd.append("--implicit-interface")
    for inc in include_paths:
        cmd.extend(["-I", str(inc)])
    cmd.append(str(path))
    # Capture raw bytes — Latin-1 bytes leak through LFortran's JSON
    # output when source comments contain non-UTF-8 sequences (LMDZ
    # ships these). text=True would crash with UnicodeDecodeError.
    # Mirrors the UTF-8 → Latin-1 fallback in core._source_io.read_text.
    result = subprocess.run(cmd, capture_output=True, cwd=cwd)
    try:
        stderr = result.stderr.decode("utf-8")
    except UnicodeDecodeError:
        stderr = result.stderr.decode("latin-1")
    if result.returncode != 0:
        raise LFortranError(
            path=Path(path),
            mode=mode,
            returncode=result.returncode,
            stderr=stderr,
        )
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        stdout = result.stdout.decode("latin-1")
    return json.loads(stdout)


def load_trees(
    path: str | os.PathLike[str],
    *,
    lfortran: str | os.PathLike[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    implicit_interface: bool = False,
) -> tuple[dict, dict]:
    """Return ``(ast, asr)`` for ``path``."""
    return (
        dump_tree(path, "ast", lfortran=lfortran, cwd=cwd, implicit_interface=implicit_interface),
        dump_tree(path, "asr", lfortran=lfortran, cwd=cwd, implicit_interface=implicit_interface),
    )


# ---------------------------------------------------------------------------
# Module-file compilation (multi-file orchestration)
# ---------------------------------------------------------------------------


def has_module(path: str | os.PathLike[str]) -> bool:
    """Cheap text scan: does ``path`` declare at least one Fortran module?"""
    from dimfort.core._source_io import read_text
    try:
        text = read_text(path)
    except OSError:
        return False
    return bool(_MODULE_DECL_RE.search(text))


def modules_provided(path: str | os.PathLike[str]) -> list[str]:
    """Return the names of every module declared in ``path`` (lower-cased)."""
    from dimfort.core._source_io import read_text
    try:
        text = read_text(path)
    except OSError:
        return []
    return [m.group(1).lower() for m in _MODULE_DECL_RE.finditer(text)]


def compile_module(
    path: str | os.PathLike[str],
    *,
    cwd: str | os.PathLike[str],
    lfortran: str | os.PathLike[str] | None = None,
    implicit_interface: bool = False,
) -> str | None:
    """Run ``lfortran -c`` so any ``.mod`` files land in ``cwd``.

    Returns ``None`` on success, or the captured stderr/stdout text on
    failure. Non-raising so callers can retry: a module's compile may
    fail on one pass because a dependency's ``.mod`` doesn't exist yet,
    and succeed on a later pass.
    """
    binary = find_lfortran(lfortran)
    cmd = [str(binary), "-c", "--no-style-suggestions", "--cpp-infer"]
    if implicit_interface:
        cmd.append("--implicit-interface")
    cmd.append(str(path))
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        return result.stderr or result.stdout or "(no error message)"
    return None


def compile_modules_retrying(
    basenames: list[str],
    *,
    cwd: str | os.PathLike[str],
    lfortran: str | os.PathLike[str] | None = None,
    implicit_interface: bool = False,
) -> dict[str, str]:
    """Compile every module file under ``cwd`` until no further progress.

    Each pass retries any file that failed on the previous one; failures
    "stick" only after a full pass produces no successful compile.
    Returns ``{basename: stderr}`` for files that never compiled — any
    remaining entry is either part of a ``use`` cycle or has an
    unresolved dependency.
    """
    pending = list(basenames)
    last_errors: dict[str, str] = {}
    while pending:
        next_pending: list[str] = []
        progressed = False
        for f in pending:
            err = compile_module(
                f, cwd=cwd, lfortran=lfortran,
                implicit_interface=implicit_interface,
            )
            if err is None:
                last_errors.pop(f, None)
                progressed = True
            else:
                last_errors[f] = err
                next_pending.append(f)
        if not progressed:
            return {f: last_errors[f] for f in next_pending}
        pending = next_pending
    return {}


def walk(node: object):
    """Yield every dict node in an AST/ASR tree in document order."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)
