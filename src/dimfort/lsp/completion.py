"""Completion provider for unit tokens inside ``@unit{…}`` annotations.

Suggests base units, derived units, and SI prefixes from the active unit
table, but only when the cursor sits inside an unclosed ``@unit{…}`` — so it
never intrudes on ordinary Fortran editing. Pure of LSP server state and
tree-sitter; ``server.py`` registers the LSP feature and delegates here.
"""
from __future__ import annotations

import re

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from dimfort.core import units as _units_mod

# Fires only when the cursor sits inside an unclosed ``@unit{…}``
# **inside an active Fortran comment** — without the comment guard the
# trigger fires inside string literals containing the substring
# (``print *, "see @unit{the docs"`` would otherwise pop the full unit
# list).
_UNIT_TRIGGER_RE = re.compile(r"@unit\s*\{([^}]*)$")


def _inside_string_literal(prefix: str) -> bool:
    """Heuristic: cursor is inside an unclosed ``'…'`` or ``"…"`` on
    this line. Fortran lacks line-continuation inside string literals,
    so a per-line scan suffices. Doubled quotes (Fortran's escape) are
    treated as two separate quote events — fine for the trigger guard
    purpose since either parity decides "in string" correctly.
    """
    in_single = False
    in_double = False
    for ch in prefix:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
    return in_single or in_double


def _comment_active(prefix: str) -> bool:
    """True iff a bare ``!`` (the canonical Fortran comment delimiter)
    has been seen on this line *outside* a string. Configurable
    inline-comment delimiters are a Phase-2 follow-up — the canonical
    case covers the common annotation surface and matches the
    user-facing trigger expectation.
    """
    in_single = False
    in_double = False
    for ch in prefix:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "!" and not (in_single or in_double):
            return True
    return False


def complete(
    ls: LanguageServer, params: lsp.CompletionParams
) -> lsp.CompletionList | None:
    table = _units_mod.DEFAULT_TABLE
    if table is None:
        return None
    try:
        doc = ls.workspace.get_text_document(params.text_document.uri)
    except Exception:
        return None
    line_text = (
        doc.lines[params.position.line]
        if params.position.line < len(doc.lines)
        else ""
    )
    prefix = line_text[: params.position.character]
    # Only fire when the cursor is inside an unclosed `@unit{…}` AND
    # the line has an active comment delimiter AND we're not inside
    # a string literal (e.g. ``print *, "@unit{...``).
    if not _UNIT_TRIGGER_RE.search(prefix):
        return None
    if _inside_string_literal(prefix):
        return None
    if not _comment_active(prefix):
        return None

    items: list[lsp.CompletionItem] = []
    for name in sorted(table.base):
        items.append(
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Unit,
                detail="base unit",
            )
        )
    for name in sorted(table.derived):
        items.append(
            lsp.CompletionItem(
                label=name,
                kind=lsp.CompletionItemKind.Unit,
                detail="derived unit",
            )
        )
    for prefix_sym in sorted(table.prefixes):
        items.append(
            lsp.CompletionItem(
                label=prefix_sym,
                kind=lsp.CompletionItemKind.Constant,
                detail=f"SI prefix ({table.prefixes[prefix_sym]})",
            )
        )
    return lsp.CompletionList(is_incomplete=False, items=items)
