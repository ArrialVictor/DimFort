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
from typing import Any

log = logging.getLogger("dimfort.config")

CONFIG_FILENAME = ".dimfort.toml"


@dataclass(frozen=True)
class UnitPatternEntry:
    """One configured ``@unit{}``-family delimiter pair.

    Attributes:
        open: Opening delimiter text (e.g. ``"@unit{"``).
        close: Closing delimiter text (e.g. ``"}"``).
    """

    open: str
    close: str


@dataclass(frozen=True)
class StructuredPatternEntry:
    """One configured ``@unit_assume{}`` / ``@unit_affine_conversion{}`` delimiter triple.

    Attributes:
        open: Opening delimiter text (e.g. ``"@unit_assume{"``).
        close: Closing delimiter text (e.g. ``"}"``).
        sep: Separator between the unit text and the directive-specific
            payload (reason for ``@unit_assume``, target unit for
            ``@unit_affine_conversion``).
    """

    open: str
    close: str
    sep: str


DEFAULT_UNIT_COMMENT_DELIMITERS: tuple[UnitPatternEntry, ...] = (
    UnitPatternEntry(open="@unit{", close="}"),
)
DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS: tuple[StructuredPatternEntry, ...] = (
    StructuredPatternEntry(open="@unit_assume{", close="}", sep=":"),
)
DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS: tuple[StructuredPatternEntry, ...] = (
    StructuredPatternEntry(open="@unit_affine_conversion{", close="}", sep="->"),
)


@dataclass(frozen=True)
class DimfortConfig:
    """Resolved project configuration, frozen.

    A ``None`` on an optional field means "not set, fall through to the
    next layer" (per the precedence chain documented at module level).
    Empty tuples mean "explicitly empty" — for instance, an explicit
    empty ``cpp_defines`` list in ``.dimfort.toml``.

    Per-field semantics are documented inline beside each field.
    """

    config_path: Path | None = None

    # ``True`` when ``load_config`` saw a ``.dimfort.toml`` but
    # couldn't parse it (malformed TOML, IO error). The CLI checks
    # this to honour the documented "exit 2 on invalid config"
    # contract; the LSP keeps the soft path and ignores the flag.
    load_error: str | None = None

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
    # ``#ifdef X`` regions (common in legacy Fortran codebases).
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
    # ``"info"`` / ``"off"`` (matches ``_VALID_LEVELS`` in ``_from_raw``).
    # Rule markers take precedence over generic codes when both are
    # configured. Empty dict ⇒ ship defaults apply.
    #
    # Example .dimfort.toml:
    #   [diagnostics]
    #   "D1.7" = "error"      # promote exponent-must-be-dim'less to hard error
    #   "D1.6" = "off"        # silence implicit wrapper untag warnings
    diagnostic_severities: dict[str, str] = field(default_factory=dict)

    # [scale] — opt-in scale checking. Off by default: dimension-only
    # checking stays first-class. When on, multiplicative S001 and
    # affine S002 (degC) both fire — same-dimension but different
    # ``factor`` operands (e.g. ``hPa`` vs ``Pa``, ``g/kg`` vs
    # ``kg/kg``) and offset-differing operands. See docs/design/scale.md.
    #   [scale]
    #   enabled = true
    scale_mode: bool = False

    # [parser] — configurable comment delimiters for the three unit
    # directive families. See docs/design/unit-comment-delimiters.md.
    # Defaults preserve the canonical ``@unit{...}`` etc. forms; users
    # opt in to additional patterns (e.g. ``[m/s]``) per directive.
    unit_comment_delimiters: tuple[UnitPatternEntry, ...] = field(
        default_factory=lambda: DEFAULT_UNIT_COMMENT_DELIMITERS
    )
    unit_assume_comment_delimiters: tuple[StructuredPatternEntry, ...] = field(
        default_factory=lambda: DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS
    )
    unit_affine_comment_delimiters: tuple[StructuredPatternEntry, ...] = field(
        default_factory=lambda: DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS
    )

    # [cache] max_entries — FIFO cap on the in-memory tree / module-exports
    # / projection caches. ``None`` (default, or ``"auto"`` in TOML) means
    # the LSP picks an adaptive value: ``max(observed_workset_size × 4,
    # 4096)`` recomputed after each ``check_files`` so the cap grows with
    # the largest workset seen this session and never evicts inside a
    # single check pass. An explicit positive integer pins the cap.
    # Sub-1000 values are accepted but warned about — on a real-world
    # Fortran codebase (~2000+ files) anything under workset-size
    # silently defeats the cache by evicting during the check itself.
    cache_max_entries: int | None = None


