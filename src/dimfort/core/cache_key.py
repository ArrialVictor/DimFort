r"""Cache key derivation for the content-hash workspace cache.

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
# v3: Phase 2c added the @unit_affine_conversion directive, which changes
#     diagnostic emission for *identical* source bytes — a valid directive
#     now suppresses the S002 the statement used to raise, and an invalid
#     one emits S003. The package version string did not change (still
#     0.1.x), so v2 entries written by a pre-2c server would otherwise
#     replay stale S002s (no suppression, no S003) on conversion lines.
# v4: 0.2.2 added three configurable comment-delimiter lists
#     (``unit_comment_delimiters`` / ``unit_assume_comment_delimiters`` /
#     ``unit_affine_comment_delimiters``). Toggling a list changes which
#     comments produce annotations for the same source bytes — e.g. a
#     user who removes the canonical ``@unit_assume{`` entry should stop
#     seeing assume-derived U020 / U023 fires. Without this bump, v3
#     entries written under one pattern set would replay under another
#     and serve stale diagnostics.
# v5: parametric-polymorphism M1 extends ``Unit`` with a ``tyvars`` field
#     and accepts ``'a`` in ``@unit{...}`` annotations. A pre-v5 cache
#     entry for a Unit serialised with no ``"v"`` key loads fine
#     (defaults to no tyvars), but any source that uses ``'a`` would
#     have errored under v4 and now succeeds — same source bytes,
#     different diagnostics. Bump invalidates those stale entries.
# v6: cache_serde now round-trips ``Unit.offset`` via an optional ``"o"``
#     key (previously dropped). Any v5 cache entry holding an affine
#     unit (e.g. degC, offset=273.15) was loaded back lossy as K
#     (offset=0), which silently corrupted downstream diagnostics. v6
#     refreshes those entries so the next check rebuilds them with
#     the offset preserved.
# v7: cache_serde now round-trips four previously-dropped ModuleExports
#     fields (``inner_uses`` / ``default_private`` / ``public_names`` /
#     ``private_names``) plus ``Diagnostic.suggested_rewrite``. The
#     ModuleExports drop was latent (no consumer reads visibility on
#     the check path *today*) but pre-empts every future visibility-
#     aware check from quietly replaying stale cached results; the
#     Diagnostic drop was live — a warm U002 lost its "did you mean...?"
#     suggestion compared to a cold one. v7 refreshes both classes of
#     entry so the next check rebuilds with all fields present.
# v8: 0.2.3.1 added ``Diagnostic.polymorphism_conflict`` (structured
#     conflict data the LSP panel reads to render H020's spec-faithful
#     ``'a = unit — collides with arg N`` form) and reformatted H020's
#     message text (multi-line, em-dash separator, bare ``arg N``
#     partner labels). Both fields are part of the cached ``Diagnostic``
#     record. v7 entries written before the rewrite would replay the
#     old single-line message with no structured field, so the panel
#     would fall back to the generic ``(expected 'a)`` trailer on a
#     warm cache hit — masking the very UX fix this release ships.
#     The package version string did not change (cache invalidates by
#     ``(__version__, CHECKER_OUTPUT_VERSION)``; bumping the latter is
#     how we invalidate cleanly within a patch release that ships a
#     diagnostic-shape change).
# v9: 0.2.3.1 also fixed ``_resolve`` to apply the call-site unifier's
#     substitution to a polymorphic callee's return unit. Pre-fix, an
#     assignment ``r:m = f(m, m)`` where ``f`` returns ``'a`` saw the
#     RHS resolve to the formal ``'a`` (not the bound ``m``) and fired
#     a spurious H001. Now the call resolves to ``m`` on success and
#     to ``None`` on unification failure (H020 already reports the
#     failure; H001 must NOT double-fire). Pre-v9 cache entries for
#     any file containing a polymorphic-function call carry the wrong
#     H001 set — bump invalidates so warm checks redo the resolution.
CHECKER_OUTPUT_VERSION = 9


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
# - ``unit_comment_delimiters`` /
#   ``unit_assume_comment_delimiters`` /
#   ``unit_affine_comment_delimiters``: 0.2.2 directive pattern lists.
#   Changing a list changes which comments produce annotations for the
#   same source bytes — e.g. removing the canonical ``@unit_assume{``
#   entry should stop generating assume records on the next check.
PER_FILE_CONFIG_KEYS: tuple[str, ...] = (
    "external_modules",
    "extra_defines",
    "extra_include_paths",
    "units_file_hash",
    "diagnostic_severities",
    "scale_mode",
    "unit_comment_delimiters",
    "unit_assume_comment_delimiters",
    "unit_affine_comment_delimiters",
)


def _section(tag: bytes, body: bytes) -> bytes:
    r"""Length-prefix a section so concatenations are unambiguous.

    Args:
        tag: Three-byte section tag (e.g. ``b"SRC"``).
        body: Raw section payload.

    Returns:
        ``tag + b"\0" + 8-byte big-endian length + body``.
    """
    return tag + b"\0" + len(body).to_bytes(8, "big") + body


def _config_bytes(config: dict[str, object]) -> bytes:
    """Return canonical JSON bytes for the per-file-affecting config subset.

    Missing keys normalise to a typed empty (``[]`` for list-y keys,
    ``{}`` for dict-y keys, ``""`` for the units-file hash) so the
    contributed bytes stay stable across runs where the user has
    not configured that dimension. The canonical form sorts keys
    and uses compact separators so byte-equality is hash-stable.

    Args:
        config: Workspace config dict. Only the keys listed in
            :data:`PER_FILE_CONFIG_KEYS` are read; all others are
            ignored.

    Returns:
        UTF-8 JSON bytes of the normalised subset, with sorted keys
        and compact separators.
    """
    list_keys = {
        "external_modules", "extra_defines", "extra_include_paths",
        "unit_comment_delimiters", "unit_assume_comment_delimiters",
        "unit_affine_comment_delimiters",
    }
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

    Args:
        source_bytes: Raw source bytes of the file under check.
        cpp_closure_hashes: Map from absolute include path to its
            content hash (use :class:`IncludeHasher` to populate).
            Order-independent: paths are sorted before hashing.
        config: Workspace config dict; only the keys listed in
            :data:`PER_FILE_CONFIG_KEYS` contribute to the key.

    Returns:
        Hex SHA-256 digest of the length-prefixed section
        concatenation (SRC, CPP, CFG, VER, OUT).
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
    consumers (real-world Fortran codebases often pull a shared header
    like ``netcdf.inc`` from hundreds of files). Hashing once per
    (path, mtime) tuple keeps that linear.

    The mtime check is a cheap invalidation signal *within a run*;
    persistent across-run caching of include hashes is left to a
    later optimisation if needed.

    Attributes:
        _cache: Map from absolute path to ``(mtime_ns, hex_digest)``.
            Populated lazily on :meth:`hash_for`.
    """

    _cache: dict[str, tuple[int, str]] = field(default_factory=dict)

    def hash_for(self, abspath: str) -> str:
        """Return the hex SHA-256 of the include's contents.

        Args:
            abspath: Absolute path of the include file to hash.

        Returns:
            Hex SHA-256 digest of the file's bytes. Cached by
            ``(abspath, mtime_ns)``; a stale entry is replaced
            transparently.

        Raises:
            FileNotFoundError: If ``abspath`` does not exist. The
                caller can catch and treat as cache-miss (the file's
                content can't be validated, so any cached entry that
                references it must be invalidated).
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

        Args:
            paths: Set of absolute include paths to hash.

        Returns:
            Map from path to hex SHA-256 digest, suitable for feeding
            to :func:`compute_file_key`. A missing file produces the
            literal digest ``"missing"`` so the key still distinguishes
            "include was here last time" from "include disappeared".
        """
        out: dict[str, str] = {}
        for p in paths:
            try:
                out[p] = self.hash_for(p)
            except FileNotFoundError:
                out[p] = "missing"
        return out
