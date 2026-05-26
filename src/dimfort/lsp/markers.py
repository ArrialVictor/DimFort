"""Pure marker helpers for the LSP panel / hover surfaces.

A "marker" is a 🟢/🟡/🔴 severity glyph. These map and aggregate them with
no dependency on server state or tree-sitter — extracted from ``server.py``
(the LSP-split refactor) so the feature modules can share one definition.
"""
from __future__ import annotations


def _marker_token(mark: str) -> str:
    """Map a 🟢/🟡/🔴 emoji to a wire-format-friendly token."""
    return {"🟢": "ok", "🟡": "warn", "🔴": "error"}.get(mark, "warn")


_MARKER_TOKEN_RANK = {"ok": 0, "warn": 1, "error": 2}


def _worst_token(*tokens: str) -> str:
    """Worst (highest-severity) of a set of marker tokens: error>warn>ok."""
    return max(tokens, key=lambda t: _MARKER_TOKEN_RANK.get(t, 1))


_MARKER_EMOJI_RANK = {"🟢": 0, "🟡": 1, "🔴": 2}


def _worst_emoji(*marks: str) -> str:
    """Worst (highest-severity) of a set of 🟢/🟡/🔴 markers."""
    return max(marks, key=lambda m: _MARKER_EMOJI_RANK.get(m, 1))


def _aggregate_marker(marks) -> str:
    """Worst-of aggregate: 🔴 > 🟡 > 🟢. Empty stream → 🟢."""
    worst = "🟢"
    for m in marks:
        if m == "🔴":
            return "🔴"
        if m == "🟡":
            worst = "🟡"
    return worst
