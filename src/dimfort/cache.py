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
    from dimfort import __version__
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

    content_sha = _sha256_bytes(file_bytes)
    lf_version = lf.cached_version(lfortran)
    key = _Key(
        abs_path=str(abs_path),
        content_sha256=content_sha,
        dimfort_version=__version__,
        lfortran_version=lf_version,
        implicit_interface=implicit_interface,
    )

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