def find_config(start: Path) -> Path | None:
    """Walk upward from ``start`` looking for a ``.dimfort.toml``.

    Args:
        start: Path to begin the search from. May point at either a file
            or a directory; if a file, its parent directory is used as
            the starting point.

    Returns:
        Absolute path to the first ``.dimfort.toml`` encountered, or
        ``None`` if the walk reaches a filesystem root without finding
        one.
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

    Args:
        start: Path to begin the upward search from (see
            :func:`find_config` for the walk rules).

    Returns:
        Resolved configuration. An empty :class:`DimfortConfig` if no
        file was found. On a *malformed* file (TOML decode error or
        ``OSError``), a config carrying ``config_path=path`` and
        ``load_error=str(exc)`` so the CLI can honour its exit-2
        contract for invalid configs; the LSP keeps the soft path and
        ignores ``load_error``.

    Note:
        Parse errors are logged but never raise — a missing or broken
        config must not break the CLI or the LSP.
    """
    path = find_config(start)
    if path is None:
        return DimfortConfig()
    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        log.warning("could not parse %s: %s", path, exc)
        # CLI consumers check ``load_error`` and exit 2 per the
        # documented contract (cli.md "Exit codes" — invalid config →
        # 2). LSP keeps the soft path and ignores the flag.
        return DimfortConfig(config_path=path, load_error=str(exc))
    return _from_raw(raw, path)


def _from_raw(raw: dict[str, Any], path: Path) -> DimfortConfig:
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
    _VALID_LEVELS = {"error", "warning", "info", "off"}
    for key, value in diagnostics_section.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if value not in _VALID_LEVELS:
            log.warning(
                "%s: ignoring [diagnostics] %r — value must be "
                "'error', 'warning', 'info', or 'off', got %r",
                path, key, value,
            )
            continue
        diagnostic_severities[key] = value

    scale_section = raw.get("scale", {}) or {}
    scale_mode = bool(scale_section.get("enabled", False))

    cache_section = raw.get("cache", {}) or {}
    cache_max_entries_raw = cache_section.get("max_entries")
    cache_max_entries: int | None
    if cache_max_entries_raw is None or cache_max_entries_raw == "auto":
        cache_max_entries = None
    elif isinstance(cache_max_entries_raw, bool):
        # ``bool`` is an ``int`` subclass — guard so ``true`` / ``false``
        # isn't silently accepted as 1 / 0.
        log.warning(
            "%s: [cache].max_entries must be 'auto' or a positive int — "
            "got %r, falling back to 'auto'",
            path, cache_max_entries_raw,
        )
        cache_max_entries = None
    elif isinstance(cache_max_entries_raw, int) and cache_max_entries_raw > 0:
        cache_max_entries = cache_max_entries_raw
        if cache_max_entries < 1000:
            log.warning(
                "%s: [cache].max_entries=%d is below the recommended "
                "floor (1000). On a workset larger than this, entries "
                "will be evicted inside a single check pass, defeating "
                "the cache.",
                path, cache_max_entries,
            )
    else:
        log.warning(
            "%s: [cache].max_entries must be 'auto' or a positive int — "
            "got %r, falling back to 'auto'",
            path, cache_max_entries_raw,
        )
        cache_max_entries = None

    unit_comment_delimiters = _parse_unit_pattern_list(
        parser_section, "unit_comment_delimiters", path,
        default=DEFAULT_UNIT_COMMENT_DELIMITERS,
    )
    unit_assume_comment_delimiters = _parse_structured_pattern_list(
        parser_section, "unit_assume_comment_delimiters", path,
        default=DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS,
    )
    unit_affine_comment_delimiters = _parse_structured_pattern_list(
        parser_section, "unit_affine_comment_delimiters", path,
        default=DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS,
    )

    return DimfortConfig(
        config_path=path,
        src_paths=src_paths,
        max_workset_size=max_size,
        external_modules=external_modules,
        cpp_defines=cpp_defines,
        include_paths=include_paths,
        units_file=units_file,
        diagnostic_severities=diagnostic_severities,
        scale_mode=scale_mode,
        unit_comment_delimiters=unit_comment_delimiters,
        unit_assume_comment_delimiters=unit_assume_comment_delimiters,
        unit_affine_comment_delimiters=unit_affine_comment_delimiters,
        cache_max_entries=cache_max_entries,
    )


