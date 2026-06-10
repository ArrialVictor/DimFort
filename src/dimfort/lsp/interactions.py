"""Cross-site unit analysis (``dimfort/interactions``).

Resolves the identifier under the cursor (or an explicit ``symbol`` param),
then runs :func:`dimfort.core.interactions.collect_interactions` over the
cached workset to report every read/write site of that symbol and any
conflicting unit claims between them. See docs/design/interaction-points.md.
``server.py`` registers the LSP feature and delegates here.

Note: this handler parses a *fresh* tree from the live document for the cursor
lookup, so it does not need ``state.ts_handler_lock`` (it never traverses a
shared cached tree). If the fresh parse fails — or ``_trees_for`` returns
``None`` — the handler simply bails by returning ``None``; there is no
cached-tree fallback (which would re-introduce the documented concurrency
hazard).
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from pygls.lsp.server import LanguageServer

from dimfort.core import ts_parser as _ts
from dimfort.core.interactions import collect_interactions
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _trees_for
from dimfort.lsp.tree_nav import _identifier_at

if TYPE_CHECKING:
    from dimfort.core.interactions import InteractionPoint, SymbolReport
    from dimfort.core.multifile import WorksetResult


# Audit #16: cache ``collect_interactions`` reports by
# ``(symbol, scale)`` per WorksetResult identity. The panel fires
# ``dimfort/interactions`` after every panelInfo; a cursor parked on
# the same identifier (and the panel re-asks during e.g. a refresh
# burst) would otherwise re-scan every cached tree in the workset
# for that symbol on every call.
#
# Bound: unbounded per result, in practice O(distinct symbols
# queried this session); cleared whenever ``state.last_result``
# swaps to a new identity. didClose has no effect (not URI-keyed).
# 0.2.6 plan item #16 will swap this to an LRU keyed by
# ``(id(result), symbol_lc, scale)``.
_report_cache_lock = threading.Lock()
_report_cache_result: WorksetResult | None = None
_report_cache: dict[tuple[str, bool], SymbolReport] = {}


def _get_cached_report(
    result: WorksetResult, symbol: str, scale: bool,
) -> SymbolReport:
    """Return cached interactions report for ``(symbol, scale)``, computing on miss."""
    global _report_cache_result, _report_cache
    key = (symbol, scale)
    with _report_cache_lock:
        if _report_cache_result is not result:
            _report_cache_result = result
            _report_cache = {}
        cached = _report_cache.get(key)
        if cached is not None:
            return cached

    report = collect_interactions(result, symbol, scale=scale)

    with _report_cache_lock:
        # Only store if no concurrent caller swapped the result key.
        if _report_cache_result is result:
            _report_cache[key] = report
    return report


def _serialize_interaction_point(p: InteractionPoint) -> dict[str, Any]:
    """Flatten an :class:`InteractionPoint` into a JSON-friendly dict.

    Args:
        p: One interaction point produced by
            :func:`dimfort.core.interactions.collect_interactions`.

    Returns:
        A dict with stable string keys (``file``, ``line``, ``column``,
        ``scope``, ``kind``, ``unit``, ``snippet``) ready to ship over
        the LSP wire to a companion. ``unit`` is the rendered string
        form (``"?"`` for unknown), not a :class:`UnitExpr`.

    Note:
        Schema matches the contract documented in
        ``docs/design/interaction-points.md``; companions parse on
        these field names.
    """
    return {
        "file": p.file,
        "line": p.line,
        "column": p.column,
        "scope": p.scope,
        "kind": p.kind,            # declares | contributes | requires | uses
        "unit": p.unit_str,        # rendered unit, or "?" when unknown
        "snippet": p.snippet,
    }


def resolve(ls: LanguageServer, params: Any) -> dict[str, Any] | None:
    """Run the interactions report for the symbol under the cursor.

    Resolves the target symbol from either an explicit ``symbol``
    parameter or, when absent, the identifier sitting under
    ``position`` in the live document. Runs
    :func:`dimfort.core.interactions.collect_interactions` over the
    cached workset and packages the result for the panel/CLI wire.

    Args:
        ls: Active :class:`LanguageServer` whose workspace exposes the
            live document text used for the cursor-driven identifier
            lookup.
        params: Loosely-typed request payload. Recognised keys (either
            attribute or dict form): ``textDocument``/``text_document``
            (with ``uri``), ``position`` (with ``line`` / ``character``),
            ``symbol`` (explicit override), and ``scale`` (bool).

    Returns:
        A dict with keys ``symbol``, ``points`` (list of serialised
        interaction points), ``conflicts`` (list of conflict records),
        and ``hasConflict`` (bool). Returns ``None`` when no workset
        result is loaded, the URI cannot be located, the fresh parse
        fails, or no symbol could be identified.

    Note:
        Parses a fresh tree from the live document for the cursor
        lookup, so the handler does **not** need ``state.ts_handler_lock``.
        On any failure (no tree, no symbol) it bails with ``None``
        rather than falling back to the shared cached tree — the
        documented concurrency hazard makes the fallback unsafe.
    """
    def _get(obj: Any, key: str) -> Any:
        """Read ``key`` from either an attribute-style object or a dict.

        Args:
            obj: Source object — may expose ``key`` as an attribute
                (pygls/lsprotocol dataclass) or as a dict entry
                (raw JSON payload).
            key: Field name to look up.

        Returns:
            The field's value, or ``None`` when neither lookup form
            yields one.

        Note:
            Used to keep this handler tolerant of both wire shapes
            seen in practice: lsprotocol-typed params from pygls and
            plain dict payloads from companion clients.
        """
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
        # ``_trees_for`` confirms a tree exists for this URI; we no
        # longer fall back to the cached Tree on parse failure (see the
        # comment below).
        if _trees_for(uri) is None:
            return None
        # Parse failure → bail rather than walking the shared cached
        # tree. Tree-sitter Node traversal is not safe across concurrent
        # readers (the "permanent concurrency gotcha"); racing with
        # hover / definition / inlay can crash the parser natively.
        # A None return here means the interactions surface renders
        # nothing for this position — strictly better than racing.
        try:
            doc = ls.workspace.get_text_document(uri)
            source_bytes = doc.source.encode("utf-8")
            tree = _ts.parse_text(source_bytes)
        except Exception:
            return None
        line = _get(position, "line")
        character = _get(position, "character")
        if line is None or character is None:
            return None
        symbol = _identifier_at(tree, source_bytes, int(line) + 1, int(character) + 1)
    if not symbol:
        return None

    report = _get_cached_report(result, symbol, scale)

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
