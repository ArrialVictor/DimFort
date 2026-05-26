"""Cross-site unit analysis (``dimfort/interactions``).

Resolves the identifier under the cursor (or an explicit ``symbol`` param),
then runs :func:`dimfort.core.interactions.collect_interactions` over the
cached workset to report every read/write site of that symbol and any
conflicting unit claims between them. See docs/design/interaction-points.md.
``server.py`` registers the LSP feature and delegates here.

Note: this handler parses a *fresh* tree from the live document for the cursor
lookup, so it does not need ``state.ts_handler_lock`` (it never traverses a
shared cached tree, except as a fallback when the fresh parse fails).
"""
from __future__ import annotations

from dimfort.core import ts_parser as _ts
from dimfort.core.interactions import collect_interactions
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _trees_for
from dimfort.lsp.tree_nav import _identifier_at


def _serialize_interaction_point(p) -> dict:
    return {
        "file": p.file,
        "line": p.line,
        "column": p.column,
        "scope": p.scope,
        "kind": p.kind,            # declares | contributes | requires | uses
        "unit": p.unit_str,        # rendered unit, or "?" when unknown
        "snippet": p.snippet,
    }


def resolve(ls, params) -> dict | None:
    def _get(obj, key):
        if hasattr(obj, key):
            return getattr(obj, key)
        if isinstance(obj, dict):
            return obj.get(key)
        return None

    text_document = _get(params, "textDocument") or _get(params, "text_document")
    position = _get(params, "position")
    uri = _get(text_document, "uri") if text_document is not None else None
    explicit_symbol = _get(params, "symbol")
    scale = bool(_get(params, "scale"))

    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None

    symbol = explicit_symbol
    if symbol is None:
        if uri is None or position is None:
            return None
        found = _trees_for(uri)
        if found is None:
            return None
        _path, cached_tree, cached_source = found
        try:
            doc = ls.workspace.get_text_document(uri)
            source_bytes = doc.source.encode("utf-8")
            tree = _ts.parse_text(source_bytes)
        except Exception:
            tree, source_bytes = cached_tree, cached_source
        line = _get(position, "line")
        character = _get(position, "character")
        if line is None or character is None:
            return None
        symbol = _identifier_at(tree, source_bytes, int(line) + 1, int(character) + 1)
    if not symbol:
        return None

    report = collect_interactions(result, symbol, scale=scale)

    conflicts = [
        {
            "code": c.diagnostic.code,
            "message": c.diagnostic.message,
            "file": c.diagnostic.file,
            "line": c.diagnostic.start.line,
            "column": c.diagnostic.start.column,
            "site": _serialize_interaction_point(c.site),
            "reference": _serialize_interaction_point(c.reference),
        }
        for c in report.conflicts
    ]
    return {
        "symbol": report.symbol,
        "points": [_serialize_interaction_point(p) for p in report.points],
        "conflicts": conflicts,
        "hasConflict": bool(conflicts),
    }
