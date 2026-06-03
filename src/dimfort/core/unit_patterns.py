"""Configurable comment-delimiter patterns for the three unit
directive families.

The scanner consumes ``UnitPattern`` / ``StructuredPattern`` objects
to find directive captures in comment text. Patterns use
literal-string ``str.find()`` matching — no regex, no escaping
concerns — so user-configured delimiters (e.g. ``[``/``]``) work
unchanged.

Spec: docs/design/unit-comment-delimiters.md §2, §8, §11.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from dimfort.config import (
    DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS,
    DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS,
    DEFAULT_UNIT_COMMENT_DELIMITERS,
    StructuredPatternEntry,
    UnitPatternEntry,
)


@dataclass(frozen=True)
class PatternMatch:
    """One occurrence of a configured pattern in a comment body.

    ``unit_text`` and ``payload`` are stripped of surrounding
    whitespace. ``payload`` is ``None`` for plain ``UnitPattern``
    matches and a non-empty string for ``StructuredPattern`` matches
    (where the separator splits unit from payload).

    ``start`` is the 0-based index of the leading character of
    ``open`` in the source text; ``end`` is the 0-based index one
    past the trailing character of ``close``. Callers translate
    these to source columns.

    A match is reported even when the inner captured text is empty
    or whitespace-only (``unit_text == ""``); the scanner decides
    whether to demote it to a malformed annotation. Keeping the
    decision out here lets the scanner emit a code with line / column
    context.
    """

    unit_text: str
    payload: str | None
    start: int
    end: int


@dataclass(frozen=True)
class UnitPattern:
    """``@unit{}``-family delimiter pair.

    Matches any substring of the form ``<open>...<close>`` in a
    comment body. The inner substring (whitespace-stripped) becomes
    ``unit_text``.
    """

    open: str
    close: str

    def find(self, text: str) -> list[PatternMatch]:
        return _find_pairs(text, self.open, self.close, sep=None)


@dataclass(frozen=True)
class StructuredPattern:
    """``@unit_assume{}`` / ``@unit_affine_conversion{}``-family
    delimiter triple.

    Matches ``<open><unit><sep><payload><close>``. The unit text and
    payload are each whitespace-stripped. A match without ``sep``
    between ``open`` and ``close`` is NOT reported (it would be a
    malformed assume / affine directive, but distinguishing "no
    match" from "match-but-malformed" is the scanner's job).
    """

    open: str
    close: str
    sep: str

    def find(self, text: str) -> list[PatternMatch]:
        return _find_pairs(text, self.open, self.close, sep=self.sep)


def _find_pairs(
    text: str, open_: str, close: str, *, sep: str | None
) -> list[PatternMatch]:
    matches: list[PatternMatch] = []
    if not open_ or not close:
        return matches
    i = 0
    n = len(text)
    while i < n:
        start = text.find(open_, i)
        if start == -1:
            break
        inner_start = start + len(open_)
        close_at = text.find(close, inner_start)
        if close_at == -1:
            break
        inner = text[inner_start:close_at]
        end = close_at + len(close)
        if sep is None:
            matches.append(
                PatternMatch(
                    unit_text=inner.strip(),
                    payload=None,
                    start=start,
                    end=end,
                )
            )
        else:
            sep_at = inner.find(sep)
            if sep_at == -1:
                # No separator inside — not a structured-directive
                # match. Skip past this open and keep scanning so
                # later overlapping matches aren't lost.
                i = inner_start
                continue
            unit_part = inner[:sep_at].strip()
            payload = inner[sep_at + len(sep):].strip()
            matches.append(
                PatternMatch(
                    unit_text=unit_part,
                    payload=payload,
                    start=start,
                    end=end,
                )
            )
        i = end
    return matches


# ---------------------------------------------------------------------------
# Compilation helpers
# ---------------------------------------------------------------------------


def compile_unit_patterns(
    entries: Iterable[UnitPatternEntry],
) -> tuple[UnitPattern, ...]:
    return tuple(UnitPattern(open=e.open, close=e.close) for e in entries)


def compile_structured_patterns(
    entries: Iterable[StructuredPatternEntry],
) -> tuple[StructuredPattern, ...]:
    return tuple(
        StructuredPattern(open=e.open, close=e.close, sep=e.sep) for e in entries
    )


# Module-level compiled defaults so the scanner can keep its
# zero-arg call site working in test and ad-hoc tooling. The CLI /
# LSP override via DimfortConfig.
DEFAULT_UNIT_PATTERNS: tuple[UnitPattern, ...] = compile_unit_patterns(
    DEFAULT_UNIT_COMMENT_DELIMITERS
)
DEFAULT_ASSUME_PATTERNS: tuple[StructuredPattern, ...] = compile_structured_patterns(
    DEFAULT_UNIT_ASSUME_COMMENT_DELIMITERS
)
DEFAULT_AFFINE_PATTERNS: tuple[StructuredPattern, ...] = compile_structured_patterns(
    DEFAULT_UNIT_AFFINE_COMMENT_DELIMITERS
)


# ---------------------------------------------------------------------------
# Match orchestration — first-match-wins with conflict detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternHit:
    """The match the scanner should apply, plus any conflicting
    matches from later-listed patterns.

    ``pattern_index`` is the 0-based index into the input pattern
    tuple of the pattern whose match was selected (first listed, per
    spec §8.1). ``match`` is its capture. ``conflicts`` is the list
    of `(pattern_index, match)` pairs from later patterns whose
    capture's ``unit_text`` differs from the selected one's
    (whitespace-stripped equality, per spec §8.2). ``conflicts`` is
    empty when no later pattern matched, or when every later
    pattern's capture text agreed with the selected one.
    """

    pattern_index: int
    match: PatternMatch
    conflicts: tuple[tuple[int, PatternMatch], ...]


def select_match(
    patterns: Iterable[UnitPattern | StructuredPattern], text: str
) -> PatternHit | None:
    """Find each pattern's first occurrence in ``text``, then apply
    the spec §8.1 / §8.2 selection rules.

    - The first pattern in iteration order that matches at all wins.
    - Later patterns that also match are reported in ``conflicts``
      iff their ``unit_text`` differs from the winner's (a U021
      candidate; identical captures are silently dropped per §8.2).
    """
    winner_idx: int | None = None
    winner_match: PatternMatch | None = None
    conflicts: list[tuple[int, PatternMatch]] = []
    for idx, pattern in enumerate(patterns):
        hits = pattern.find(text)
        if not hits:
            continue
        first = hits[0]
        if winner_match is None:
            winner_idx = idx
            winner_match = first
            continue
        if first.unit_text != winner_match.unit_text:
            conflicts.append((idx, first))
    if winner_match is None or winner_idx is None:
        return None
    return PatternHit(
        pattern_index=winner_idx,
        match=winner_match,
        conflicts=tuple(conflicts),
    )
