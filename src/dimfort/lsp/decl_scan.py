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
    """
    from dimfort.core.annotations import scan_text
    try:
        doc = ls.workspace.get_text_document(uri)
        return scan_text(doc.source).declarations
    except Exception:
        return _last_scan_declarations(resolved)
