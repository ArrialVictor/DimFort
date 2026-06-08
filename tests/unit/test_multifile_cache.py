"""Tests for the session-scoped multifile caches.

Module-level tests cover the dataclasses + hashing helpers; the
``check_files``-driven tests exercise the wiring end-to-end (cache hit
skips parse, content edit invalidates, no-cache parity).
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from dimfort.core import (
    ts_checker,
    unit_config,  # noqa: F401  (installs DEFAULT_TABLE)
)
from dimfort.core import ts_parser as _ts
from dimfort.core.multifile import check_files
from dimfort.core.multifile_cache import (
    CachedParse,
    CachedProjection,
    ExportsKey,
    ModuleExportsCache,
    ProjectionCache,
    ProjectionKey,
    TreeCache,
    TreeKey,
    content_hash,
    cpp_fingerprint,
    digest_merged_var_units,
    patterns_fingerprint,
)

SAMPLE = """\
module mymod
  implicit none
  !@unit{m}
  real :: x
end module mymod
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p.resolve()


def test_content_hash_stable():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert content_hash(b"abc") != content_hash(b"abd")


def test_cpp_fingerprint_changes_with_defines():
    a = cpp_fingerprint(("-DFOO",), ())
    b = cpp_fingerprint(("-DBAR",), ())
    assert a != b


def test_cpp_fingerprint_changes_with_includes():
    a = cpp_fingerprint((), (Path("/a"),))
    b = cpp_fingerprint((), (Path("/b"),))
    assert a != b


def test_tree_cache_get_put_roundtrip():
    cache = TreeCache()
    key = TreeKey("h", "raw")
    assert cache.get(key) is None
    sentinel = CachedParse(tree=object(), source=b"x")  # type: ignore[arg-type]
    cache.put(key, sentinel)
    assert cache.get(key) is sentinel
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


def test_exports_cache_get_put_roundtrip():
    cache = ModuleExportsCache()
    key = ExportsKey(content_hash="abc", merged_units_digest="def")
    assert cache.get(key) is None
    sentinel = ({}, None)
    cache.put(key, sentinel)
    assert cache.get(key) is sentinel
    cache.clear()
    assert cache.get(key) is None


def test_digest_merged_var_units_stable_and_order_independent():
    a = digest_merged_var_units({"x": "m/s", "y": "K"})
    b = digest_merged_var_units({"y": "K", "x": "m/s"})
    c = digest_merged_var_units({"x": "m/s", "y": "kg"})
    assert a == b
    assert a != c


def test_check_files_without_cache_is_unchanged(tmp_path: Path):
    """Baseline: passing no cache must not change the result shape."""
    src = _write(tmp_path, "a.f90", SAMPLE)
    r1 = check_files([src])
    r2 = check_files([src])
    assert set(r1.diagnostics) == set(r2.diagnostics)
    assert set(r1.trees) == set(r2.trees)


def test_tree_cache_skips_parse_on_hit(tmp_path: Path):
    """A warm pass must perform strictly fewer ``parse_text`` calls.

    ``_load_one`` parses once; ``scan_text`` parses once more (independent
    code path). The TreeCache only skips the ``_load_one`` parse, so warm
    < cold rather than warm == 0. Eliminating the scan-internal parse is
    tracked as a follow-up in ``docs/design/future/multifile-cache.md``.
    """
    src = _write(tmp_path, "a.f90", SAMPLE)
    cache = TreeCache()
    real_parse = _ts.parse_text

    cold_calls = {"n": 0}
    with mock.patch.object(
        _ts, "parse_text",
        side_effect=lambda t: (cold_calls.__setitem__("n", cold_calls["n"] + 1)
                               or real_parse(t)),
    ):
        check_files([src], tree_cache=cache)
    assert len(cache) == 1

    warm_calls = {"n": 0}
    with mock.patch.object(
        _ts, "parse_text",
        side_effect=lambda t: (warm_calls.__setitem__("n", warm_calls["n"] + 1)
                               or real_parse(t)),
    ):
        check_files([src], tree_cache=cache)
    assert warm_calls["n"] < cold_calls["n"], (
        f"cache should reduce parse calls (cold={cold_calls['n']}, "
        f"warm={warm_calls['n']})"
    )


