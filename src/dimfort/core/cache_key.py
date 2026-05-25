"""Cache key derivation for the content-hash workspace cache.

A per-file cache key is the SHA-256 of length-prefixed sections:

1. ``b"SRC\0"`` + raw source bytes
2. ``b"CPP\0"`` + sorted ``(abspath, sha256)`` of every cpp-include
3. ``b"CFG\0"`` + canonical JSON of the per-file-affecting config subset
4. ``b"VER\0"`` + ``dimfort.__version__``
5. ``b"OUT\0"`` + ``CHECKER_OUTPUT_VERSION`` (decimal)

Any change to a section's bytes invalidates the entry. The OUT version
is hand-bumped whenever the serialized output shape changes (see
``cache_serde``); the cache directory is sharded by it so old entries
fall outside the lookup path automatically.

This module is pure: no I/O outside of reading include files when
:class:`IncludeHasher.hash_for` is called.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field

from dimfort import __version__ as _dimfort_version

# Bump on any change to:
#   - the serialized payload shape in ``cache_serde``
#   - the checker's diagnostic emission semantics in a way that
#     changes what a cached file would have produced
# After a bump, the cache directory's ``v{N}/`` shard for the old
# version is orphaned and gets pruned by the LRU sweep.
#
# v2: the LSP now applies [diagnostics] severity overrides (it never
#     called set_severity_overrides before). v1 entries written by the
#     buggy server are keyed *with* the severity config but baked the
#     un-overridden severity in — poisoned hits that replay the wrong
#     severity. Bumping orphans them so the fix actually surfaces.
CHECKER_OUTPUT_VERSION = 2


# Keys from a workspace config that affect a *file's* output and
# therefore must contribute to the cache key. Every dimension along
# which the checker's diagnostics can change for the same source
# bytes belongs here. If a config dimension is missing from this
# tuple, edits to it will silently serve stale cached diagnostics.
#
# - ``external_modules``: changes which ``use foo`` clauses resolve.
# - ``extra_defines`` / ``extra_include_paths``: change cpp expansion.
# - ``units_file_hash``: content hash of the project's units table.
#   Caller is responsible for hashing the file (it's typically small).
# - ``diagnostic_severities``: ``[diagnostics]`` overrides are applied
#   inside ``ts_checker.check`` via ``finalize_diagnostics`` *before*
#   diagnostics are cached. A change to overrides must invalidate.
# - ``scale_mode``: opt-in scale checking changes which S001 diagnostics
#   a file produces for the same bytes. Toggling it (CLI ``--scale`` /
#   ``[scale] enabled`` / LSP ``scaleMode``) must invalidate, else a
#   scale-on run's S001s are replayed after scale is turned off.
PER_FILE_CONFIG_KEYS: tuple[str, ...] = (
    "external_modules",
    "extra_defines",
    "extra_include_paths",
    "units_file_hash",
    "diagnostic_severities",
    "scale_mode",
)


def _section(tag: bytes, body: bytes) -> bytes:
    """Length-prefix a section so concatenations are unambiguous."""
    return tag + b"\0" + len(body).to_bytes(8, "big") + body


def _config_bytes(config: dict[str, object]) -> bytes:
    """Canonical JSON bytes for the per-file-affecting config subset.

    Missing keys normalise to a typed empty (``[]`` for list-y keys,
    ``{}`` for dict-y keys, ``""`` for the units-file hash) so the
    contributed bytes stay stable across runs where the user has
    not configured that dimension. The canonical form sorts keys
    and uses compact separators so byte-equality is hash-stable.
    """
    list_keys = {"external_modules", "extra_defines", "extra_include_paths"}
    dict_keys = {"diagnostic_severities"}
    str_keys = {"units_file_hash"}
    bool_keys = {"scale_mode"}
    subset: dict[str, object] = {}
    for k in PER_FILE_CONFIG_KEYS:
        v = config.get(k)
        if v is None:
            if k in list_keys:
                v = []
            elif k in dict_keys:
                v = {}
            elif k in str_keys:
                v = ""
            elif k in bool_keys:
                v = False
            else:
                v = None
        # Frozenset / set are unordered; coerce to a sorted list.
        if isinstance(v, (set, frozenset)):
            v = sorted(v)
        subset[k] = v
    return json.dumps(subset, sort_keys=True, separators=(",", ":")).encode()


def compute_file_key(
    *,
    source_bytes: bytes,
    cpp_closure_hashes: dict[str, str],
    config: dict[str, object],
) -> str:
    """Return the hex SHA-256 cache key for one file.

    ``cpp_closure_hashes`` maps absolute include path → its content
    hash (use :class:`IncludeHasher` to populate). Order-independent.
    ``config`` is the workspace config dict; only the keys listed in
    :data:`PER_FILE_CONFIG_KEYS` are read.
    """
    h = hashlib.sha256()
    h.update(_section(b"SRC", source_bytes))

    # Sort by path so the bytes contributed are order-independent.
    sorted_pairs = sorted(cpp_closure_hashes.items())
    cpp_body = b"".join(
        path.encode() + b"\0" + digest.encode() + b"\0"
        for path, digest in sorted_pairs
    )
    h.update(_section(b"CPP", cpp_body))

    h.update(_section(b"CFG", _config_bytes(config)))
    h.update(_section(b"VER", _dimfort_version.encode()))
    h.update(_section(b"OUT", str(CHECKER_OUTPUT_VERSION).encode()))

    return h.hexdigest()


@dataclass
class IncludeHasher:
    """Memoised content-hash for include files.

    Within one workspace check, the same header may be hashed for many
    consumers (LMDZ pulls ``netcdf.inc`` from ~hundreds of files).
    Hashing once per (path, mtime) tuple keeps that linear.

    The mtime check is a cheap invalidation signal *within a run*;
    persistent across-run caching of include hashes is left to a
    later optimisation if needed.
    """

    _cache: dict[str, tuple[int, str]] = field(default_factory=dict)

    def hash_for(self, abspath: str) -> str:
        """Return the hex SHA-256 of the include's contents.

        Missing files raise ``FileNotFoundError`` — the caller can
        catch and treat as cache-miss (the file's content can't be
        validated, so any cached entry that references it must be
        invalidated).
        """
        try:
            mtime_ns = os.stat(abspath).st_mtime_ns
        except FileNotFoundError:
            self._cache.pop(abspath, None)
            raise

        cached = self._cache.get(abspath)
        if cached is not None and cached[0] == mtime_ns:
            return cached[1]

        with open(abspath, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        self._cache[abspath] = (mtime_ns, digest)
        return digest

    def hash_closure(self, paths: frozenset[str]) -> dict[str, str]:
        """Bulk-hash an entire cpp closure.

        Returns a ``{path: digest}`` map suitable for feeding to
        :func:`compute_file_key`. A missing file produces the literal
        digest ``"missing"`` so the key still distinguishes "include
        was here last time" from "include disappeared".
        """
        out: dict[str, str] = {}
        for p in paths:
            try:
                out[p] = self.hash_for(p)
            except FileNotFoundError:
                out[p] = "missing"
        return out
