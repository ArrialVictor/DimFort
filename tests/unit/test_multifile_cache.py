"""Tests for the session-scoped multifile caches.

Module-level tests for the dataclasses + hashing helpers. End-to-end
tests covering the ``check_files`` wiring live alongside the call-site
changes (added in the follow-up commit).
"""
from __future__ import annotations

from pathlib import Path

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
