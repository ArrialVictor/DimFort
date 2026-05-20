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
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("dimfort.config")

CONFIG_FILENAME = ".dimfort.toml"


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

    # [parser] — CPP preprocessing for ``.F90`` files. Identical
    # semantics to the equivalent ``[lfortran]`` keys in the
    # pre-tree-sitter era: ``cpp_defines`` becomes ``cpp -DX``,
    # ``include_paths`` becomes ``cpp -IPATH``. Required to unblock
    # files whose ``module``/``use`` constructs sit inside
    # ``#ifdef X`` regions (LMDZ's ``isotopes_routines_mod``,
    # ``radiation_cloud_cover``, etc).
    cpp_defines: tuple[str, ...] = ()
    include_paths: tuple[Path, ...] = ()

    # [units] — extension TOML merged on top of the shipped SI table.
    # Lets projects add domain-specific units (``hPa``, ``bar``,
    # ``degrees``, ``day``, ``percent``…) without touching the package.
    # Path is resolved relative to the config file.
    units_file: Path | None = None

    # [diagnostics] — per-rule severity overrides. Keys are diagnostic
    # codes (``H001``, ``H002``, ``H010``) or rule markers (``D1.4``,
    # ``D1.6``, ``D1.7``). Values are ``"error"`` / ``"warning"`` /
    # ``"off"``. Rule markers take precedence over generic codes when
    # both are configured. Empty dict ⇒ ship defaults apply.
    #
    # Example .dimfort.toml:
    #   [diagnostics]
    #   "D1.7" = "error"      # promote exponent-must-be-dim'less to hard error
    #   "D1.6" = "off"        # silence implicit wrapper untag warnings
    diagnostic_severities: dict[str, str] = field(default_factory=dict)


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

    # [parser] is the canonical home for CPP-related config. We also
    # accept the pre-tree-sitter ``[lfortran]`` keys for the same
    # fields so projects can upgrade in any order. Explicit
    # ``[parser]`` overrides ``[lfortran]`` when both are present.
    legacy_parser = raw.get("lfortran", {}) or {}
    parser_section = raw.get("parser", {}) or {}

    def _strings(key: str) -> tuple[str, ...]:
        raw_val = parser_section.get(key, legacy_parser.get(key, []) or [])
        return tuple(v for v in raw_val if isinstance(v, str) and v)

    def _paths(key: str) -> tuple[Path, ...]:
        raw_val = parser_section.get(key, legacy_parser.get(key, []) or [])
        return tuple(
            (base / p).resolve() for p in raw_val if isinstance(p, str)
        )

    cpp_defines = _strings("cpp_defines")
    include_paths = _paths("include_paths")

    units_section = raw.get("units", {}) or {}
    units_file_raw = units_section.get("file")
    units_file = (
        (base / units_file_raw).resolve()
        if isinstance(units_file_raw, str) and units_file_raw
        else None
    )

    # The pre-tree-sitter [checker] section had a `backend` field. It's
    # silently ignored now — accepted for backward compatibility but
    # not exposed on DimfortConfig.

    diagnostics_section = raw.get("diagnostics", {}) or {}
    diagnostic_severities: dict[str, str] = {}
    _VALID_LEVELS = {"error", "warning", "off"}
    for key, value in diagnostics_section.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if value not in _VALID_LEVELS:
            log.warning(
                "%s: ignoring [diagnostics] %r — value must be "
                "'error', 'warning', or 'off', got %r",
                path, key, value,
            )
            continue
        diagnostic_severities[key] = value

    return DimfortConfig(
        config_path=path,
        src_paths=src_paths,
        max_workset_size=max_size,
        external_modules=external_modules,
        cpp_defines=cpp_defines,
        include_paths=include_paths,
        units_file=units_file,
        diagnostic_severities=diagnostic_severities,
    )
