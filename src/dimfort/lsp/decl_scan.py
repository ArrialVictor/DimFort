"""Source-side declaration scanning for the LSP server.

Recovers the raw ``DeclarationSite`` list for a file — from the live (possibly
unsaved) editor buffer when available, else from disk — so the panel's scope
section and the code-action provider can see which declarations still lack a
``@unit{}`` annotation. Thin wrappers over ``dimfort.core.annotations``.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pygls.lsp.server import LanguageServer

if TYPE_CHECKING:
    from dimfort.core.annotations import DeclarationSite


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
        return scan_text(doc.source).declarations
    except Exception:
        return _last_scan_declarations(resolved)
