"""LRU cap on the interactions report cache.

Per 0.2.6 plan item #16. The cache is keyed by ``(symbol_lc, scale)``
per ``WorksetResult`` identity and capped at ``_REPORT_CACHE_MAX``
entries (FIFO/LRU-with-move-to-end).
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

from dimfort.core.interactions import SymbolReport
from dimfort.lsp import interactions as _i


@dataclass
class _FakeResult:
    """Stand-in for ``WorksetResult`` — only identity matters here."""

    tag: str = ""


def _reset_cache() -> None:
    _i._report_cache_result = None
    _i._report_cache.clear()


def _stub_collect(result: object, symbol: str, *, scale: bool) -> SymbolReport:
    """Return a SymbolReport echoing ``symbol`` so tests can spot the cached value."""
    return SymbolReport(symbol=symbol)


def test_cache_evicts_oldest_at_cap() -> None:
    """Inserting MAX+1 distinct symbols evicts exactly one (the oldest)."""
    _reset_cache()
    result = _FakeResult()
    with mock.patch.object(_i, "collect_interactions", side_effect=_stub_collect):
        for i in range(_i._REPORT_CACHE_MAX):
            _i._get_cached_report(result, f"sym_{i:03d}", False)
        assert len(_i._report_cache) == _i._REPORT_CACHE_MAX
        # One more pushes us over → oldest (sym_000) gets evicted.
        _i._get_cached_report(result, "sym_new", False)
    assert len(_i._report_cache) == _i._REPORT_CACHE_MAX
    assert ("sym_000", False) not in _i._report_cache
    assert ("sym_new", False) in _i._report_cache
    _reset_cache()


def test_cache_hit_moves_to_end() -> None:
    """Recently-accessed entries survive eviction."""
    _reset_cache()
    result = _FakeResult()
    with mock.patch.object(_i, "collect_interactions", side_effect=_stub_collect):
        for i in range(_i._REPORT_CACHE_MAX):
            _i._get_cached_report(result, f"sym_{i:03d}", False)
        # Re-access the oldest → bumps it to most-recent.
        _i._get_cached_report(result, "sym_000", False)
        # Add one more → oldest is now sym_001, NOT sym_000.
        _i._get_cached_report(result, "sym_new", False)
    assert ("sym_000", False) in _i._report_cache  # survived
    assert ("sym_001", False) not in _i._report_cache  # evicted
    _reset_cache()


def test_cache_flushes_on_result_swap() -> None:
    """A fresh ``WorksetResult`` identity wipes prior entries."""
    _reset_cache()
    r1 = _FakeResult("first")
    r2 = _FakeResult("second")
    with mock.patch.object(_i, "collect_interactions", side_effect=_stub_collect):
        _i._get_cached_report(r1, "alpha", False)
        assert len(_i._report_cache) == 1
        _i._get_cached_report(r2, "beta", False)
    assert len(_i._report_cache) == 1
    assert ("alpha", False) not in _i._report_cache
    assert ("beta", False) in _i._report_cache
    _reset_cache()


def test_cache_key_is_case_insensitive() -> None:
    """Two casings of the same Fortran name collapse to one entry."""
    _reset_cache()
    result = _FakeResult()
    with mock.patch.object(
        _i, "collect_interactions", side_effect=_stub_collect,
    ) as mocked:
        _i._get_cached_report(result, "Alpha", False)
        _i._get_cached_report(result, "ALPHA", False)
        _i._get_cached_report(result, "alpha", False)
    assert len(_i._report_cache) == 1
    # Only the first call paid the compute cost.
    assert mocked.call_count == 1
    _reset_cache()


def test_overwrite_does_not_grow_cache() -> None:
    """Re-storing the same key keeps the cache size stable."""
    _reset_cache()
    result = _FakeResult()
    with mock.patch.object(_i, "collect_interactions", side_effect=_stub_collect):
        for _ in range(5):
            _i._get_cached_report(result, "samekey", True)
    assert len(_i._report_cache) == 1
    _reset_cache()
