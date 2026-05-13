"""Per-project analysis cache.

Layout: ``<cache_dir>/<rel-path>.json`` where the relative path mirrors
the source tree under the project root. Each entry is keyed by source
mtime + content hash + dimfort version + lfortran version; mismatches
trigger a re-parse.

The cache is internal but the on-disk schema is documented in
``docs/cache-format.md`` for third-party consumers.
"""
from __future__ import annotations

import shutil
from pathlib import Path

DEFAULT_CACHE_DIRNAME = ".dimfort"


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