# ---------------------------------------------------------------------------
# Delimiter-list parsing helpers
# ---------------------------------------------------------------------------


def _validate_required_string(
    entry: dict[str, Any], key: str, *, where: str, path: Path
) -> str | None:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        log.error(
            "%s: %s: missing or empty required string field %r — "
            "entry ignored", path, where, key,
        )
        return None
    return value


def _parse_unit_pattern_list(
    parser_section: dict[str, Any],
    key: str,
    path: Path,
    *,
    default: tuple[UnitPatternEntry, ...],
) -> tuple[UnitPatternEntry, ...]:
    if key not in parser_section:
        return default
    raw_list = parser_section.get(key)
    if not isinstance(raw_list, list):
        log.error(
            "%s: [parser].%s must be an array of tables — falling back "
            "to default", path, key,
        )
        return default
    if not raw_list:
        log.error(
            "%s: [parser].%s is explicitly empty — clearing the unit "
            "pattern list would disable all unit recognition; falling "
            "back to default", path, key,
        )
        return default
    allowed = {"open", "close"}
    entries: list[UnitPatternEntry] = []
    seen: set[tuple[str, str]] = set()
    for i, raw in enumerate(raw_list):
        where = f"[parser].{key}[{i}]"
        if not isinstance(raw, dict):
            log.error("%s: %s: entry must be a table — ignored", path, where)
            continue
        unknown = set(raw) - allowed
        if unknown:
            log.error(
                "%s: %s: unknown key(s) %s — entry ignored",
                path, where, sorted(unknown),
            )
            continue
        op = _validate_required_string(raw, "open", where=where, path=path)
        cl = _validate_required_string(raw, "close", where=where, path=path)
        if op is None or cl is None:
            continue
        key_pair = (op, cl)
        if key_pair in seen:
            log.error(
                "%s: %s: duplicate entry {open=%r, close=%r} — ignored",
                path, where, op, cl,
            )
            continue
        seen.add(key_pair)
        entries.append(UnitPatternEntry(open=op, close=cl))
    if not entries:
        log.error(
            "%s: [parser].%s yielded no valid entries — falling back "
            "to default", path, key,
        )
        return default
    return tuple(entries)


def _parse_structured_pattern_list(
    parser_section: dict[str, Any],
    key: str,
    path: Path,
    *,
    default: tuple[StructuredPatternEntry, ...],
) -> tuple[StructuredPatternEntry, ...]:
    if key not in parser_section:
        return default
    raw_list = parser_section.get(key)
    if not isinstance(raw_list, list):
        log.error(
            "%s: [parser].%s must be an array of tables — falling back "
            "to default", path, key,
        )
        return default
    if not raw_list:
        log.error(
            "%s: [parser].%s is explicitly empty — clearing the list "
            "would disable directive recognition; falling back to "
            "default", path, key,
        )
        return default
    allowed = {"open", "close", "sep"}
    entries: list[StructuredPatternEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for i, raw in enumerate(raw_list):
        where = f"[parser].{key}[{i}]"
        if not isinstance(raw, dict):
            log.error("%s: %s: entry must be a table — ignored", path, where)
            continue
        unknown = set(raw) - allowed
        if unknown:
            log.error(
                "%s: %s: unknown key(s) %s — entry ignored",
                path, where, sorted(unknown),
            )
            continue
        op = _validate_required_string(raw, "open", where=where, path=path)
        cl = _validate_required_string(raw, "close", where=where, path=path)
        sep = _validate_required_string(raw, "sep", where=where, path=path)
        if op is None or cl is None or sep is None:
            continue
        if sep in op or sep in cl:
            log.error(
                "%s: %s: sep %r must not appear inside open %r or "
                "close %r — entry ignored", path, where, sep, op, cl,
            )
            continue
        key_triple = (op, cl, sep)
        if key_triple in seen:
            log.error(
                "%s: %s: duplicate entry {open=%r, close=%r, sep=%r} — "
                "ignored", path, where, op, cl, sep,
            )
            continue
        seen.add(key_triple)
        entries.append(StructuredPatternEntry(open=op, close=cl, sep=sep))
    if not entries:
        log.error(
            "%s: [parser].%s yielded no valid entries — falling back "
            "to default", path, key,
        )
        return default
    return tuple(entries)
