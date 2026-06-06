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

    Args:
        mark: Single-glyph marker string. Recognised values are
            ``"🟢"``, ``"🟡"``, ``"🔴"``, and the provenance overlay
            ``"🔵"``.

    Returns:
        The matching wire token (``"ok"``, ``"warn"``, ``"error"``, or
        ``"assumed"``). Unknown glyphs degrade to ``"warn"`` so a
        stray marker never silently turns into clean.

    Note:
        Used by the panel/hover renderers when serialising rows for
        companion clients; companions paint the glyph back from the
        token.
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

    Args:
        *tokens: One or more wire-format severity tokens (``"ok"``,
            ``"warn"``, ``"error"``). Unknown tokens are ranked as
            ``"warn"`` so a typo never silently degrades a worst-of
            comparison.

    Returns:
        The single token with the highest severity rank.

    Raises:
        ValueError: When called with zero arguments (``max`` of an
            empty iterable).

    Note:
        Pure helper; no module state, safe to call from any thread.
    """
    return max(tokens, key=lambda t: _MARKER_TOKEN_RANK.get(t, 1))


_MARKER_EMOJI_RANK = {"🟢": 0, "🟡": 1, "🔴": 2}


def _worst_emoji(*marks: str) -> str:
    """Worst (highest-severity) of a set of 🟢/🟡/🔴 markers.

    Three-tier emoji-domain analogue of :func:`_worst_token`. 🔵 is
    unranked and treated as 🟢 for aggregation purposes — the overlay
    glyph never beats a clean parent nor masks a 🟡/🔴 elsewhere.

    Args:
        *marks: One or more severity glyphs. Unknown glyphs are
            ranked as 🟢 so a stray character never escalates the
            aggregate.

    Returns:
        The single glyph with the highest severity rank.

    Raises:
        ValueError: When called with zero arguments (``max`` of an
            empty iterable).

    Note:
        Pure helper; mirrors the wire-token aggregator but stays in
        glyph space for renderers that haven't tokenised yet.
    """
    return max(marks, key=lambda m: _MARKER_EMOJI_RANK.get(m, 0))


def _aggregate_marker(marks: Iterable[str]) -> str:
    """Worst-of aggregate: 🔴 > 🟡 > 🟢. Empty stream → 🟢.

    🔵 is unranked overlay — ignored here; callers that want to surface
    it use the root-row's marker directly instead of this aggregate.

    Args:
        marks: Iterable of severity glyphs to fold. May be empty.

    Returns:
        🔴 if any input is 🔴; else 🟡 if any input is 🟡; else 🟢
        (including the empty-iterable case).

    Note:
        Short-circuits on the first 🔴 seen, so the iterable is not
        guaranteed to be fully consumed.
    """
    worst = "🟢"
    for m in marks:
        if m == "🔴":
            return "🔴"
        if m == "🟡":
            worst = "🟡"
    return worst
