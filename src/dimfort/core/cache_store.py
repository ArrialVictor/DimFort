"""On-disk store for the content-hash workspace cache.

Layout
------
::

    {root}/v{CHECKER_OUTPUT_VERSION}/{first2}/{rest_of_hash}.json.gz

``{first2}`` is the first two hex chars of the key. With ~2,400 files
in a representative workspace and uniformly-distributed SHA-256 keys,
each shard holds <10 entries, which is friendly to every filesystem.

Concurrency
-----------
Reads are lock-free: entries are immutable, atomic writes prevent
half-written content from being read. Writes go to a temp file and
``os.replace`` into place — a duplicate write from a concurrent
process just overwrites with byte-identical content.

Pruning
-------
LRU sweep keyed on file ``mtime``: whenever total size exceeds
:attr:`size_limit_bytes`, oldest files are removed until under the
limit. Files older than :attr:`max_age_days` are dropped regardless
of size. The sweep is best-effort; a missed sweep just defers
reclamation, it doesn't break correctness.
"""
from __future__ import annotations

import contextlib
import gzip
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dimfort.core.cache_key import CHECKER_OUTPUT_VERSION

DEFAULT_CACHE_DIR_NAME = ".dimfort-cache"
DEFAULT_SIZE_LIMIT_BYTES = 500 * 1024 * 1024  # 500 MB
DEFAULT_MAX_AGE_DAYS = 30


def default_cache_dir(workspace_root: str | Path) -> Path:
    """Return the workspace-local cache directory.

    Args:
        workspace_root: Path of the workspace root.

    Returns:
        ``{workspace_root}/.dimfort-cache``.
    """
    return Path(workspace_root) / DEFAULT_CACHE_DIR_NAME


@dataclass
class CacheStore:
    """Read/write/prune entries on disk.

    Construct once per workspace check; reuse across all file
    lookups in that run.

    Attributes:
        root: Cache directory root (typically ``.dimfort-cache`` under
            the workspace).
        size_limit_bytes: Soft cap enforced by :meth:`prune`; defaults
            to 500 MB.
        max_age_days: Entries older than this many days are dropped on
            :meth:`prune` regardless of size; defaults to 30.
        output_version: Shard version directory (``v{N}``) under
            :attr:`root`; defaults to
            :data:`cache_key.CHECKER_OUTPUT_VERSION`.
        hits: Number of successful :meth:`read` lookups in this run.
        misses: Number of unsuccessful :meth:`read` lookups (missing
            or corrupt entries).
        writes: Number of :meth:`write` calls that succeeded.
    """

    root: Path
    size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES
    max_age_days: int = DEFAULT_MAX_AGE_DAYS
    output_version: int = CHECKER_OUTPUT_VERSION

    # Runtime stats — caller can inspect after a run for --timings.
    hits: int = field(default=0, init=False)
    misses: int = field(default=0, init=False)
    writes: int = field(default=0, init=False)

    def shard_root(self) -> Path:
        """Return the version-shard directory (``{root}/v{output_version}``)."""
        return self.root / f"v{self.output_version}"

    def _entry_path(self, key: str) -> Path:
        """Return the on-disk path for an entry keyed by ``key``.

        Args:
            key: Hex SHA-256 cache key.

        Returns:
            ``{shard_root}/{key[:2]}/{key[2:]}.json.gz``.
        """
        return self.shard_root() / key[:2] / f"{key[2:]}.json.gz"

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the cached payload for ``key`` or ``None``.

        Increments :attr:`hits` or :attr:`misses` as a side effect.

        Args:
            key: Hex SHA-256 cache key.

        Returns:
            The decoded JSON payload on a hit, or ``None`` on a miss.
            Any read error (corrupt gzip, malformed JSON, missing
            file) is treated as a miss; corrupted entries are removed
            so the next write fills the slot cleanly.
        """
        path = self._entry_path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            with gzip.open(path, "rb") as fh:
                payload: dict[str, Any] = json.loads(fh.read().decode())
            self.hits += 1
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            # Best-effort cleanup; don't propagate.
            with contextlib.suppress(OSError):
                path.unlink()
            self.misses += 1
            return None

    def write(self, key: str, payload: dict[str, Any]) -> None:
        """Write ``payload`` for ``key`` atomically.

        A temp file is written in the same directory then renamed
        into place. Concurrent writers from another process race
        harmlessly — the last writer wins and content is byte-equal.
        Increments :attr:`writes` on success.

        Args:
            key: Hex SHA-256 cache key.
            payload: JSON-serialisable dict to store under ``key``.

        Raises:
            Exception: Any exception raised during temp-file creation,
                gzip write, or rename is re-raised after the temp file
                is best-effort unlinked.
        """
        path = self._entry_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, separators=(",", ":")).encode()
        # mkstemp returns an open fd; we close it via os.fdopen
        # below, then rename the file into place atomically.
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".tmp-", suffix=".json.gz", dir=str(path.parent),
        )
        try:
            with (
                os.fdopen(fd, "wb") as raw,
                gzip.GzipFile(fileobj=raw, mode="wb") as gz,
            ):
                gz.write(body)
            os.replace(tmp_path_str, path)
            self.writes += 1
        except Exception:
            # Clean up the temp file on any failure.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path_str)
            raise

    def clear(self) -> None:
        """Remove every cached entry.

        Safe to call when the cache directory is missing. Errors during
        removal are swallowed (best-effort).
        """
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    # ---- pruning ----------------------------------------------------------

    def prune(self) -> int:
        """Drop too-old entries, then trim by size if still over the limit.

        Pruning is best-effort: permission errors and concurrent
        removals are swallowed.

        Returns:
            Total number of files removed (age-based + size-based).
            Zero when the cache directory does not exist.
        """
        if not self.root.exists():
            return 0
        removed = 0
        entries: list[tuple[float, int, Path]] = []  # (mtime, size, path)
        cutoff_ts = time.time() - self.max_age_days * 86400.0
        for path in self.root.rglob("*.json.gz"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff_ts:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
                continue
            entries.append((st.st_mtime, st.st_size, path))

        total = sum(sz for _, sz, _ in entries)
        if total <= self.size_limit_bytes:
            return removed

        # Oldest first — drop until under the limit.
        entries.sort(key=lambda t: t[0])
        for _mtime, size, path in entries:
            if total <= self.size_limit_bytes:
                break
            try:
                path.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
        return removed
