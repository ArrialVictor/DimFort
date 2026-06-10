"""Per-URI cache hygiene for LSP modules.

Covers the (uri, version)-keyed caches that pair with
``server._forget_uri`` for didClose eviction. Today: the inlay tables
cache and the decl_scan declarations cache.

The checks are deliberately minimal — they exercise the eviction
contract without spinning up a full LSP server. Resolver behaviour
under cached state is exercised by the existing in-editor smoke
walks per the standing pre-publish rule.
"""
from __future__ import annotations

from dimfort.lsp import decl_scan, inlay


def _prime_inlay(uri: str, version: int) -> None:
    """Stash an arbitrary entry under ``uri`` in the inlay tables cache."""
    inlay._tables_cache[uri] = (version, {}, {}, {})


def _prime_decl_scan(uri: str, version: int) -> None:
    """Stash an arbitrary entry under ``uri`` in the decl-scan cache."""
    decl_scan._uri_scan_cache[uri] = (version, ())


def test_inlay_forget_uri_evicts() -> None:
    """``forget_uri`` drops the matching entry; unrelated entries untouched."""
    _prime_inlay("file:///kept.f90", 1)
    _prime_inlay("file:///gone.f90", 1)
    inlay.forget_uri("file:///gone.f90")
    assert "file:///gone.f90" not in inlay._tables_cache
    assert "file:///kept.f90" in inlay._tables_cache
    inlay.forget_uri("file:///kept.f90")


def test_inlay_forget_uri_noop_on_unknown() -> None:
    """Calling ``forget_uri`` on a uri that's not cached doesn't raise."""
    inlay.forget_uri("file:///never-seen.f90")


def test_decl_scan_forget_uri_evicts() -> None:
    """``decl_scan.forget_uri`` drops the matching entry only."""
    _prime_decl_scan("file:///kept.f90", 1)
    _prime_decl_scan("file:///gone.f90", 1)
    decl_scan.forget_uri("file:///gone.f90")
    assert "file:///gone.f90" not in decl_scan._uri_scan_cache
    assert "file:///kept.f90" in decl_scan._uri_scan_cache
    decl_scan.forget_uri("file:///kept.f90")


def test_decl_scan_forget_uri_noop_on_unknown() -> None:
    """Calling ``forget_uri`` on an unknown URI doesn't raise."""
    decl_scan.forget_uri("file:///never-seen.f90")


def test_inlay_open_close_loop_bounded() -> None:
    """Open + close 100 distinct URIs leaves the cache empty.

    Memory-churn check per the 0.2.6 cache-hygiene checklist: the
    per-URI cache must not accumulate entries for closed buffers.
    """
    for i in range(100):
        uri = f"file:///churn-{i}.f90"
        _prime_inlay(uri, 1)
        inlay.forget_uri(uri)
    assert all(not k.startswith("file:///churn-") for k in inlay._tables_cache)


def test_decl_scan_open_close_loop_bounded() -> None:
    """Same memory-churn check for decl_scan."""
    for i in range(100):
        uri = f"file:///churn-{i}.f90"
        _prime_decl_scan(uri, 1)
        decl_scan.forget_uri(uri)
    assert all(
        not k.startswith("file:///churn-") for k in decl_scan._uri_scan_cache
    )
