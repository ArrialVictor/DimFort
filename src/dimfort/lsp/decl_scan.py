"""Source-side declaration scanning for the LSP server.

Recovers the raw ``DeclarationSite`` list for a file — from the live (possibly
unsaved) editor buffer when available, else from disk — so the panel's scope
section and the code-action provider can see which declarations still lack a
``@unit{}`` annotation. Thin wrappers over ``dimfort.core.annotations``.

Per-URI scan cache
------------------
``_uri_scan_cache``: ``uri → (version, declarations)``. Single entry
per URI; replaces re-running ``scan_text`` from scratch on every
cursor-move ``dimfort/panelInfo`` request (the panel's debounce
limits LSP traffic, not per-event server-side cost). A typing
session over the same buffer pays the scan cost at most once per
edit.

Invalidation
~~~~~~~~~~~~
Entry replaced in place when the cached ``version`` differs from the
caller-supplied document version. Every ``didChange`` bumps the
version on the call site, so a stale entry never reaches a consumer.
There is no separate invalidation API — version comparison is the
sole correctness gate.

Bound
~~~~~
One entry per open buffer (``O(open buffers)``). Eviction on
``didClose`` via :func:`forget_uri`, called from
``server._forget_uri`` — without that hook closed buffers would
persist for the LSP session. No explicit numerical cap; bound is
the editor's open-file count.

Thread safety
~~~~~~~~~~~~~
``_uri_scan_cache_lock`` guards every read and write — ``panelInfo``
and ``codeAction`` can invoke the scan path concurrently. Lock is
held only for the dict op, not the underlying scan.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from pygls.lsp.server import LanguageServer

log = logging.getLogger("dimfort.lsp")

if TYPE_CHECKING:
    from dimfort.core.annotations import DeclarationSite

# Per-URI scan cache.
#
# Key: ``uri``. Value: ``(version, declarations)``. Single entry per
# URI — the cached version is whichever buffer revision we last
# scanned; a stale entry on version bump is replaced in place.
#
# Bound: O(open buffers). The didClose handler (``server._forget_uri``)
# evicts via :func:`forget_uri` below; without that call the entry
# would persist for the LSP session.
#
# Thread-safe via the module-level lock — panelInfo + codeAction can
# both invoke this handler concurrently.
_uri_scan_cache: dict[str, tuple[int, tuple[DeclarationSite, ...]]] = {}
_uri_scan_cache_lock = threading.Lock()


def forget_uri(uri: str) -> None:
    """Evict any cached scan for ``uri``.

    Called from the LSP ``textDocument/didClose`` handler so closed
    buffers don't accumulate in the per-URI cache.
    """
    with _uri_scan_cache_lock:
        _uri_scan_cache.pop(uri, None)


def _try_cached_uri(
    uri: str, version: int,
) -> tuple[DeclarationSite, ...] | None:
    """Return cached declarations for ``(uri, version)`` if any, else None."""
    with _uri_scan_cache_lock:
        cached = _uri_scan_cache.get(uri)
    if cached is None or cached[0] != version:
        return None
    return cached[1]


def _store_cached_uri(
    uri: str, version: int, decls: tuple[DeclarationSite, ...],
) -> None:
    """Store the latest scan result for ``uri`` at ``version``."""
    with _uri_scan_cache_lock:
        _uri_scan_cache[uri] = (version, decls)


def _last_scan_declarations(path: Path) -> tuple[DeclarationSite, ...] | None:
    """Re-scan the file on disk to recover the source-side declarations.

    Disk-only fallback. Prefer :func:`_scan_declarations_for_uri` when
    a ``LanguageServer`` + ``uri`` are in hand — it reads the live
    (possibly unsaved) buffer text so freshly-typed declarations show
    up without a save.

    Args:
        path: Absolute filesystem path of the Fortran source to scan.

    Returns:
        Tuple of :class:`DeclarationSite` records recovered from the
        on-disk file, or ``None`` when the read failed (missing file,
        permission error, etc.).

    Raises:
        Does not raise: any :class:`OSError` from the disk read is
        swallowed and reported as ``None``.

    Note:
        Wraps :func:`dimfort.core.annotations.scan_file`; no caching
        or buffer awareness — every call re-reads the file.
    """
    from dimfort.core.annotations import scan_file

    try:
        return scan_file(path).declarations
    except OSError:
        return None


def _scan_declarations_for_uri(
    ls: LanguageServer, uri: str, resolved: Path
) -> tuple[DeclarationSite, ...] | None:
    """Scan declarations from the live document text when available.

    Reads the open buffer's text (which includes unsaved edits) so the
    panel reflects a just-typed declaration immediately. Falls back to
    a disk read when the document isn't open in the workspace.

    Args:
        ls: Active :class:`LanguageServer` whose workspace exposes the
            live document store.
        uri: Editor URI for the document being inspected.
        resolved: Resolved on-disk path used as the disk-read fallback
            when the URI is not currently open in the workspace.

    Returns:
        Tuple of :class:`DeclarationSite` records recovered from the
        live buffer text, or ``None`` when neither the live read nor
        the disk fallback succeeded.

    Raises:
        Does not raise: any exception from the workspace lookup is
        swallowed and the disk fallback is attempted instead.

    Note:
        Wraps :func:`dimfort.core.annotations.scan_text` (buffer path)
        and :func:`_last_scan_declarations` (disk fallback).
    """
    from dimfort.core.annotations import scan_text
    try:
        doc = ls.workspace.get_text_document(uri)
    except Exception:
        # audited(0.2.7): silent-OK — pygls raises when the document
        # isn't open (yet); the disk fallback is the correct path.
        # No log: this is reachable from background paths that
        # legitimately query closed files, and would carpet the log.
        return _last_scan_declarations(resolved)
    # Audit #7: avoid re-scanning the full buffer text on every
    # panelInfo / codeAction request that lands on the same buffer
    # revision. pygls' TextDocument.version monotonically increases
    # on every didChange; a hit means the user hasn't edited since
    # our last scan. On miss (or first call for this URI) we scan
    # and cache.
    version = getattr(doc, "version", None) or 0
    cached = _try_cached_uri(uri, version)
    if cached is not None:
        return cached
    try:
        decls = scan_text(doc.source).declarations
    except Exception:
        # audited(0.2.7): error-surfacing — scan_text raising on a
        # live buffer is the canonical "silent stale-disk fallback"
        # bug class: panel renders the on-disk state with no
        # indication the live buffer scan failed. Could hide a
        # parser regression for days. log.warning surfaces to the
        # Output channel for diagnosis; no toast (this path runs on
        # every panel-info request; toasting would carpet the user).
        log.warning(
            "decl_scan: scan_text failed on live buffer for %s; "
            "falling back to last-known disk scan",
            uri,
            exc_info=True,
        )
        return _last_scan_declarations(resolved)
    _store_cached_uri(uri, version, decls)
    return decls
