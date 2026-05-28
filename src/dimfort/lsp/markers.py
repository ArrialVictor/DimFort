"""Pure marker helpers for the LSP panel / hover surfaces.

A "marker" is a 🟢/🔵/🟡/🔴 severity glyph. These map and aggregate them with
no dependency on server state or tree-sitter — extracted from ``server.py``
(the LSP-split refactor) so the feature modules can share one definition.

The 🔵 tier — "**assumed via `@unit_assume`**" — sits between 🟢 and 🟡 in
the worst-of aggregation order: a 🔵 child propagates 🔵 up to its parent
(unless the parent already has its own worse marker), and a 🟡/🔴 sibling
beats it. The semantic difference from 🟢: 🟢 says "I derived this from the
algebra"; 🔵 says "I accepted this because the source asked me to." See
docs/design/markers.md §4.6.
"""
from __future__ import annotations

from collections.abc import Iterable


def _marker_token(mark: str) -> str:
    """Map a 🟢/🔵/🟡/🔴 emoji to a wire-format-friendly token."""
    return {
        "🟢": "ok", "🔵": "assumed", "🟡": "warn", "🔴": "error",
    }.get(mark, "warn")


_MARKER_TOKEN_RANK = {"ok": 0, "assumed": 1, "warn": 2, "error": 3}


def _worst_token(*tokens: str) -> str:
    """Worst (highest-severity) of a set of marker tokens.

    Order: ``error`` > ``warn`` > ``assumed`` > ``ok``.
    """
    return max(tokens, key=lambda t: _MARKER_TOKEN_RANK.get(t, 1))


_MARKER_EMOJI_RANK = {"🟢": 0, "🔵": 1, "🟡": 2, "🔴": 3}


def _worst_emoji(*marks: str) -> str:
    """Worst (highest-severity) of a set of 🟢/🔵/🟡/🔴 markers."""
    return max(marks, key=lambda m: _MARKER_EMOJI_RANK.get(m, 1))


def _aggregate_marker(marks: Iterable[str]) -> str:
    """Worst-of aggregate: 🔴 > 🟡 > 🔵 > 🟢. Empty stream → 🟢."""
    worst = "🟢"
    for m in marks:
        if m == "🔴":
            return "🔴"
        if m == "🟡":
            worst = "🟡"
        elif m == "🔵" and worst == "🟢":
            worst = "🔵"
    return worst
