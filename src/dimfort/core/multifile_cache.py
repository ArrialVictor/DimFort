"""In-memory caches for the load + index phases of ``check_files``.

The per-file diagnostic cache in :mod:`dimfort.core.cache_store` covers
the check phase (D); these two caches cover load (A) and index (C),
which together are the floor any repeated ``check_files`` caller pays.

Both caches are session-scoped: held by the LSP state for the lifetime
of the running server, never persisted to disk. Default for CLI callers
is to pass ``None``, leaving behaviour byte-identical to the
un-cached path.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Tree

    from dimfort.core.ts_checker import ModuleExports


@dataclass(frozen=True)
class TreeKey:
    """Identifies a parsed-tree cache entry.

    ``parse_mode`` folds the CPP configuration so that toggling
    ``cpp_defines`` / ``include_paths`` between calls invalidates
    naturally. For raw parses (no cpp run), it's the literal
    ``"raw"``.
    """

    content_hash: str
    parse_mode: str


@dataclass(frozen=True)
class CachedParse:
    """Cached output of one ``_load_one`` parse step.

    Mirrors the parse-derived fields of ``_Loaded`` so a hit can
    reconstitute the record without re-running tree-sitter. Fields
    after ``tree`` are populated only for cpp-mode entries.
    """

    tree: Tree
    source: bytes
    expanded_text: bytes | None = None
    line_map: tuple[int | None, ...] | None = None
    raw_tree: Tree | None = None
    cpp_closure: frozenset[str] = frozenset()
    # SHA-256 hex of ``source`` (the bytes the tree was built from —
    # cpp-expanded for cpp files, raw otherwise). Lets the index loop's
    # ExportsKey reuse the hash _load_one already computed.
    source_hash: str = ""


def cpp_fingerprint(
    cpp_defines: tuple[str, ...], include_paths: tuple[Path, ...]
) -> str:
    """Stable short hash of the CPP configuration.

    Folded into :class:`TreeKey` ``parse_mode`` so a defines/includes
    toggle invalidates without callers tracking config diffs. Does not
    hash the *contents* of included headers — editing a header
    mid-session can serve a stale cpp tree until server restart;
    opt out with ``--no-tree-cache`` if that bites.
    """
    h = hashlib.sha256()
    for d in cpp_defines:
        h.update(d.encode("utf-8"))
        h.update(b"\0")
    h.update(b"|")
    for p in include_paths:
        h.update(str(p).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def content_hash(source: bytes) -> str:
    """SHA-256 hex digest of file content bytes."""
    return hashlib.sha256(source).hexdigest()


class TreeCache:
    """Thread-safe in-memory cache of tree-sitter parse results.

    Concurrency: the LSP runs a background workspace-stats refresh on
    a daemon thread alongside per-request handlers. Both can hit the
    same cache; the lock is held only for the dict op, never across
    a parse.
    """

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[TreeKey, CachedParse] = {}
        self._lock = threading.Lock()

    def get(self, key: TreeKey) -> CachedParse | None:
        """Return the cached parse for ``key``, or ``None`` on miss."""
        with self._lock:
            return self._entries.get(key)

    def put(self, key: TreeKey, value: CachedParse) -> None:
        """Store ``value`` under ``key`` (overwrites any existing entry)."""
        with self._lock:
            self._entries[key] = value

    def __len__(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Drop every cached entry."""
        with self._lock:
            self._entries.clear()


@dataclass(frozen=True)
class ExportsKey:
    """Identifies a ModuleExports cache entry.

    Both fields are content-based hashes so the key survives the
    per-call rebuilding of ``merged_var_units`` (the same workset state
    produces the same key even though the dict object is fresh).

    ``content_hash`` ties the entry to the source the tree was built
    from (same value used by :class:`TreeKey`). ``merged_units_digest``
    captures the workset-wide var-units fallback context: rebuilt every
    ``check_files`` call but cheap to fingerprint once.
    """

    content_hash: str
    merged_units_digest: str


def digest_merged_var_units(merged_var_units: dict[str, object]) -> str:
    """Stable short hash of the workset-wide flat ``var_units`` table.

    Computed once per ``check_files`` call and reused across every
    file's exports-cache lookup, so the per-file cost is one dict
    lookup plus the constant hash. Keys are sorted to make the digest
    order-independent.
    """
    h = hashlib.sha256()
    for name in sorted(merged_var_units):
        h.update(name.encode("utf-8"))
        h.update(b"=")
        h.update(repr(merged_var_units[name]).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


class ModuleExportsCache:
    """Thread-safe cache of ``collect_function_signatures_and_module_exports``."""

    def __init__(self) -> None:
        """Create an empty cache."""
        self._entries: dict[
            ExportsKey, tuple[object, ModuleExports | None]
        ] = {}
        self._lock = threading.Lock()

    def get(
        self, key: ExportsKey
    ) -> tuple[object, ModuleExports | None] | None:
        """Return the cached ``(sigs, modules)`` tuple, or ``None`` on miss."""
        with self._lock:
            return self._entries.get(key)

    def put(
        self,
        key: ExportsKey,
        value: tuple[object, ModuleExports | None],
    ) -> None:
        """Store ``value`` (a ``(sigs, modules)`` tuple) under ``key``."""
        with self._lock:
            self._entries[key] = value

    def __len__(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Drop every cached entry."""
        with self._lock:
            self._entries.clear()
