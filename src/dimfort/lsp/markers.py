"""Pure marker helpers for the LSP panel / hover surfaces.

A **severity** marker is a 🟢/🟡/🔴 glyph, three-tier worst-of. These
helpers map and aggregate them with no dependency on server state or
tree-sitter — extracted from ``server.py`` (the LSP-split refactor) so
the feature modules can share one definition.

🔵 is **NOT a severity tier** — it's a per-row provenance overlay
("accepted via ``@unit_assume``"; see docs/design/markers.md §4.6),
applied at render time on the row that carries the assumption. It
does not participate in worst-of aggregation: a 🔵 row never beats a
🟢 parent's clean status nor masks a 🟡/🔴 elsewhere. The only place
🔵 appears in this module is :func:`_marker_token`'s wire-format
mapping, used by the panel so companions can paint 🔵 from the
``"assumed"`` token.
"""
from __future__ import annotations

from collections.abc import Iterable


def _marker_token(mark: str) -> str:
    """Map a marker glyph to its wire-format token.

    Severity glyphs (🟢/🟡/🔴) map to ``ok``/``warn``/``error``. The
    provenance overlay 🔵 maps to ``assumed`` — companions render it
    as 🔵, but it never participates in worst-of aggregation.
    """
    return {
        "🟢": "ok", "🔵": "assumed", "🟡": "warn", "🔴": "error",
    }.get(mark, "warn")


_MARKER_TOKEN_RANK = {"ok": 0, "warn": 1, "error": 2}


def _worst_token(*tokens: str) -> str:
    """Worst (highest-severity) of a set of marker tokens.

    Three-tier severity order: ``error`` > ``warn`` > ``ok``. The
    ``assumed`` overlay is **not** ranked — callers strip it before
    invoking this helper (typically by mapping ``assumed`` → ``ok``
    in their aggregation step).
    """
    return max(tokens, key=lambda t: _MARKER_TOKEN_RANK.get(t, 1))


_MARKER_EMOJI_RANK = {"🟢": 0, "🟡": 1, "🔴": 2}


def _worst_emoji(*marks: str) -> str:
    """Worst (highest-severity) of a set of 🟢/🟡/🔴 markers. 🔵 is
    unranked and treated as 🟢 for aggregation."""
    return max(marks, key=lambda m: _MARKER_EMOJI_RANK.get(m, 0))


def _aggregate_marker(marks: Iterable[str]) -> str:
    """Worst-of aggregate: 🔴 > 🟡 > 🟢. Empty stream → 🟢.

    🔵 is unranked overlay — ignored here; callers that want to surface
    it use the root-row's marker directly instead of this aggregate.
    """
    worst = "🟢"
    for m in marks:
        if m == "🔴":
            return "🔴"
        if m == "🟡":
            worst = "🟡"
    return worst
