"""Workset-adaptive cache cap + .dimfort.toml [cache] override.

Covers the machinery added on top of the LRU support from PR #74:
``set_max_entries`` trims live entries when shrunk, the config parser
accepts ``"auto"`` / int / rejects junk, and the LSP-side helper
``_apply_cache_max_entries`` honours both the adaptive default and a
user-pinned value.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.config import _from_raw
from dimfort.core.multifile_cache import (
    CachedParse,
    ProjectionCache,
    ProjectionKey,
    TreeCache,
    TreeKey,
)

# ---------------------------------------------------------------------------
# set_max_entries on the cache classes
# ---------------------------------------------------------------------------


def _fake_parse() -> CachedParse:
    return CachedParse(tree=None, source=b"")  # type: ignore[arg-type]


def test_tree_cache_shrinks_to_new_cap() -> None:
    """Lowering ``max_entries`` evicts oldest entries until size fits."""
    cache = TreeCache()
    for i in range(20):
        cache.put(TreeKey(content_hash=f"{i:04d}", parse_mode="raw"), _fake_parse())
    assert len(cache) == 20
    cache.set_max_entries(5)
    assert len(cache) == 5
    # The five most-recent inserts (0015..0019) survive (FIFO eviction).
    assert cache.get(TreeKey(content_hash="0019", parse_mode="raw")) is not None
    assert cache.get(TreeKey(content_hash="0015", parse_mode="raw")) is not None
    assert cache.get(TreeKey(content_hash="0014", parse_mode="raw")) is None


def test_tree_cache_set_max_entries_none_removes_cap() -> None:
    """Setting cap to ``None`` makes the cache unbounded again."""
    cache = TreeCache(max_entries=5)
    for i in range(10):
        cache.put(TreeKey(content_hash=f"{i:04d}", parse_mode="raw"), _fake_parse())
    assert len(cache) == 5
    cache.set_max_entries(None)
    for i in range(20, 50):
        cache.put(TreeKey(content_hash=f"{i:04d}", parse_mode="raw"), _fake_parse())
    assert len(cache) == 35  # 5 survivors + 30 fresh, no eviction


def test_projection_cache_shrinks_to_new_cap() -> None:
    """Same eviction-on-shrink contract on ProjectionCache."""
    cache = ProjectionCache()
    for i in range(50):
        cache.put(
            ProjectionKey(content_hash=f"{i:04d}", patterns_fp="x"),
            object(),  # type: ignore[arg-type]
        )
    assert len(cache) == 50
    cache.set_max_entries(10)
    assert len(cache) == 10


# ---------------------------------------------------------------------------
# .dimfort.toml [cache] max_entries parsing
# ---------------------------------------------------------------------------


def _parse(raw: dict, tmp_path: Path):
    return _from_raw(raw, tmp_path / ".dimfort.toml")


def test_config_max_entries_auto(tmp_path: Path) -> None:
    """``"auto"`` keeps the field as ``None`` so the LSP picks adaptive."""
    cfg = _parse({"cache": {"max_entries": "auto"}}, tmp_path)
    assert cfg.cache_max_entries is None


def test_config_max_entries_integer(tmp_path: Path) -> None:
    """A positive integer pins the cap."""
    cfg = _parse({"cache": {"max_entries": 16384}}, tmp_path)
    assert cfg.cache_max_entries == 16384


def test_config_max_entries_absent(tmp_path: Path) -> None:
    """Absent key falls through to ``None`` (adaptive)."""
    cfg = _parse({}, tmp_path)
    assert cfg.cache_max_entries is None


@pytest.mark.parametrize("bad", ["nope", -1, 0, True, False, 3.5])
def test_config_max_entries_rejects_junk(
    bad: object, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-int, non-``"auto"`` values fall back to ``None`` with a warning."""
    cfg = _parse({"cache": {"max_entries": bad}}, tmp_path)
    assert cfg.cache_max_entries is None
    assert any("max_entries" in r.message for r in caplog.records)


def test_config_max_entries_warns_below_floor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A pinned cap below 1000 is accepted but warned about."""
    cfg = _parse({"cache": {"max_entries": 100}}, tmp_path)
    assert cfg.cache_max_entries == 100
    assert any("below the recommended floor" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _apply_cache_max_entries — adaptive sizing on the LSP side
# ---------------------------------------------------------------------------


def test_apply_uses_adaptive_default_floor() -> None:
    """Tiny workset → cap floors at 4096 (not 4× the workset)."""
    pytest.importorskip("pygls")
    from dimfort.config import DimfortConfig
    from dimfort.lsp import server as _s
    from dimfort.lsp.state import state

    state.observed_max_workset_size = 0
    state.project_config = DimfortConfig()
    _s._apply_cache_max_entries(5)
    assert state.observed_max_workset_size == 5
    assert state.tree_cache is not None
    assert state.tree_cache._max_entries == 4096


def test_apply_uses_adaptive_scales_with_workset() -> None:
    """Large workset → cap = workset × 4."""
    pytest.importorskip("pygls")
    from dimfort.config import DimfortConfig
    from dimfort.lsp import server as _s
    from dimfort.lsp.state import state

    state.observed_max_workset_size = 0
    state.project_config = DimfortConfig()
    _s._apply_cache_max_entries(2435)
    assert state.tree_cache is not None
    assert state.tree_cache._max_entries == 2435 * 4


def test_apply_is_sticky_on_high_watermark() -> None:
    """A smaller follow-up check does NOT shrink the cap."""
    pytest.importorskip("pygls")
    from dimfort.config import DimfortConfig
    from dimfort.lsp import server as _s
    from dimfort.lsp.state import state

    state.observed_max_workset_size = 0
    state.project_config = DimfortConfig()
    _s._apply_cache_max_entries(2435)
    big = state.tree_cache._max_entries  # type: ignore[union-attr]
    _s._apply_cache_max_entries(3)
    assert state.tree_cache._max_entries == big  # type: ignore[union-attr]
    assert state.observed_max_workset_size == 2435


def test_apply_honours_user_pin() -> None:
    """An explicit ``cache_max_entries`` overrides the adaptive default."""
    pytest.importorskip("pygls")
    from dimfort.config import DimfortConfig
    from dimfort.lsp import server as _s
    from dimfort.lsp.state import state

    state.observed_max_workset_size = 0
    state.project_config = DimfortConfig(cache_max_entries=20000)
    _s._apply_cache_max_entries(2435)
    assert state.tree_cache is not None
    assert state.tree_cache._max_entries == 20000
    # And the user pin wins even when adaptive would prefer a larger value.
    _s._apply_cache_max_entries(100000)
    assert state.tree_cache._max_entries == 20000
