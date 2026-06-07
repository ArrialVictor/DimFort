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
    ExportsKey,
    ModuleExportsCache,
    TreeCache,
    TreeKey,
    content_hash,
    cpp_fingerprint,
    digest_merged_var_units,
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
