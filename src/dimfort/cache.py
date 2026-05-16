"""Per-project analysis cache.

Layout: ``<cache_dir>/<sha1(abs_path)[:16]>.json`` — opaque filename
keyed by absolute source path. Each entry stores the AST + ASR pair
LFortran produced for that file, plus the key fields used to validate
freshness: source content sha256, DimFort version, LFortran version,
and the ``implicit_interface`` flag (it changes the output).

On miss (no file, or any key mismatch) the caller falls back to
``lf.load_trees`` and writes a fresh entry. Writes are atomic
(tempfile + rename) so a partially-written entry never breaks loads.

The cache is internal but the on-disk schema is documented in
``docs/cache-format.md`` for third-party consumers.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("dimfort.cache")

DEFAULT_CACHE_DIRNAME = ".dimfort"
CACHE_SCHEMA_VERSION = 1


def default_cache_dir(project_root: Path | str = ".") -> Path:
    return Path(project_root).resolve() / DEFAULT_CACHE_DIRNAME / "cache"


def cache_size_bytes(cache_dir: Path) -> int:
    if not cache_dir.exists():
        return 0
    return sum(p.stat().st_size for p in cache_dir.rglob("*") if p.is_file())


def _human_size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TiB"


def clean(cache_dir: Path) -> int:
    """Remove the cache directory. Returns bytes freed."""
    freed = cache_size_bytes(cache_dir)
    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    return freed


def info(cache_dir: Path) -> str:
    if not cache_dir.exists():
        return f"cache: {cache_dir} (does not exist)"
    n_files = sum(1 for _ in cache_dir.rglob("*.json"))
    size = cache_size_bytes(cache_dir)
    return f"cache: {cache_dir}\n  entries: {n_files}\n  size: {_human_size(size)}"


# ---------------------------------------------------------------------------
# Tree cache (AST + ASR pairs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Key:
    abs_path: str
    content_sha256: str
    dimfort_version: str
    lfortran_version: str
    implicit_interface: bool


def _entry_path(cache_dir: Path, abs_path: Path) -> Path:
    digest = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.json"


def _mods_entry_path(cache_dir: Path, abs_path: Path) -> Path:
    digest = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.mods.json"


def _build_key(
    abs_path: Path,
    content_bytes: bytes,
    lf_module,
    lfortran,
    implicit_interface: bool,
) -> "_Key":
    from dimfort import __version__
    return _Key(
        abs_path=str(abs_path),
        content_sha256=_sha256_bytes(content_bytes),
        dimfort_version=__version__,
        lfortran_version=lf_module.cached_version(lfortran),
        implicit_interface=implicit_interface,
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_entry(entry_path: Path) -> dict | None:
    try:
        with entry_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_entry(entry_path: Path, payload: dict) -> None:
    """Atomic write: tempfile in the same dir, then rename."""
    try:
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp-", suffix=".json", dir=entry_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_name, entry_path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        # A read-only filesystem or permission error must not break the
        # check pipeline. Log once and move on; subsequent runs will keep
        # re-parsing (just without the speedup).
        log.debug("cache write failed for %s: %s", entry_path, exc)


def load_trees_cached(
    load_path,
    *,
    source_path: Path,
    lfortran=None,
    cwd=None,
    implicit_interface: bool = False,
    cache_dir: Path | None,
    content: bytes | None = None,
) -> tuple[dict, dict]:
    """Return ``(ast, asr)`` for a source file, consulting the cache.

    ``load_path`` is what LFortran sees (often a basename inside a
    workset's temp ``cwd``, so ``.mod`` files resolve). ``source_path``
    is the absolute path of the real source file on disk — used to read
    content for the cache key and to derive the cache file's location.

    ``cache_dir=None`` disables caching entirely. ``content`` lets the
    caller pass an in-memory override (LSP buffer); when given, the
    cache is bypassed for this file but other workset files still
    benefit. On any kind of cache miss (no entry, content drift,
    version drift, corrupt JSON) we fall back to ``lf.load_trees`` and
    refresh the entry. Cache write errors are swallowed — caching is
    opt-in speedup, never required for correctness.
    """
    from dimfort.core import lfortran as lf

    abs_path = Path(source_path).resolve()
    use_cache = cache_dir is not None and content is None

    if not use_cache:
        return lf.load_trees(
            load_path,
            lfortran=lfortran,
            cwd=cwd,
            implicit_interface=implicit_interface,
        )

    assert cache_dir is not None  # for type-checker
    try:
        file_bytes = abs_path.read_bytes()
    except OSError:
        # Can't even read the source — let lf.load_trees produce the
        # real error.
        return lf.load_trees(
            load_path,
            lfortran=lfortran,
            cwd=cwd,
            implicit_interface=implicit_interface,
        )

    key = _build_key(abs_path, file_bytes, lf, lfortran, implicit_interface)

    entry_path = _entry_path(cache_dir, abs_path)
    entry = _read_entry(entry_path)
    if entry and _entry_matches(entry, key):
        ast = entry.get("ast")
        asr = entry.get("asr")
        if isinstance(ast, dict) and isinstance(asr, dict):
            return ast, asr

    # Miss: invoke LFortran and refresh the entry.
    ast, asr = lf.load_trees(
        load_path,
        lfortran=lfortran,
        cwd=cwd,
        implicit_interface=implicit_interface,
    )
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "abs_path": key.abs_path,
        "content_sha256": key.content_sha256,
        "dimfort_version": key.dimfort_version,
        "lfortran_version": key.lfortran_version,
        "implicit_interface": key.implicit_interface,
        "ast": ast,
        "asr": asr,
    }
    _write_entry(entry_path, payload)
    return ast, asr


def _entry_matches(entry: dict, key: _Key) -> bool:
    return (
        entry.get("schema_version") == CACHE_SCHEMA_VERSION
        and entry.get("abs_path") == key.abs_path
        and entry.get("content_sha256") == key.content_sha256
        and entry.get("dimfort_version") == key.dimfort_version
        and entry.get("lfortran_version") == key.lfortran_version
        and entry.get("implicit_interface") == key.implicit_interface
    )


# ---------------------------------------------------------------------------
# Single-tree cache (AST or ASR alone) — used by the AST-only backend
# ---------------------------------------------------------------------------


def _single_entry_path(cache_dir: Path, abs_path: Path, mode: str) -> Path:
    """Cache filename for the single-mode cache. Distinct from
    :func:`_entry_path` (used by ``load_trees_cached`` which stores
    both modes together) so AST-only and ASR-pair caches can coexist
    without one overwriting the other."""
    digest = hashlib.sha1(str(abs_path).encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}.{mode}.json"


def load_single_tree_cached(
    load_path,
    *,
    mode: str,
    source_path: Path,
    lfortran=None,
    cwd=None,
    implicit_interface: bool = False,
    cache_dir: Path | None,
    content: bytes | None = None,
    include_paths: tuple = (),
    cpp_defines: tuple = (),
) -> dict:
    """Return the AST (or ASR) for a source file, consulting the cache.

    Mirrors :func:`load_trees_cached` but only invokes one ``lfortran
    --show-<mode>`` instead of the pair. ``mode`` is ``"ast"`` or
    ``"asr"``. Used by the AST-only backend so that warm runs skip the
    LFortran subprocess entirely.

    ``include_paths`` and ``cpp_defines`` are part of the input that
    affects LFortran's output; they're hashed into the cache key as a
    tuple so a config change invalidates entries cleanly.
    """
    from dimfort.core import lfortran as lf

    abs_path = Path(source_path).resolve()
    use_cache = cache_dir is not None and content is None

    if not use_cache:
        return lf.dump_tree(
            load_path,
            mode,
            lfortran=lfortran,
            cwd=cwd,
            implicit_interface=implicit_interface,
            include_paths=include_paths,
            cpp_defines=cpp_defines,
        )

    assert cache_dir is not None
    try:
        file_bytes = abs_path.read_bytes()
    except OSError:
        return lf.dump_tree(
            load_path,
            mode,
            lfortran=lfortran,
            cwd=cwd,
            implicit_interface=implicit_interface,
            include_paths=include_paths,
            cpp_defines=cpp_defines,
        )

    key = _build_key(abs_path, file_bytes, lf, lfortran, implicit_interface)
    # Mix include_paths / cpp_defines into the content sha256 so that
    # editing them invalidates the cache for every file (they change
    # LFortran's behaviour globally).
    extras = (
        b"|inc=" + repr(sorted(str(p) for p in include_paths)).encode("utf-8")
        + b"|def=" + repr(sorted(cpp_defines)).encode("utf-8")
    )
    extended_sha = hashlib.sha256(
        key.content_sha256.encode("ascii") + extras
    ).hexdigest()

    entry_path = _single_entry_path(cache_dir, abs_path, mode)
    entry = _read_entry(entry_path)
    if (
        entry
        and entry.get("schema_version") == CACHE_SCHEMA_VERSION
        and entry.get("abs_path") == key.abs_path
        and entry.get("content_sha256") == extended_sha
        and entry.get("dimfort_version") == key.dimfort_version
        and entry.get("lfortran_version") == key.lfortran_version
        and entry.get("implicit_interface") == key.implicit_interface
        and entry.get("mode") == mode
        and isinstance(entry.get("tree"), dict)
    ):
        return entry["tree"]

    # Miss: invoke LFortran and refresh.
    tree = lf.dump_tree(
        load_path,
        mode,
        lfortran=lfortran,
        cwd=cwd,
        implicit_interface=implicit_interface,
        include_paths=include_paths,
        cpp_defines=cpp_defines,
    )
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "abs_path": key.abs_path,
        "content_sha256": extended_sha,
        "dimfort_version": key.dimfort_version,
        "lfortran_version": key.lfortran_version,
        "implicit_interface": key.implicit_interface,
        "mode": mode,
        "tree": tree,
    }
    _write_entry(entry_path, payload)
    return tree


# ---------------------------------------------------------------------------
# Module-file (.mod) cache
# ---------------------------------------------------------------------------


def load_mods_cached(
    source_path: Path,
    *,
    lfortran=None,
    implicit_interface: bool = False,
    cache_dir: Path | None,
) -> dict[str, bytes] | None:
    """Return the cached ``.mod`` bytes for every module declared by
    ``source_path``, or ``None`` on any cache miss.

    The returned mapping is ``{module_name: mod_file_bytes}``. The
    caller is expected to write each entry into the temp dir where
    LFortran will look for ``.mod`` files; that's how a cache hit
    skips ``lfortran -c`` for this source.

    Callers must NOT use this result unless every transitive
    dependency of this module was also restored from cache (or
    successfully recompiled). LFortran's ``.mod`` format embeds
    information about ``use``-d modules; a stale dep's ``.mod``
    will silently propagate.
    """
    from dimfort.core import lfortran as lf

    if cache_dir is None:
        return None
    abs_path = Path(source_path).resolve()
    try:
        file_bytes = abs_path.read_bytes()
    except OSError:
        return None
    key = _build_key(abs_path, file_bytes, lf, lfortran, implicit_interface)

    entry = _read_entry(_mods_entry_path(cache_dir, abs_path))
    if not entry or not _entry_matches(entry, key):
        return None
    mods_raw = entry.get("mods")
    if not isinstance(mods_raw, dict):
        return None
    try:
        return {
            name: base64.b64decode(b64.encode("ascii"))
            for name, b64 in mods_raw.items()
            if isinstance(name, str) and isinstance(b64, str)
        }
    except (ValueError, TypeError):
        return None


def save_mods_cached(
    source_path: Path,
    mods: dict[str, bytes],
    *,
    lfortran=None,
    implicit_interface: bool = False,
    cache_dir: Path | None,
) -> None:
    """Persist ``.mod`` bytes for every module declared by
    ``source_path``. ``cache_dir=None`` is a no-op."""
    from dimfort.core import lfortran as lf

    if cache_dir is None or not mods:
        return
    abs_path = Path(source_path).resolve()
    try:
        file_bytes = abs_path.read_bytes()
    except OSError:
        return
    key = _build_key(abs_path, file_bytes, lf, lfortran, implicit_interface)

    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "abs_path": key.abs_path,
        "content_sha256": key.content_sha256,
        "dimfort_version": key.dimfort_version,
        "lfortran_version": key.lfortran_version,
        "implicit_interface": key.implicit_interface,
        "mods": {
            name: base64.b64encode(data).decode("ascii")
            for name, data in mods.items()
        },
    }
    _write_entry(_mods_entry_path(cache_dir, abs_path), payload)
