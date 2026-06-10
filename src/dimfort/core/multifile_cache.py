"""In-memory caches for the load + index phases of ``check_files``.

The per-file diagnostic cache in :mod:`dimfort.core.cache_store` covers
the check phase (D); the three caches in this module cover load (A),
index (C), and the projection step (M1), which together are the floor
any repeated ``check_files`` caller pays.

Session-scoped: held by the LSP state for the lifetime of the running
server. ``ProjectionCache`` is mirrored to disk by
:mod:`dimfort.core.multifile_cache_persist`; ``TreeCache`` and
``ModuleExportsCache`` are in-memory only. Default for CLI callers is
to pass ``None``, leaving behaviour byte-identical to the un-cached
path.

Bounds & eviction
-----------------

Each cache accepts an optional ``max_entries`` keyword. Default
``None`` = unbounded — the current LSP behaviour. When set, the cache
evicts in **FIFO** order on overflow (oldest inserted entry first; hits
do not move-to-end). FIFO over LRU here because content-hash keys mean
a "hit on an old entry" already implies the entry's source bytes are
still live somewhere; LRU bumping would buy little and require an
extra dict op per get.

Sizing: the bound MUST be at least the active workset size, otherwise
entries get evicted *during* a single ``check_files`` call and the
cache no-ops. A real-world ``check_files`` over a large Fortran
codebase touches ~2000-3000 files, so any cap below ~3000 silently
defeats the cache. The workset-adaptive default + ``.dimfort.toml``
``[cache] max_entries`` override that wire this knob through the LSP
land as a follow-up (see ``docs/0_2_6_PLAN.md``).
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Tree

    from dimfort.core.annotations import ScanResult
    from dimfort.core.attach import AttachmentResult
    from dimfort.core.symbols import FuncSig, ModuleExports
    from dimfort.core.unit_patterns import StructuredPattern, UnitPattern


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

    **Key:** ``TreeKey(content_hash, parse_mode)`` — entries dedup by
    source bytes + CPP config fingerprint.

    **Bound:** ``max_entries`` (default ``None`` = unbounded). When
    set, FIFO eviction on overflow. Header-file edits are NOT detected
    (the CPP ``parse_mode`` fingerprint hashes config, not included-file
    contents) — opt out with ``--no-tree-cache`` if that bites.

    **didClose:** N/A — content-keyed, not URI-keyed.

    **Concurrency:** the LSP runs a background workspace-stats refresh
    on a daemon thread alongside per-request handlers. Both can hit the
    same cache; the lock is held only for the dict op, never across a
    parse.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        """Create an empty cache.

        Args:
            max_entries: Optional FIFO cap. ``None`` (default) means
                unbounded. See module docstring "Bounds & eviction"
                for sizing constraints.
        """
        self._entries: OrderedDict[TreeKey, CachedParse] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, key: TreeKey) -> CachedParse | None:
        """Return the cached parse for ``key``, or ``None`` on miss."""
        with self._lock:
            return self._entries.get(key)

    def put(self, key: TreeKey, value: CachedParse) -> None:
        """Store ``value`` under ``key`` (overwrites any existing entry).

        Evicts the oldest entry (FIFO) when adding would exceed
        ``max_entries``. Overwriting an existing key does NOT count as
        a new insertion.
        """
        with self._lock:
            if key in self._entries:
                self._entries[key] = value
                return
            self._entries[key] = value
            if (
                self._max_entries is not None
                and len(self._entries) > self._max_entries
            ):
                self._entries.popitem(last=False)

    def set_max_entries(self, n: int | None) -> None:
        """Update the FIFO cap and immediately trim entries above it.

        Wired by the LSP layer to the adaptive default
        ``max(observed_workset_size × 4, 4096)`` so the cap grows with
        the largest workset seen this session and never evicts inside
        a single ``check_files`` pass. ``None`` removes the cap.
        """
        with self._lock:
            self._max_entries = n
            if n is None:
                return
            while len(self._entries) > n:
                self._entries.popitem(last=False)

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


def digest_merged_var_units(merged_var_units: Mapping[str, object]) -> str:
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
    """Thread-safe cache of ``collect_function_signatures_and_module_exports``.

    **Key:** ``ExportsKey(content_hash, merged_units_digest)`` — file
    content + workset-wide ``var_units`` fallback context.

    **Bound:** ``max_entries`` (default ``None`` = unbounded). When
    set, FIFO eviction on overflow. The three sub-memos below
    (``digest_memo``, ``parsed_units_memo``, ``extract_uses_memo``)
    are NOT capped: they grow with the number of distinct
    ``ModuleExports`` / unit-table / file-text objects observed in the
    session — small per entry, bounded in practice by ``_entries``.

    **didClose:** N/A — content-keyed, not URI-keyed.

    Also holds a session-lifetime ``digest_memo`` keyed by
    ``id(ModuleExports)`` — the per-file diagnostic cache asks for a
    digest of every consumed module on every check, and once
    ``ModuleExports`` identity is stable across calls (which this
    cache provides) the digest is also stable.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        """Create an empty cache.

        Args:
            max_entries: Optional FIFO cap on ``_entries`` only. Sub-memos
                (``digest_memo`` etc.) are uncapped. ``None`` (default)
                means fully unbounded.
        """
        self._entries: OrderedDict[
            ExportsKey, tuple[dict[str, FuncSig], dict[str, ModuleExports]]
        ] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries
        # id(exports) → digest; populated lazily by callers. The id is
        # only stable while the underlying object stays alive, which
        # is the entire LSP session because ``_entries`` (above) holds
        # references to the same objects.
        self.digest_memo: dict[int, str] = {}
        # (input_text_digest, id(table)) → parsed UnitExpr table.
        # Shared across the flat ``_parse_var_units`` and the scoped
        # ``_parse_var_units_by_scope`` (different key+value shapes
        # but the same memo dict works because the input_digest fully
        # distinguishes them).
        self.parsed_units_memo: dict[tuple[str, int], object] = {}
        # File-text → parsed ``use`` clauses. ``extract_uses`` walks
        # the raw text per file every Phase D pass; memoizing by the
        # str itself (Python interns hash on first compute) skips
        # the walk when the file's text is unchanged across calls.
        self.extract_uses_memo: dict[str, tuple[object, ...]] = {}

    def get(
        self, key: ExportsKey
    ) -> tuple[dict[str, FuncSig], dict[str, ModuleExports]] | None:
        """Return the cached ``(sigs, modules)`` tuple, or ``None`` on miss."""
        with self._lock:
            return self._entries.get(key)

    def put(
        self,
        key: ExportsKey,
        value: tuple[dict[str, FuncSig], dict[str, ModuleExports]],
    ) -> None:
        """Store ``value`` (a ``(sigs, modules)`` tuple) under ``key``.

        FIFO-evicts the oldest entry when adding would exceed
        ``max_entries``. Overwriting an existing key is not a new
        insertion.
        """
        with self._lock:
            if key in self._entries:
                self._entries[key] = value
                return
            self._entries[key] = value
            if (
                self._max_entries is not None
                and len(self._entries) > self._max_entries
            ):
                self._entries.popitem(last=False)

    def set_max_entries(self, n: int | None) -> None:
        """Update the FIFO cap on ``_entries`` and trim above it.

        Wired by the LSP layer to the adaptive default
        ``max(observed_workset_size × 4, 4096)``. ``None`` removes
        the cap (sub-memos are unaffected — see class docstring).
        """
        with self._lock:
            self._max_entries = n
            if n is None:
                return
            while len(self._entries) > n:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Drop every cached entry."""
        with self._lock:
            self._entries.clear()


# ---------------------------------------------------------------------------
# Projection cache (M1) — scan + attach outputs per file content
# ---------------------------------------------------------------------------


def patterns_fingerprint(
    unit_patterns: tuple[UnitPattern, ...],
    assume_patterns: tuple[StructuredPattern, ...],
    affine_patterns: tuple[StructuredPattern, ...],
) -> str:
    """Stable short hash of the configured annotation patterns.

    Folded into :class:`ProjectionKey` so a project-config change that
    affects which comments scan as ``@unit{}`` invalidates cached
    projections naturally.
    """
    h = hashlib.sha256()
    for up in unit_patterns:
        h.update(up.open.encode("utf-8"))
        h.update(b"|")
        h.update(up.close.encode("utf-8"))
        h.update(b";")
    h.update(b"||")
    for ap in assume_patterns:
        h.update(ap.open.encode("utf-8"))
        h.update(b"|")
        h.update(ap.close.encode("utf-8"))
        h.update(b"|")
        h.update(ap.sep.encode("utf-8"))
        h.update(b";")
    h.update(b"||")
    for fp in affine_patterns:
        h.update(fp.open.encode("utf-8"))
        h.update(b"|")
        h.update(fp.close.encode("utf-8"))
        h.update(b"|")
        h.update(fp.sep.encode("utf-8"))
        h.update(b";")
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class ProjectionKey:
    """Identifies a per-file projection cache entry.

    ``content_hash`` is the same SHA-256 of source bytes ``TreeCache``
    uses. ``patterns_fp`` captures the configured ``@unit{}`` /
    ``@unit_assume{}`` / ``@unit_affine_conversion{}`` delimiters so
    a project-config change that changes which comments scan as
    annotations invalidates naturally.
    """

    content_hash: str
    patterns_fp: str


@dataclass(frozen=True)
class CachedProjection:
    """Cached output of ``scan_text + attach`` for one file.

    Both fields are read-only by contract. Callers MUST NOT mutate
    them — the same record is handed back on every cache hit.
    """

    scan: ScanResult
    attachment: AttachmentResult


class ProjectionCache:
    """Thread-safe in-memory cache of per-file scan + attach outputs.

    **Key:** ``ProjectionKey(content_hash, patterns_fp)`` — file content
    + configured ``@unit{}`` / ``@unit_assume{}`` /
    ``@unit_affine_conversion{}`` pattern fingerprint.

    **Bound:** ``max_entries`` (default ``None`` = unbounded). When
    set, FIFO eviction on overflow. Mirrored to disk by
    :mod:`dimfort.core.multifile_cache_persist`; the on-disk file is
    pre-loaded on LSP startup so cold-after-restart benefits too.

    **didClose:** N/A — content-keyed, not URI-keyed. Workspace file
    deletions are NOT auto-pruned from the on-disk cache (matches
    :class:`WorkspaceIndex` behaviour; see ``0_2_6_PLAN.md`` for the
    file-watcher follow-up).

    Population fills as ``_load_one`` runs. On a cache hit the bulk
    tree-walking work of ``scan_text`` (~3 s on a 2000-file workset)
    and the attachment-building pass (~1 s) collapse to a single dict
    lookup.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        """Create an empty cache.

        Args:
            max_entries: Optional FIFO cap. ``None`` (default) means
                unbounded. See module docstring "Bounds & eviction"
                for sizing constraints.
        """
        self._entries: OrderedDict[ProjectionKey, CachedProjection] = OrderedDict()
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, key: ProjectionKey) -> CachedProjection | None:
        """Return the cached projection, or ``None`` on miss."""
        with self._lock:
            return self._entries.get(key)

    def put(self, key: ProjectionKey, value: CachedProjection) -> None:
        """Store ``value`` under ``key`` (overwrites any prior entry).

        FIFO-evicts the oldest entry when adding would exceed
        ``max_entries``. Overwriting an existing key is not a new
        insertion.
        """
        with self._lock:
            if key in self._entries:
                self._entries[key] = value
                return
            self._entries[key] = value
            if (
                self._max_entries is not None
                and len(self._entries) > self._max_entries
            ):
                self._entries.popitem(last=False)

    def set_max_entries(self, n: int | None) -> None:
        """Update the FIFO cap and trim above it.

        Wired by the LSP layer to the adaptive default
        ``max(observed_workset_size × 4, 4096)``. ``None`` removes
        the cap. The on-disk projection cache is unaffected — it's
        persisted in full and re-loaded at next session start.
        """
        with self._lock:
            self._max_entries = n
            if n is None:
                return
            while len(self._entries) > n:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        """Number of cached entries."""
        with self._lock:
            return len(self._entries)

    def clear(self) -> None:
        """Drop every cached entry."""
        with self._lock:
            self._entries.clear()
