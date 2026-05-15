"""Project configuration loader.

Loads ``.dimfort.toml`` from the workspace root (walking upward from a
start path until either a file is found or a filesystem root is hit).
The returned :class:`DimfortConfig` is a frozen snapshot; callers
overlay it with CLI flags / LSP ``initializationOptions`` as needed.

Precedence (lowest → highest):

1. Built-in defaults.
2. ``.dimfort.toml`` (this loader).
3. LSP ``initializationOptions`` / ``settings.json``.
4. Explicit CLI flags.

Unknown keys are silently ignored so newer DimFort versions can add
fields without breaking older projects.
"""
from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("dimfort.config")

CONFIG_FILENAME = ".dimfort.toml"


VALID_BACKENDS: frozenset[str] = frozenset({"ast", "asr"})


@dataclass(frozen=True)
class DimfortConfig:
    """Resolved configuration. ``None`` means "not set, fall through to
    the next layer." Empty tuples mean "explicitly empty."
    """

    config_path: Path | None = None

    # [project]
    src_paths: tuple[Path, ...] = ()

    # [workset]
    max_workset_size: int | None = None
    external_modules: tuple[str, ...] = ()

    # [lfortran]
    lfortran_binary: Path | None = None

    # [checker]
    backend: str | None = None    # "ast" | "asr"; None → caller default


def find_config(start: Path) -> Path | None:
    """Walk upward from ``start`` looking for a ``.dimfort.toml``.

    Returns the absolute path on first hit, or ``None`` if none is found
    before reaching a filesystem root. ``start`` may point at either a
    file or a directory.
    """
    cur = Path(start).resolve()
    if cur.is_file():
        cur = cur.parent
    while True:
        candidate = cur / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def load_config(start: Path) -> DimfortConfig:
    """Locate and parse the nearest ``.dimfort.toml``.

    Returns an empty :class:`DimfortConfig` if no file is found or if
    the file is malformed. Parse errors are logged but never raise — a
    missing/broken config must not break the CLI or the LSP.
    """
    path = find_config(start)
    if path is None:
        return DimfortConfig()
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse %s: %s", path, exc)
        return DimfortConfig(config_path=path)
    return _from_raw(raw, path)


def _from_raw(raw: dict, path: Path) -> DimfortConfig:
    base = path.parent

    project = raw.get("project", {}) or {}
    src_paths_raw = project.get("src_paths", []) or []
    src_paths = tuple(
        (base / p).resolve()
        for p in src_paths_raw
        if isinstance(p, str)
    )

    workset = raw.get("workset", {}) or {}
    max_size = workset.get("max_size")
    if not isinstance(max_size, int) or max_size <= 0:
        max_size = None
    external_modules_raw = workset.get("external_modules", []) or []
    external_modules = tuple(
        m.lower()
        for m in external_modules_raw
        if isinstance(m, str)
    )

    lfortran_section = raw.get("lfortran", {}) or {}
    binary_raw = lfortran_section.get("binary")
    lfortran_binary = (
        (base / binary_raw).resolve() if isinstance(binary_raw, str) else None
    )

    checker_section = raw.get("checker", {}) or {}
    backend_raw = checker_section.get("backend")
    if isinstance(backend_raw, str) and backend_raw.lower() in VALID_BACKENDS:
        backend = backend_raw.lower()
    else:
        if backend_raw is not None:
            log.warning(
                "ignoring [checker].backend = %r in %s; "
                "expected one of %s",
                backend_raw, path, sorted(VALID_BACKENDS),
            )
        backend = None

    return DimfortConfig(
        config_path=path,
        src_paths=src_paths,
        max_workset_size=max_size,
        external_modules=external_modules,
        lfortran_binary=lfortran_binary,
        backend=backend,
    )