def test_tree_cache_invalidates_on_content_change(tmp_path: Path):
    src = _write(tmp_path, "a.f90", SAMPLE)
    cache = TreeCache()
    check_files([src], tree_cache=cache)
    assert len(cache) == 1

    # Edit the file: content hash changes → cache miss for this file.
    src.write_text(SAMPLE + "\n! tail comment\n")
    call_count = {"n": 0}
    real_parse = _ts.parse_text

    def counting_parse(text):
        call_count["n"] += 1
        return real_parse(text)

    with mock.patch.object(_ts, "parse_text", side_effect=counting_parse):
        check_files([src], tree_cache=cache)
    assert call_count["n"] >= 1, "edited file must trigger a fresh parse"
    # Cache now holds both the old and the new entry.
    assert len(cache) == 2


def test_exports_cache_skips_index_walk_on_hit(tmp_path: Path):
    """Warm pass with an ``exports_cache`` must not re-invoke the index walker."""
    src = _write(tmp_path, "a.f90", SAMPLE)
    tree_cache = TreeCache()
    exports_cache = ModuleExportsCache()
    check_files([src], tree_cache=tree_cache, exports_cache=exports_cache)
    assert len(exports_cache) == 1

    with mock.patch.object(
        ts_checker,
        "collect_function_signatures_and_module_exports",
        side_effect=AssertionError("index walk should be skipped"),
    ) as spy:
        check_files([src], tree_cache=tree_cache, exports_cache=exports_cache)
    spy.assert_not_called()


def test_exports_cache_invalidates_on_content_change(tmp_path: Path):
    src = _write(tmp_path, "a.f90", SAMPLE)
    exports_cache = ModuleExportsCache()
    check_files([src], exports_cache=exports_cache)
    assert len(exports_cache) == 1

    src.write_text(SAMPLE.replace("mymod", "renamed_mod"))
    check_files([src], exports_cache=exports_cache)
    # Both old and new entries now sit in the cache.
    assert len(exports_cache) == 2


def test_parsed_units_memo_persists_across_calls(tmp_path: Path):
    """ModuleExportsCache.parsed_units_memo retains parsed dicts."""
    src = _write(tmp_path, "a.f90", SAMPLE)
    exports_cache = ModuleExportsCache()
    check_files([src], exports_cache=exports_cache)
    assert exports_cache.parsed_units_memo, "memo should be populated"
    # Second call hits the memo; values must round-trip equal.
    snapshot = dict(exports_cache.parsed_units_memo)
    check_files([src], exports_cache=exports_cache)
    for key, parsed in snapshot.items():
        # Identity check — the memo returns the cached dict directly.
        assert exports_cache.parsed_units_memo[key] is parsed


def test_digest_memo_persists_across_calls(tmp_path: Path):
    """ModuleExportsCache.digest_memo accumulates across check_files calls."""
    src = _write(tmp_path, "a.f90", SAMPLE)
    exports_cache = ModuleExportsCache()
    import tempfile

    from dimfort.core.cache_store import CacheStore
    cache_root = Path(tempfile.mkdtemp())
    cstore = CacheStore(root=cache_root)
    check_files([src], exports_cache=exports_cache,
                cache=cstore, cache_mode="read-write")
    first = dict(exports_cache.digest_memo)
    check_files([src], exports_cache=exports_cache,
                cache=cstore, cache_mode="read-write")
    # Same module-export objects → memo entries persist (we never see
    # them recomputed). Use the first call's id-keys as the proof.
    for k in first:
        assert k in exports_cache.digest_memo


def test_outer_lock_yields_periodically(tmp_path: Path):
    """check_files releases + re-acquires outer_lock every yield_every files.

    Synthesises 12 files so two yield windows fire when ``lock_yield_every=5``
    (after files 5 and 10). Wraps a real ``threading.Lock`` in a counting
    proxy because the builtin lock methods are read-only and can't be
    monkey-patched directly.
    """
    import threading

    class CountingLock:
        """threading.Lock-shaped proxy that tallies release/acquire calls."""

        def __init__(self):
            self._lock = threading.Lock()
            self.releases = 0
            self.acquires = 0

        def acquire(self, *args, **kwargs):
            self.acquires += 1
            return self._lock.acquire(*args, **kwargs)

        def release(self):
            self.releases += 1
            self._lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc):
            self.release()

    files = [
        _write(tmp_path, f"f{i}.f90", SAMPLE.replace("mymod", f"mod_{i}"))
        for i in range(12)
    ]
    proxy = CountingLock()

    with proxy:
        check_files(files, outer_lock=proxy, lock_yield_every=5)

    # Two yield windows (after files 5 and 10) — each releases + reacquires
    # once. Plus the initial caller-side acquire (+1) and final release (+1).
    assert proxy.releases >= 3, f"expected >=3 releases, got {proxy.releases}"
    assert proxy.acquires >= 3, f"expected >=3 acquires, got {proxy.acquires}"


