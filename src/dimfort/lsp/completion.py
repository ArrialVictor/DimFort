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

# Fires only when the cursor sits inside an unclosed ``@unit{…}``.
_UNIT_TRIGGER_RE = re.compile(r"@unit\s*\{([^}]*)$")


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
    # Only fire when the cursor is inside an unclosed `@unit{…}`.
    if not _UNIT_TRIGGER_RE.search(prefix):
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
