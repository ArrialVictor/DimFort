"""End-to-end tests for the content-hash cache wired into check_files.

These tests exercise the read-only and read-write cache modes against
a small fixture workspace. They guard the invariant the stress test
(step 7) makes definitive: cached runs produce diagnostics identical
to cold runs.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core.cache_store import CacheStore
from dimfort.core.multifile import check_files


def _make_pair(tmp_path: Path) -> tuple[Path, Path]:
    producer = tmp_path / "producer.f90"
    producer.write_text(
        "module producer_mod\n"
        "  real :: x  ! @unit{m/s}\n"
        "end module\n"
    )
    consumer = tmp_path / "consumer.f90"
    consumer.write_text(
        "subroutine s\n"
        "  use producer_mod\n"
        "  real :: y  ! @unit{m/s}\n"
        "  y = x\n"
        "end subroutine\n"
    )
    return producer, consumer


def _diag_keys(result, path: Path) -> list[tuple]:
    """Project diagnostics to a comparable tuple form."""
    return sorted(
        (d.code, d.severity.value, d.start.line, d.start.column, d.message)
        for d in result.diagnostics.get(path, [])
    )


def test_cold_run_with_cache_writes_entries(tmp_path: Path):
    producer, consumer = _make_pair(tmp_path)
    cache = CacheStore(root=tmp_path / ".dimfort-cache")
    result = check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    # Two files checked → two writes; nothing was hit on a cold run.
    assert result.cache_writes == 2
    assert result.cache_hits == 0


def test_warm_run_serves_from_cache(tmp_path: Path):
    producer, consumer = _make_pair(tmp_path)
    cache = CacheStore(root=tmp_path / ".dimfort-cache")
    cold = check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    warm = check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    assert warm.cache_hits == 2
    assert warm.cache_misses == 0
    # Cached diagnostics must match the cold-run output exactly.
    for f in (producer, consumer):
        assert _diag_keys(cold, f) == _diag_keys(warm, f)


def test_edit_invalidates_only_edited_file(tmp_path: Path):
    producer, consumer = _make_pair(tmp_path)
    cache = CacheStore(root=tmp_path / ".dimfort-cache")
    check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    # Edit consumer in a way that doesn't change its observable output
    # (add a blank line at top) — should be a cache *miss* on consumer
    # only, producer still hits.
    consumer.write_text("\n" + consumer.read_text())
    warm = check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    assert warm.cache_hits == 1
    assert warm.cache_misses == 1


def test_edit_producer_invalidates_consumer_via_deps(tmp_path: Path):
    producer, consumer = _make_pair(tmp_path)
    cache = CacheStore(root=tmp_path / ".dimfort-cache")
    check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    # Rename the producer's exported variable. ModuleExports.all_var_names
    # changes, which changes the export digest and invalidates consumer.
    producer.write_text(
        "module producer_mod\n"
        "  real :: x_renamed  ! @unit{m/s}\n"
        "end module\n"
    )
    warm = check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    # producer: self-hash changed → cache_miss (counted as miss).
    # consumer: self-hash unchanged → cache_hit candidate, but dep
    #           signature differs → flagged dirty.
    assert warm.cache_misses == 1
    assert warm.cache_dirty == 1
    assert warm.cache_hits == 0


def test_read_only_mode_writes_nothing(tmp_path: Path):
    producer, consumer = _make_pair(tmp_path)
    cache = CacheStore(root=tmp_path / ".dimfort-cache")
    result = check_files(
        [producer, consumer], cache=cache, cache_mode="read-only",
    )
    assert result.cache_writes == 0
    # On a cold, read-only run nothing is in the cache.
    assert result.cache_misses == 2


def test_off_mode_bypasses_cache_entirely(tmp_path: Path):
    producer, consumer = _make_pair(tmp_path)
    cache = CacheStore(root=tmp_path / ".dimfort-cache")
    # Populate the cache first so we'd hit it if cache_mode honoured it.
    check_files(
        [producer, consumer], cache=cache, cache_mode="read-write",
    )
    # Now ask for "off" — counters must all be zero.
    result = check_files(
        [producer, consumer], cache=cache, cache_mode="off",
    )
    assert result.cache_hits == 0
    assert result.cache_misses == 0
    assert result.cache_writes == 0