def test_tree_cache_isolation_across_two_files(tmp_path: Path):
    """Two distinct files in one workset produce two distinct cache entries."""
    a = _write(tmp_path, "a.f90", SAMPLE)
    b = _write(
        tmp_path,
        "b.f90",
        SAMPLE.replace("mymod", "othermod").replace(":: x", ":: y"),
    )
    cache = TreeCache()
    check_files([a, b], tree_cache=cache)
    assert len(cache) == 2


def test_patterns_fingerprint_changes_with_unit_patterns():
    from dimfort.core.unit_patterns import (
        DEFAULT_AFFINE_PATTERNS,
        DEFAULT_ASSUME_PATTERNS,
        DEFAULT_UNIT_PATTERNS,
        UnitPattern,
    )
    a = patterns_fingerprint(
        DEFAULT_UNIT_PATTERNS, DEFAULT_ASSUME_PATTERNS, DEFAULT_AFFINE_PATTERNS,
    )
    custom = (UnitPattern(open="<!u{", close="}>"),) + DEFAULT_UNIT_PATTERNS
    b = patterns_fingerprint(
        custom, DEFAULT_ASSUME_PATTERNS, DEFAULT_AFFINE_PATTERNS,
    )
    assert a != b


def test_projection_cache_get_put_roundtrip():
    cache = ProjectionCache()
    key = ProjectionKey(content_hash="abc", patterns_fp="def")
    assert cache.get(key) is None
    sentinel = CachedProjection(
        scan=object(),  # type: ignore[arg-type]
        attachment=object(),  # type: ignore[arg-type]
    )
    cache.put(key, sentinel)
    assert cache.get(key) is sentinel
    assert len(cache) == 1
    cache.clear()
    assert len(cache) == 0


def test_projection_cache_skips_scan_and_attach_on_hit(tmp_path: Path):
    """A warm pass must not re-invoke scan_text or attach.

    Cache miss on the first call populates entries; the second pass
    with the same content + projection_cache must skip both walks.
    """
    from dimfort.core import annotations as _ann
    from dimfort.core import attach as _att

    src = _write(tmp_path, "p.f90", SAMPLE)
    cache = ProjectionCache()

    check_files([src], projection_cache=cache)
    assert len(cache) == 1

    with mock.patch.object(
        _ann, "scan_text", side_effect=AssertionError("scan should be skipped"),
    ) as scan_spy, mock.patch.object(
        _att, "attach", side_effect=AssertionError("attach should be skipped"),
    ) as attach_spy:
        check_files([src], projection_cache=cache)
    scan_spy.assert_not_called()
    attach_spy.assert_not_called()


def test_projection_cache_invalidates_on_content_change(tmp_path: Path):
    src = _write(tmp_path, "p.f90", SAMPLE)
    cache = ProjectionCache()
    check_files([src], projection_cache=cache)
    assert len(cache) == 1

    src.write_text(SAMPLE + "\n! trailer\n")
    check_files([src], projection_cache=cache)
    # Both old and new content hashes now sit in the cache.
    assert len(cache) == 2


def test_projection_cache_invalidates_on_patterns_change(tmp_path: Path):
    """Different ``unit_patterns`` → different ProjectionKey → cache miss."""
    from dimfort.core.unit_patterns import (
        DEFAULT_AFFINE_PATTERNS,
        DEFAULT_ASSUME_PATTERNS,
        DEFAULT_UNIT_PATTERNS,
        UnitPattern,
    )

    src = _write(tmp_path, "p.f90", SAMPLE)
    cache = ProjectionCache()
    check_files([src], projection_cache=cache)
    assert len(cache) == 1

    custom_patterns = (
        UnitPattern(open="<!u{", close="}>"),
    ) + DEFAULT_UNIT_PATTERNS
    check_files(
        [src],
        unit_patterns=custom_patterns,
        assume_patterns=DEFAULT_ASSUME_PATTERNS,
        affine_patterns=DEFAULT_AFFINE_PATTERNS,
        projection_cache=cache,
    )
    # Same content hash + different patterns fingerprint → second
    # entry alongside the first.
    assert len(cache) == 2
