"""Configurable comment-delimiter patterns for the three unit directive families.

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

    A match is reported even when the inner captured text is empty or
    whitespace-only (``unit_text == ""``); the scanner decides whether
    to demote it to a malformed annotation. Keeping the decision out
    here lets the scanner emit a code with line / column context.

    Attributes:
        unit_text: Inner unit substring, surrounding whitespace
            stripped.
        payload: ``StructuredPattern`` payload (whitespace stripped),
            or ``None`` for plain ``UnitPattern`` matches.
        start: 0-based index of the leading character of ``open`` in
            the source text.
        end: 0-based index one past the trailing character of
            ``close``. Callers translate ``start`` / ``end`` to source
            columns.
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
    :attr:`PatternMatch.unit_text`.

    Attributes:
        open: Opening delimiter literal.
        close: Closing delimiter literal.
    """

    open: str
    close: str

    def find(self, text: str) -> list[PatternMatch]:
        """Return every ``<open>...<close>`` match in ``text``.

        Args:
            text: Comment body to scan.

        Returns:
            Matches in source order; empty when no pair occurs or
            either delimiter is empty.
        """
        return _find_pairs(text, self.open, self.close, sep=None)


@dataclass(frozen=True)
class StructuredPattern:
    """``@unit_assume{}`` / ``@unit_affine_conversion{}``-family delimiter triple.

    Matches ``<open><unit><sep><payload><close>``. The unit text and
    payload are each whitespace-stripped. A match without ``sep``
    between ``open`` and ``close`` is NOT reported (it would be a
    malformed assume / affine directive, but distinguishing "no
    match" from "match-but-malformed" is the scanner's job).

    Attributes:
        open: Opening delimiter literal.
        close: Closing delimiter literal.
        sep: Inner separator splitting unit text from payload.
    """

    open: str
    close: str
    sep: str

    def find(self, text: str) -> list[PatternMatch]:
        """Return every ``<open><unit><sep><payload><close>`` match in ``text``.

        Args:
            text: Comment body to scan.

        Returns:
            Matches in source order; pairs without an inner ``sep``
            are skipped silently.
        """
        return _find_pairs(text, self.open, self.close, sep=self.sep)


def _find_pairs(
    text: str, open_: str, close: str, *, sep: str | None
) -> list[PatternMatch]:
    """Scan ``text`` for ``<open>...<close>`` (optionally split by ``sep``).

    Args:
        text: Comment body to scan.
        open_: Opening delimiter literal; an empty string yields no
            matches.
        close: Closing delimiter literal; an empty string yields no
            matches.
        sep: When set, the inner substring must contain this separator;
            ``unit_text`` and ``payload`` are taken from either side of
            its first occurrence. When ``None``, the inner substring
            becomes ``unit_text`` whole and ``payload`` stays ``None``.

    Returns:
        Matches in source order. Non-overlapping for the ``sep=None``
        path; when ``sep`` is set and the inner has no separator,
        scanning resumes just past the open so later non-malformed
        pairs are still found.
    """
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
    """Compile config-shaped ``UnitPatternEntry`` rows into runtime patterns.

    Args:
        entries: Configured ``@unit{}``-family delimiter pairs.

    Returns:
        Tuple of :class:`UnitPattern` in input order.
    """
    return tuple(UnitPattern(open=e.open, close=e.close) for e in entries)


def compile_structured_patterns(
    entries: Iterable[StructuredPatternEntry],
) -> tuple[StructuredPattern, ...]:
    """Compile config-shaped ``StructuredPatternEntry`` rows into runtime patterns.

    Args:
        entries: Configured structured-directive delimiter triples.

    Returns:
        Tuple of :class:`StructuredPattern` in input order.
    """
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
    """The match the scanner should apply, plus any conflicting matches.

    Attributes:
        pattern_index: 0-based index into the input pattern tuple of
            the pattern whose match was selected (first listed, per
            spec §8.1).
        match: The selected pattern's capture.
        conflicts: ``(pattern_index, match)`` pairs from later
            patterns whose capture's ``unit_text`` differs from the
            selected one's (whitespace-stripped equality, per spec
            §8.2). Empty when no later pattern matched, or when every
            later pattern's capture text agreed with the selected
            one.
    """

    pattern_index: int
    match: PatternMatch
    conflicts: tuple[tuple[int, PatternMatch], ...]


def select_match(
    patterns: Iterable[UnitPattern | StructuredPattern], text: str
) -> PatternHit | None:
    """Find each pattern's first occurrence in ``text`` and pick the winner.

    Applies the spec §8.1 / §8.2 selection rules:

    - The first pattern in iteration order that matches at all wins.
    - Later patterns that also match are reported in ``conflicts``
      iff their ``unit_text`` differs from the winner's (a U021
      candidate; identical captures are silently dropped per §8.2).

    Args:
        patterns: Patterns to try, in priority order.
        text: Comment body to scan.

    Returns:
        The :class:`PatternHit` describing the winner and any
        conflicting later matches, or ``None`` when no pattern matched.
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
