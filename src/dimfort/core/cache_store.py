"""On-disk store for the content-hash workspace cache.

Layout
------
::

    {root}/v{CHECKER_OUTPUT_VERSION}/{first2}/{rest_of_hash}.json.gz

``{first2}`` is the first two hex chars of the key. With ~2,400 files
in LMDZ and uniformly-distributed SHA-256 keys, each shard holds <10
entries, which is friendly to every filesystem.

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
    """Workspace-local cache: ``{workspace_root}/.dimfort-cache``."""
    return Path(workspace_root) / DEFAULT_CACHE_DIR_NAME


@dataclass
class CacheStore:
    """Read/write/prune entries on disk.

    Construct once per workspace check; reuse across all file
    lookups in that run.
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
        return self.root / f"v{self.output_version}"

    def _entry_path(self, key: str) -> Path:
        return self.shard_root() / key[:2] / f"{key[2:]}.json.gz"

    def read(self, key: str) -> dict[str, Any] | None:
        """Return the cached payload for ``key`` or ``None``.

        Any read error (corrupt gzip, malformed JSON, missing file) is
        treated as a miss. Corrupted entries are removed so the next
        write fills the slot cleanly.
        """
        path = self._entry_path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            with gzip.open(path, "rb") as fh:
                payload = json.loads(fh.read().decode())
            self.hits += 1
            return payload
        except (OSError, ValueError, json.JSONDecodeError):
            # Best-effort cleanup; don't propagate.
            try:
                path.unlink()
            except OSError:
                pass
            self.misses += 1
            return None

    def write(self, key: str, payload: dict[str, Any]) -> None:
        """Write ``payload`` for ``key`` atomically.

        A temp file is written in the same directory then renamed
        into place. Concurrent writers from another process race
        harmlessly — the last writer wins and content is byte-equal.
        """
        path = self._entry_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, separators=(",", ":")).encode()
        # NamedTemporaryFile with delete=False so we can rename.
        fd, tmp_path_str = tempfile.mkstemp(
            prefix=".tmp-", suffix=".json.gz", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb") as gz:
                    gz.write(body)
            os.replace(tmp_path_str, path)
            self.writes += 1
        except Exception:
            # Clean up the temp file on any failure.
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        """Remove every cached entry. Safe to call when the dir is missing."""
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    # ---- pruning ----------------------------------------------------------

    def prune(self) -> int:
        """Drop too-old entries, then trim by size if still over the limit.

        Returns the number of files removed. Pruning is best-effort:
        permission errors and concurrent removals are swallowed.
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
