"""Tests for the on-disk content-hash cache store."""
from __future__ import annotations

import gzip
import os
import time
from pathlib import Path

from dimfort.core.cache_store import CacheStore

KEY_A = "a" * 64
KEY_B = "b" * 64
PAYLOAD = {"hello": "world", "n": 42, "list": [1, 2, 3]}


def _store(tmp_path: Path, **kwargs) -> CacheStore:
    return CacheStore(root=tmp_path / ".dimfort-cache", **kwargs)


def test_roundtrip(tmp_path: Path):
    s = _store(tmp_path)
    assert s.read(KEY_A) is None
    s.write(KEY_A, PAYLOAD)
    assert s.read(KEY_A) == PAYLOAD


def test_miss_increments_misses(tmp_path: Path):
    s = _store(tmp_path)
    s.read(KEY_A)
    s.read(KEY_B)
    assert s.misses == 2
    assert s.hits == 0


def test_hit_increments_hits(tmp_path: Path):
    s = _store(tmp_path)
    s.write(KEY_A, PAYLOAD)
    s.read(KEY_A)
    s.read(KEY_A)
    assert s.hits == 2


def test_shard_layout(tmp_path: Path):
    s = _store(tmp_path)
    s.write(KEY_A, PAYLOAD)
    shard = tmp_path / ".dimfort-cache" / f"v{s.output_version}" / "aa"
    assert shard.is_dir()
    assert (shard / ("a" * 62 + ".json.gz")).is_file()


def test_atomic_write_no_temp_left_behind(tmp_path: Path):
    s = _store(tmp_path)
    s.write(KEY_A, PAYLOAD)
    leftover = list((tmp_path / ".dimfort-cache").rglob(".tmp-*"))
    assert leftover == []


def test_overwrite(tmp_path: Path):
    s = _store(tmp_path)
    s.write(KEY_A, {"v": 1})
    s.write(KEY_A, {"v": 2})
    assert s.read(KEY_A) == {"v": 2}


def test_corrupt_entry_treated_as_miss(tmp_path: Path):
    s = _store(tmp_path)
    s.write(KEY_A, PAYLOAD)
    # Corrupt the file in place.
    path = s._entry_path(KEY_A)
    path.write_bytes(b"not a gzip")
    assert s.read(KEY_A) is None
    # Corrupt entry is removed so the slot is reusable.
    assert not path.exists()


def test_clear(tmp_path: Path):
    s = _store(tmp_path)
    s.write(KEY_A, PAYLOAD)
    s.write(KEY_B, PAYLOAD)
    s.clear()
    assert s.read(KEY_A) is None
    assert s.read(KEY_B) is None


def test_clear_when_missing(tmp_path: Path):
    s = _store(tmp_path)
    # Cache dir never created — clear() should still be safe.
    s.clear()


def test_prune_age(tmp_path: Path):
    s = _store(tmp_path, max_age_days=0)  # immediately stale
    s.write(KEY_A, PAYLOAD)
    # Backdate so prune notices.
    path = s._entry_path(KEY_A)
    old = time.time() - 86400
    os.utime(path, (old, old))
    removed = s.prune()
    assert removed == 1
    assert s.read(KEY_A) is None


def test_prune_size(tmp_path: Path):
    # Tiny size limit forces eviction.
    s = _store(tmp_path, size_limit_bytes=1)
    s.write(KEY_A, PAYLOAD)
    s.write(KEY_B, PAYLOAD)
    s.prune()
    # At least one entry got evicted — possibly both, depending on
    # mtime resolution. Asserting "≤ 1 remaining" matches semantics
    # without over-specifying.
    remaining = sum(
        1 for _ in (tmp_path / ".dimfort-cache").rglob("*.json.gz")
    )
    assert remaining <= 1


def test_write_then_read_gzip_content(tmp_path: Path):
    # Sanity-check that the file is actually gzip-encoded JSON, not
    # plain text — keeps the on-disk format honest.
    s = _store(tmp_path)
    s.write(KEY_A, PAYLOAD)
    raw = s._entry_path(KEY_A).read_bytes()
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    body = gzip.decompress(raw)
    import json
    assert json.loads(body) == PAYLOAD
