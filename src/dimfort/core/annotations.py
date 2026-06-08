"""Doxygen ``@unit{...}`` annotation scanner.

Stage 1: produces two streams from a Fortran source file —
``RawAnnotation`` records (every ``@unit{...}`` occurrence) and
``DeclarationSite`` records (every ``real :: x``, ``integer :: i``,
etc. statement). Stage 2 (:mod:`dimfort.core.attach`) joins them by
physical line range.

Two scanners, two independent reasons for existing:

- The **comment scanner** (everything in the ``@unit`` extraction
  section below) is the only place that needs to understand Fortran
  string literals so a ``!`` inside ``"hello!"`` isn't mistaken for a
  comment marker. Kept hand-written and string-aware.
- The **declaration scanner** uses tree-sitter (``core.ts_parser``).
  Previously regex-based — but a regex scanner that handles F90 +
  F77-flavoured idioms + continuations + derived-type blocks
  accurately is hard to maintain, and the LFortran-AST-based approach
  it replaced suffered from position drift on ``&``-continuations.
  Tree-sitter is the third (and we hope final) implementation: byte-
  exact positions, real grammar, handles every fixture we cover.

Restrictions in v1 (apply to any configured unit pattern — the
``@unit{...}`` token is the default, but since 0.2.2 the open/close
delimiters are project-configurable):

- The annotation must fit on one source line. Multi-line forms across
  ``!>`` continuation lines are not parsed.
- At most one unit annotation per comment line. A second one is
  reported as :class:`MalformedAnnotation`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tree_sitter import Node, Tree

from dimfort.core import ts_parser as _ts
from dimfort.core.unit_patterns import (
    DEFAULT_AFFINE_PATTERNS,
    DEFAULT_ASSUME_PATTERNS,
    DEFAULT_UNIT_PATTERNS,
    PatternMatch,
    StructuredPattern,
    UnitPattern,
)


class AnnotationKind(StrEnum):
    """Where the annotation attaches relative to its declaration.

    Attributes:
        PRE: Comment marked ``!>`` or ``!!`` preceding the declaration
            (and possibly continued by further ``!!`` lines).
        POST: Comment marked ``!<`` trailing the declaration on the
            same source line.
    """

    PRE = "pre"   # !> or !! preceding the declaration
    POST = "post"  # !< trailing the declaration


@dataclass(frozen=True)
class RawAnnotation:
    """One ``@unit{...}`` occurrence as found by the scanner.

    Attributes:
        kind: Whether the annotation precedes (PRE) or trails (POST) its
            target declaration.
        line: 1-based physical line number of the comment.
        column: 1-based column where the matched opening delimiter
            begins.
        unit_text: Inner capture between the matched pattern's open and
            close delimiters (configurable since 0.2.2), stripped of
            surrounding whitespace.
        end_column: 1-based column one past the closing delimiter.
            Defaults to ``0`` for back-compat callers that only need
            ``column``.
    """

    kind: AnnotationKind
    line: int        # 1-based physical line of the comment
    column: int      # 1-based column where `@unit{` begins
    # Inner capture between the matched pattern's open/close delimiters
    # (configurable since 0.2.2), stripped of surrounding whitespace.
    unit_text: str
    end_column: int = 0  # 1-based column one past the closing delimiter


@dataclass(frozen=True)
class RawAssume:
    """One ``@unit_assume{ <unit> : <reason> }`` occurrence.

    The escape hatch: on an assignment line it tells the checker to stop
    deriving the RHS unit (suppressing D1.4 and any interior fire) and
    instead treat the result as ``unit_text``, still consistency-checked
    against the LHS. ``reason`` is mandatory (a category plus free text,
    e.g. ``empirical-fit: Brandes 2007``) so every assumption is
    auditable.

    Attributes:
        line: 1-based physical line of the comment.
        column: 1-based column where the matched opening delimiter
            begins.
        end_column: 1-based column just past the closing delimiter
            (exclusive end).
        unit_text: The asserted unit (e.g. ``"kg/m^3"``).
        reason: Mandatory justification (a category plus free text).
    """

    line: int        # 1-based physical line of the comment
    column: int      # 1-based column where `@unit_assume{` begins
    end_column: int  # 1-based column just past the closing `}` (exclusive end)
    unit_text: str   # the asserted unit, e.g. "kg/m^3"
    reason: str      # mandatory justification (category + text)


@dataclass(frozen=True)
class RawAffineConv:
    """One ``@unit_affine_conversion{ <src> -> <tgt> }`` occurrence.

    A *verified* affine-conversion directive (Phase 2c, scale.md §11): on
    an assignment line it asserts the statement converts a ``src``-typed
    quantity into the ``tgt`` frame (e.g. ``degC -> K``). Unlike
    ``@unit_assume`` it carries no reason and needs no registry — the
    checker *verifies* the arithmetic against the known offsets and
    errors (S003) if it doesn't fit. ``->`` is primary; ``,`` is accepted
    as a synonym separator.

    Attributes:
        line: 1-based physical line of the comment.
        column: 1-based column where the matched opening delimiter
            begins.
        src: Source unit name (e.g. ``"degC"``).
        tgt: Target unit name (e.g. ``"K"``).
        end_column: 1-based column one past the closing delimiter.
            Defaults to ``0`` for back-compat callers.
    """

    line: int        # 1-based physical line of the comment
    column: int      # 1-based column where `@unit_affine_conversion{` begins
    src: str         # source unit name, e.g. "degC"
    tgt: str         # target unit name, e.g. "K"
    end_column: int = 0  # 1-based column one past the closing delimiter


@dataclass(frozen=True)
class MalformedAnnotation:
    """A ``@unit``-family invocation that the scanner could not parse.

    Attributes:
        line: 1-based physical line of the offending comment.
        column: 1-based column of the open delimiter that started the
            malformed match.
        reason: Human-readable explanation surfaced through the U001
            emitter.
        end_column: 1-based column one past the closing delimiter of the
            offending token. When ``0`` (back-compat default), the U001
            emitter widens to ``column + 1`` so the squiggle covers at
            least one character.
    """

    line: int
    column: int
    reason: str
    # 1-based column one past the closing delimiter of the offending
    # token. When ``0`` (back-compat default), the U001 emitter widens
    # to ``column + 1`` so the squiggle covers at least one character.
    end_column: int = 0


@dataclass(frozen=True)
class WrongStatementKind:
    """A directive landed on a statement of the wrong kind (spec §8.3 → U023).

    For example, ``@unit_assume`` on a ``real :: x`` declaration, or
    ``@unit`` on a ``v = 1.0`` assignment. The directive is dropped
    (not attached). The diagnostic emitter names the directive found,
    the statement kind it landed on, and which directive would attach
    correctly there.

    Attributes:
        line: 1-based physical line of the comment carrying the
            directive.
        column: 1-based column of the matched opening delimiter.
        end_column: 1-based column one past the closing delimiter.
        directive_found: Name of the directive that fired (e.g.
            ``"@unit_assume"``).
        landed_on: Kind of statement actually present at the target
            line — ``"declaration"`` or ``"assignment"``.
        expected_directive: Name of the directive that would have
            attached correctly to ``landed_on``.
    """

    line: int
    column: int
    end_column: int               # 1-based column one past the closing delimiter
    directive_found: str          # e.g. "@unit_assume"
    landed_on: str                # "declaration" / "assignment"
    expected_directive: str       # what would attach correctly here


@dataclass(frozen=True)
class PatternConflict:
    """Two configured patterns matched the same comment with disagreeing capture text.

    Spec §8.2 → U021. The first-listed pattern's capture is the one
    applied to the statement; this record is the input the U021 emitter
    uses to point the user at both captures.

    Attributes:
        line: 1-based physical line of the comment.
        column: 1-based column of the LATER pattern's opening
            delimiter.
        end_column: 1-based column one past its closing delimiter.
        directive: One of ``"@unit"``, ``"@unit_assume"``,
            ``"@unit_affine_conversion"``.
        first_unit_text: Winning capture (from the first-listed
            pattern).
        second_unit_text: Losing capture (from the later-listed
            pattern).
        first_pattern_index: Position of the winning pattern in the
            configured pattern list.
        second_pattern_index: Position of the losing pattern in the
            configured pattern list.
    """

    line: int
    column: int                   # 1-based column of the LATER pattern's open
    end_column: int               # 1-based column one past its closing delimiter
    directive: str                # "@unit" / "@unit_assume" / "@unit_affine_conversion"
    first_unit_text: str          # winner capture
    second_unit_text: str         # loser capture
    first_pattern_index: int
    second_pattern_index: int


# ---------------------------------------------------------------------------
# String-aware comment detection
# ---------------------------------------------------------------------------


def _comment_start(line: str) -> int | None:
    """Find the column of the first ``!`` that opens a comment.

    Tracks single- and double-quoted strings so a ``!`` inside a literal
    isn't mistaken for a comment marker. Doubled quotes (``''`` / ``""``)
    inside a string are the Fortran escape and don't close it.

    Args:
        line: One physical source line, without the trailing newline.

    Returns:
        Zero-based column of the comment-opening ``!``, or ``None`` if
        the line carries no comment.

    Note:
        Fast path — the vast majority of Fortran lines contain no
        string literal, so a single ``str.find("!")`` is enough. Only
        when a quote is actually present do we fall back to the quote-
        aware character scan. Profiling on a large workspace (1.75M
        calls) showed this function and its per-iteration ``len(line)``
        accounted for ~9 seconds; the fast path drops it under 1.
    """
    if "'" not in line and '"' not in line:
        idx = line.find("!")
        return idx if idx != -1 else None

    n = len(line)
    in_quote: str | None = None
    i = 0
    while i < n:
        c = line[i]
        if in_quote is None:
            if c == "!":
                return i
            if c == "'" or c == '"':
                in_quote = c
        else:
            if c == in_quote:
                if i + 1 < n and line[i + 1] == in_quote:
                    i += 1  # escaped pair
                else:
                    in_quote = None
        i += 1
    return None


# ---------------------------------------------------------------------------
# Doxygen marker + @unit extraction
# ---------------------------------------------------------------------------

# After the leading `!`, a Doxygen marker is one of `>`, `<`, `!`.
# `!>` / `!!`  → PRE (preceding block, possibly continued by further `!!`)
# `!<`         → POST (trailing on the same line)
_DOX_MARKER = {">": AnnotationKind.PRE, "!": AnnotationKind.PRE, "<": AnnotationKind.POST}

def _doxygen_kind(comment_text: str) -> AnnotationKind | None:
    """Classify a comment by its Doxygen marker.

    Args:
        comment_text: Everything in the source line after the opening
            ``!`` character.

    Returns:
        :class:`AnnotationKind.PRE` for ``!>`` or ``!!``,
        :class:`AnnotationKind.POST` for ``!<``, and ``None`` for plain
        ``!`` comments. The Doxygen marker character is consumed; the
        caller scans the rest of the line for the configured unit
        directive.
    """
    if not comment_text:
        return None
    return _DOX_MARKER.get(comment_text[0])


# Per-directive descriptors used in MalformedAnnotation / PatternConflict
# messages so the unified extractor below can format errors that look
# the same as the previous per-directive scanners produced.
_UNIT_DIR_NAME = "@unit"
_ASSUME_DIR_NAME = "@unit_assume"
_AFFINE_DIR_NAME = "@unit_affine_conversion"


def _open_implies_brace(pat: UnitPattern | StructuredPattern) -> bool:
    """Report whether a pattern's open delimiter ends with ``{``.

    Args:
        pat: A configured ``@unit{}``-family pattern.

    Returns:
        ``True`` when the open delimiter ends with ``{``. Such patterns
        trigger the historical ``unclosed '{' in @unit`` malformed-
        annotation when an opener is found without a closing brace.
        Patterns with other closers (e.g. bracket-style ``[``/``]``)
        don't fire that diagnostic.
    """
    return pat.open.endswith("{")


def _find_unsupported_open(
    body: str, patterns: tuple[UnitPattern | StructuredPattern, ...]
) -> tuple[int, UnitPattern | StructuredPattern] | None:
    """Detect a ``{``-style pattern opener with no matching close.

    Args:
        body: Comment body to scan.
        patterns: Configured patterns to consider; only patterns whose
            open ends with ``{`` are tested.

    Returns:
        ``(index, pattern)`` for the first such unclosed opener, where
        ``index`` is the position of the open within ``body``. ``None``
        if no unclosed ``{``-style opener is present.

    Note:
        Used to preserve the pre-0.2.2 ``unclosed '{' in @unit``
        malformed-annotation behavior.
    """
    for pat in patterns:
        if not _open_implies_brace(pat):
            continue
        idx = body.find(pat.open)
        if idx == -1:
            continue
        if body.find(pat.close, idx + len(pat.open)) == -1:
            return idx, pat
    return None


def _select_unit(
    body: str, line_no: int, body_col_offset: int, kind: AnnotationKind,
    patterns: tuple[UnitPattern, ...],
) -> tuple[
    list[RawAnnotation], list[MalformedAnnotation], list[PatternConflict],
    int | None,
]:
    """Run the ``@unit{}``-family extractor on a single comment body.

    Implements spec §2 and §8 against the configured pattern list.

    Args:
        body: Comment body text (already stripped of the leading ``!``
            and any Doxygen marker character).
        line_no: 1-based physical line number of the comment.
        body_col_offset: 1-based column at which ``body[0]`` sits in
            the original source line.
        kind: PRE or POST classification of the comment.
        patterns: Configured ``@unit{}``-family patterns to try, in
            precedence order.

    Returns:
        A ``(annotations, errors, conflicts, winner_idx)`` tuple. The
        trailing ``int | None`` is the winning pattern's index within
        ``patterns`` when one was selected — the caller uses it to
        apply the spec §6 multi-var-skip rule. ``None`` if no pattern
        matched.
    """
    if not patterns:
        return [], [], [], None
    hits_per_pattern: list[tuple[int, UnitPattern, list[PatternMatch]]] = []
    for idx, pat in enumerate(patterns):
        ms = pat.find(body)
        if ms:
            hits_per_pattern.append((idx, pat, ms))
    if not hits_per_pattern:
        unclosed = _find_unsupported_open(body, tuple(patterns))
        if unclosed is not None:
            idx_in_body, _pat = unclosed
            return [], [MalformedAnnotation(
                line_no, body_col_offset + idx_in_body,
                f"unclosed '{{' in {_UNIT_DIR_NAME}",
            )], [], None
        return [], [], [], None
    winner_idx, winner_pat, winner_hits = hits_per_pattern[0]
    first = winner_hits[0]
    col = body_col_offset + first.start

    annotations: list[RawAnnotation] = []
    errors: list[MalformedAnnotation] = []
    if len(winner_hits) > 1:
        # Multiple captures on one line — ambiguous intent. Drop ALL
        # captures so the variable surfaces as unannotated (U005 will
        # nudge the user to fix the comment) rather than silently
        # picking the first match. Every capture site is flagged so
        # the user sees the full extent of the ambiguity.
        for extra in winner_hits:
            errors.append(MalformedAnnotation(
                line_no, body_col_offset + extra.start,
                f"more than one {_UNIT_DIR_NAME} on one line",
                end_column=body_col_offset + extra.end,
            ))
    elif first.unit_text == "":
        errors.append(MalformedAnnotation(
            line_no, col, f"empty {winner_pat.open}{winner_pat.close}",
            end_column=body_col_offset + first.end,
        ))
    else:
        annotations.append(RawAnnotation(
            kind=kind, line=line_no, column=col,
            unit_text=first.unit_text,
            end_column=body_col_offset + first.end,
        ))

    conflicts: list[PatternConflict] = []
    for idx, _pat, hits in hits_per_pattern[1:]:
        other = hits[0]
        if other.unit_text and other.unit_text != first.unit_text:
            conflicts.append(PatternConflict(
                line=line_no, column=body_col_offset + other.start,
                end_column=body_col_offset + other.end,
                directive=_UNIT_DIR_NAME,
                first_unit_text=first.unit_text,
                second_unit_text=other.unit_text,
                first_pattern_index=winner_idx,
                second_pattern_index=idx,
            ))
    return annotations, errors, conflicts, winner_idx


def _select_assume(
    body: str, line_no: int, body_col_offset: int,
    patterns: tuple[StructuredPattern, ...],
) -> tuple[
    list[RawAssume], list[MalformedAnnotation], list[PatternConflict]
]:
    """Run the ``@unit_assume{}``-family extractor on a comment body.

    Args:
        body: Comment body text (already stripped of the leading ``!``
            and any Doxygen marker character).
        line_no: 1-based physical line number of the comment.
        body_col_offset: 1-based column at which ``body[0]`` sits in
            the original source line.
        patterns: Configured ``@unit_assume{}``-family patterns to try,
            in precedence order.

    Returns:
        A ``(assumes, errors, conflicts)`` tuple. ``errors`` may carry
        a malformed-annotation report when an opener was found but the
        body had no matching close, an empty unit, an empty reason, or
        a missing separator.
    """
    if not patterns:
        return [], [], []
    hits_per_pattern: list[tuple[int, StructuredPattern, list[PatternMatch]]] = []
    for idx, pat in enumerate(patterns):
        ms = pat.find(body)
        if ms:
            hits_per_pattern.append((idx, pat, ms))
    if not hits_per_pattern:
        # Distinguish "open present but unclosed" from "open present but
        # missing sep" — both used to surface as MalformedAnnotation.
        for pat in patterns:
            if not _open_implies_brace(pat):
                continue
            i = body.find(pat.open)
            if i == -1:
                continue
            close_at = body.find(pat.close, i + len(pat.open))
            if close_at == -1:
                return [], [MalformedAnnotation(
                    line_no, body_col_offset + i,
                    f"unclosed '{{' in {_ASSUME_DIR_NAME}",
                )], []
            inner = body[i + len(pat.open):close_at]
            if pat.sep not in inner:
                return [], [MalformedAnnotation(
                    line_no, body_col_offset + i,
                    f"{_ASSUME_DIR_NAME} requires '{{ <unit> : <reason> }}' "
                    f"(missing '{pat.sep}' separating unit from reason)",
                )], []
        return [], [], []

    winner_idx, winner_pat, winner_hits = hits_per_pattern[0]
    first = winner_hits[0]
    col = body_col_offset + first.start
    end_col = body_col_offset + first.end

    assumes: list[RawAssume] = []
    errors: list[MalformedAnnotation] = []
    if len(winner_hits) > 1:
        for extra in winner_hits:
            errors.append(MalformedAnnotation(
                line_no, body_col_offset + extra.start,
                f"more than one {_ASSUME_DIR_NAME} on one line",
                end_column=body_col_offset + extra.end,
            ))
    elif first.unit_text == "":
        errors.append(MalformedAnnotation(
            line_no, col, f"empty unit in {_ASSUME_DIR_NAME}",
            end_column=end_col,
        ))
    elif not first.payload:
        errors.append(MalformedAnnotation(
            line_no, col,
            f"empty reason in {_ASSUME_DIR_NAME} "
            "(a justification is required)",
            end_column=end_col,
        ))
    else:
        assumes.append(RawAssume(
            line=line_no, column=col, end_column=end_col,
            unit_text=first.unit_text, reason=first.payload,
        ))

    conflicts: list[PatternConflict] = []
    for idx, _pat, hits in hits_per_pattern[1:]:
        other = hits[0]
        if other.unit_text and other.unit_text != first.unit_text:
            conflicts.append(PatternConflict(
                line=line_no, column=body_col_offset + other.start,
                end_column=body_col_offset + other.end,
                directive=_ASSUME_DIR_NAME,
                first_unit_text=first.unit_text,
                second_unit_text=other.unit_text,
                first_pattern_index=winner_idx,
                second_pattern_index=idx,
            ))
    return assumes, errors, conflicts


def _select_affine(
    body: str, line_no: int, body_col_offset: int,
    patterns: tuple[StructuredPattern, ...],
) -> tuple[
    list[RawAffineConv], list[MalformedAnnotation], list[PatternConflict]
]:
    """Run the ``@unit_affine_conversion{}``-family extractor on a comment body.

    Args:
        body: Comment body text (already stripped of the leading ``!``
            and any Doxygen marker character).
        line_no: 1-based physical line number of the comment.
        body_col_offset: 1-based column at which ``body[0]`` sits in
            the original source line.
        patterns: Configured ``@unit_affine_conversion{}``-family
            patterns to try, in precedence order.

    Returns:
        A ``(affines, errors, conflicts)`` tuple. ``,`` is accepted as
        a legacy synonym for ``->`` on the canonical
        ``@unit_affine_conversion{`` open; that compatibility path is
        handled here rather than plumbed through
        :class:`StructuredPattern`.
    """
    if not patterns:
        return [], [], []
    hits_per_pattern: list[tuple[int, StructuredPattern, list[PatternMatch]]] = []
    for idx, pat in enumerate(patterns):
        ms = pat.find(body)
        if ms:
            hits_per_pattern.append((idx, pat, ms))

    # Legacy synonym: when the canonical ``@unit_affine_conversion{`` open
    # is present and a ``->`` separator is missing but a ``,`` is, accept
    # it as the pre-0.2.2 scanner did. We hand-roll this rather than
    # plumb it through StructuredPattern — synonym support is a one-off
    # for back-compat, not a general feature.
    if not hits_per_pattern:
        for pat in patterns:
            if pat.open != "@unit_affine_conversion{" or pat.sep != "->":
                continue
            i = body.find(pat.open)
            if i == -1:
                continue
            close_at = body.find(pat.close, i + len(pat.open))
            if close_at == -1:
                return [], [MalformedAnnotation(
                    line_no, body_col_offset + i,
                    f"unclosed '{{' in {_AFFINE_DIR_NAME}",
                )], []
            inner = body[i + len(pat.open):close_at]
            if "," in inner:
                src_part, tgt_part = inner.split(",", 1)
                src = src_part.strip()
                tgt = tgt_part.strip()
                if not src or not tgt:
                    return [], [MalformedAnnotation(
                        line_no, body_col_offset + i,
                        f"empty source or target unit in {_AFFINE_DIR_NAME}",
                    )], []
                return [RawAffineConv(
                    line=line_no, column=body_col_offset + i,
                    src=src, tgt=tgt,
                    end_column=body_col_offset + close_at + len(pat.close),
                )], [], []
            return [], [MalformedAnnotation(
                line_no, body_col_offset + i,
                f"{_AFFINE_DIR_NAME} requires '{{ <src> -> <tgt> }}' "
                "(missing '->' or ',' separating source from target)",
            )], []
        # No canonical open at all — defer to brace-detect for other
        # configured patterns.
        for pat in patterns:
            if not _open_implies_brace(pat):
                continue
            i = body.find(pat.open)
            if i == -1:
                continue
            close_at = body.find(pat.close, i + len(pat.open))
            if close_at == -1:
                return [], [MalformedAnnotation(
                    line_no, body_col_offset + i,
                    f"unclosed '{{' in {_AFFINE_DIR_NAME}",
                )], []
            inner = body[i + len(pat.open):close_at]
            if pat.sep not in inner:
                return [], [MalformedAnnotation(
                    line_no, body_col_offset + i,
                    f"{_AFFINE_DIR_NAME} requires '{{ <src> -> <tgt> }}' "
                    f"(missing '{pat.sep}' separating source from target)",
                )], []
        return [], [], []

    winner_idx, winner_pat, winner_hits = hits_per_pattern[0]
    first = winner_hits[0]
    col = body_col_offset + first.start

    affines: list[RawAffineConv] = []
    errors: list[MalformedAnnotation] = []
    src = first.unit_text
    tgt = first.payload or ""
    if len(winner_hits) > 1:
        for extra in winner_hits:
            errors.append(MalformedAnnotation(
                line_no, body_col_offset + extra.start,
                f"more than one {_AFFINE_DIR_NAME} on one line",
                end_column=body_col_offset + extra.end,
            ))
    elif not src or not tgt:
        errors.append(MalformedAnnotation(
            line_no, col,
            f"empty source or target unit in {_AFFINE_DIR_NAME}",
            end_column=body_col_offset + first.end,
        ))
    else:
        affines.append(RawAffineConv(
            line=line_no, column=col, src=src, tgt=tgt,
            end_column=body_col_offset + first.end,
        ))

    conflicts: list[PatternConflict] = []
    for idx, _pat, hits in hits_per_pattern[1:]:
        other = hits[0]
        if other.unit_text and other.unit_text != first.unit_text:
            conflicts.append(PatternConflict(
                line=line_no, column=body_col_offset + other.start,
                end_column=body_col_offset + other.end,
                directive=_AFFINE_DIR_NAME,
                first_unit_text=first.unit_text,
                second_unit_text=other.unit_text,
                first_pattern_index=winner_idx,
                second_pattern_index=idx,
            ))
    return affines, errors, conflicts


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclarationSite:
    """A single Fortran declaration (possibly continued across lines).

    Attributes:
        line_start: 1-based physical line carrying the type-spec.
        line_end: 1-based physical line of the last line of the
            statement (after any ``&``-continuations).
        names: Variable names declared on the statement, in source
            order.
        enclosing_type: Name of the enclosing ``type :: …`` block when
            this declaration is a field of a derived type, else
            ``None``.
        scope: Lower-cased name of the innermost enclosing
            ``subroutine`` / ``function``. ``None`` for declarations at
            module or file top level. Used by stage 2 (``attach``) to
            key annotations per scope so same-named arguments in two
            routines don't collide.
        intrinsic_type: Lower-cased intrinsic type qualifier
            (``real``, ``integer``, ``logical``, ``character``,
            ``complex``, ``double precision``, ``type``), or ``None``
            if not detected. Used by ``attach`` to inject an implicit
            dimensionless default for unannotated ``integer``
            declarations (counts / indices / loop iterators) so the
            U005 firehose is restricted to REAL variables where unit
            consistency actually matters.
    """

    line_start: int            # 1-based: the line containing the type-spec
    line_end: int              # 1-based: the last physical line of the statement
    names: tuple[str, ...]     # variable names declared, in source order
    enclosing_type: str | None = None  # type-block name, if inside `type :: …`
    # Lower-cased name of the innermost enclosing ``subroutine`` /
    # ``function``. ``None`` for declarations at module or file top
    # level. Used by stage 2 (``attach``) to key annotations per scope
    # so same-named arguments in two routines don't collide.
    scope: str | None = None
    # Lower-cased intrinsic type qualifier (``real``, ``integer``,
    # ``logical``, ``character``, ``complex``, ``double precision``,
    # ``type``). ``None`` if not detected. Used by ``attach`` to inject
    # an implicit dim'less default for unannotated ``integer``
    # declarations (counts / indices / loop iterators) so the U005
    # firehose is restricted to REAL variables where unit consistency
    # actually matters.
    intrinsic_type: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """Aggregated output of one source-file scan.

    Bundles every annotation, declaration, directive, and diagnostic
    seed surfaced by :func:`scan_text`. Stage 2 (:mod:`dimfort.core.attach`)
    joins ``annotations`` to ``declarations`` by physical line range;
    higher layers consume the directive streams and the malformed /
    conflict / wrong-kind reports.

    Attributes:
        annotations: Every ``@unit{...}`` occurrence in textual order.
        errors: Malformed-annotation seeds surfaced by U001 / U021 /
            U023 emitters downstream.
        pre_block_lines: Lines whose comment starts with ``!>`` or
            ``!!`` — used to find where a PRE block ends so it can be
            attached to the next declaration. POST (``!<``) is treated
            as one-line and is NOT included here.
        declarations: Every declaration statement found in the source,
            in textual order.
        routine_scopes: Byte-range cover of every ``subroutine`` /
            ``function`` in the file as ``(start_byte, end_byte,
            name_lc)``. Sorted by ``start_byte`` so the checker can
            bisect a node's byte offset to find its enclosing routine
            scope.
        assumes: Every ``@unit_assume{...}`` occurrence (escape-hatch
            directive).
        affine_conversions: Every ``@unit_affine_conversion{...}``
            occurrence.
        pattern_conflicts: Conflicts between configured patterns (spec
            §8.2). Populated when more than one pattern matches a
            comment with disagreeing capture text. Empty for the
            default single-pattern config. Consumed by the U021
            emitter.
        wrong_statement_kinds: Wrong-statement-kind events (spec §8.3
            → U023). A directive landed on a statement of a kind it
            does not target; the directive is dropped.
        assignment_line_ranges: Line ranges (1-based, inclusive) of
            every ``assignment_statement`` in the source. Used to
            distinguish "wrong statement kind here" (U023) from "no
            statement here at all" (U006 orphan) for ``@unit{}``
            annotations that don't find a declaration.
    """

    annotations: tuple[RawAnnotation, ...]
    errors: tuple[MalformedAnnotation, ...]
    # Lines whose comment starts with `!>` or `!!` — used to find where a
    # PRE block ends so it can be attached to the next declaration. POST
    # (`!<`) is treated as one-line and is NOT included here.
    pre_block_lines: frozenset[int] = frozenset()
    # Every declaration statement found in the source, in textual order.
    declarations: tuple[DeclarationSite, ...] = ()
    # Byte-range cover of every ``subroutine`` / ``function`` in the
    # file as ``(start_byte, end_byte, name_lc)``. Sorted by
    # ``start_byte`` so the checker can bisect a node's byte offset to
    # find its enclosing routine scope.
    routine_scopes: tuple[tuple[int, int, str], ...] = ()
    # Every ``@unit_assume{...}`` occurrence (escape-hatch directive).
    assumes: tuple[RawAssume, ...] = ()
    # Every ``@unit_affine_conversion{...}`` occurrence.
    affine_conversions: tuple[RawAffineConv, ...] = ()
    # Conflicts between configured patterns (spec §8.2). Populated when
    # more than one pattern matches a comment with disagreeing capture
    # text. Empty for the default single-pattern config. Consumed by
    # the U021 emitter.
    pattern_conflicts: tuple[PatternConflict, ...] = ()
    # Wrong-statement-kind events (spec §8.3 → U023). A directive
    # landed on a statement of a kind it does not target; the
    # directive is dropped.
    wrong_statement_kinds: tuple[WrongStatementKind, ...] = ()
    # Line ranges (1-based, inclusive) of every ``assignment_statement``
    # in the source. Used to distinguish "wrong statement kind here"
    # (U023) from "no statement here at all" (U006 orphan) for
    # ``@unit{}`` annotations that don't find a declaration.
    assignment_line_ranges: tuple[tuple[int, int], ...] = ()


def scan_text(
    source: str,
    *,
    unit_patterns: tuple[UnitPattern, ...] = DEFAULT_UNIT_PATTERNS,
    assume_patterns: tuple[StructuredPattern, ...] = DEFAULT_ASSUME_PATTERNS,
    affine_patterns: tuple[StructuredPattern, ...] = DEFAULT_AFFINE_PATTERNS,
    tree: Tree | None = None,
) -> ScanResult:
    """Scan a single Fortran source string for annotations and declarations.

    Args:
        source: Full text of one Fortran source file.
        unit_patterns: Configured ``@unit{}``-family patterns, in
            precedence order. Defaults to the canonical pattern.
        assume_patterns: Configured ``@unit_assume{}``-family patterns,
            in precedence order. Defaults to the canonical pattern.
        affine_patterns: Configured ``@unit_affine_conversion{}``-family
            patterns, in precedence order. Defaults to the canonical
            pattern.
        tree: Optional pre-parsed tree-sitter ``Tree`` over the same
            ``source`` bytes. When supplied, the scanner reuses it
            instead of re-running ``_ts.parse_text(source)``. Lets
            ``_load_one`` share one parse between scanning and the
            primary tree returned to the checker.

    Returns:
        A :class:`ScanResult` carrying every annotation, declaration,
        directive, and diagnostic seed surfaced by the scan.

    Note:
        Pattern lists default to the canonical ``@unit{...}`` etc.
        forms so callers with no project config get bit-for-bit
        pre-0.2.2 behavior — with one documented expansion (spec §10):
        a bare ``!`` comment containing a default-pattern match is now
        eligible at trailing-on-decl-line and standalone-immediately-
        above-decl positions.
    """
    lines = source.splitlines()
    declarations, routine_scopes, assignment_ranges = _scan_declarations(
        source, tree=tree,
    )

    # Per spec §3, plain ``!`` eligibility is decided by position
    # against the declaration set. Pre-compute two lookups:
    #   - decl_starts[N] → the Declaration whose line_start == N
    #   - decl_covered[N] → the Declaration whose line range covers N
    decl_starts: dict[int, DeclarationSite] = {
        d.line_start: d for d in declarations
    }
    decl_covered: dict[int, DeclarationSite] = {}
    for d in declarations:
        for ln in range(d.line_start, d.line_end + 1):
            decl_covered[ln] = d
    assignment_starts: frozenset[int] = frozenset(lo for lo, _ in assignment_ranges)
    assignment_covered: frozenset[int] = frozenset(
        ln for lo, hi in assignment_ranges for ln in range(lo, hi + 1)
    )

    annotations: list[RawAnnotation] = []
    errors: list[MalformedAnnotation] = []
    assumes: list[RawAssume] = []
    affine_conversions: list[RawAffineConv] = []
    pre_block_lines: set[int] = set()
    pattern_conflicts: list[PatternConflict] = []
    wrong_statement_kinds: list[WrongStatementKind] = []

    def _line_in_decl(ln: int) -> bool:
        """Return ``True`` if line ``ln`` is covered by a declaration."""
        return ln in decl_covered

    for line_no, line in enumerate(lines, start=1):
        col = _comment_start(line)
        if col is None:
            continue
        comment = line[col + 1:]
        # `col` is 0-based column of `!`. `comment[0]` sits at 1-based
        # column `col + 2`.
        marker_kind = _doxygen_kind(comment)

        kind: AnnotationKind
        if marker_kind is not None:
            # Doxygen-marked: kind from marker; body skips the marker
            # character. Pattern matching is config-driven (spec §4).
            kind = marker_kind
            body = comment[1:]
            body_col_offset = col + 3
        else:
            # Plain `!`: eligibility depends on position (spec §3).
            plain_kind = _classify_plain_comment(
                line, col, line_no, decl_starts, decl_covered,
                assignment_starts, assignment_covered,
            )
            if plain_kind is None:
                continue
            kind = plain_kind
            body = comment
            body_col_offset = col + 2

        u_anns, u_errs, u_confs, _u_winner_idx = _select_unit(
            body, line_no, body_col_offset, kind, unit_patterns,
        )
        # Spec §6 (post-Q1): every configured pattern attaches to all
        # names on a multi-variable declaration, same as canonical
        # ``@unit{...}``. Authors who want different units per name
        # write multiple matches on the line (``! [m] [s]``), which
        # fires today's "more than one … on one line" malformed
        # diagnostic and asks them to split the declaration.
        annotations.extend(u_anns)
        errors.extend(u_errs)
        pattern_conflicts.extend(u_confs)

        a_anns, a_errs, a_confs = _select_assume(
            body, line_no, body_col_offset, assume_patterns,
        )
        # Spec §8.3: @unit_assume on a declaration is the wrong
        # statement kind (declarations don't host an RHS to
        # suppress). Drop + U023. The target line is the comment's
        # own line for POST, the next line for PRE (mirrors how the
        # directive would attach if the kind were right).
        target_line = line_no if kind is AnnotationKind.POST else line_no + 1
        if a_anns and _line_in_decl(target_line):
            for a in a_anns:
                wrong_statement_kinds.append(WrongStatementKind(
                    line=a.line, column=a.column,
                    end_column=a.end_column,
                    directive_found=_ASSUME_DIR_NAME,
                    landed_on="declaration",
                    expected_directive=_UNIT_DIR_NAME,
                ))
            a_anns = []
        assumes.extend(a_anns)
        errors.extend(a_errs)
        pattern_conflicts.extend(a_confs)

        f_anns, f_errs, f_confs = _select_affine(
            body, line_no, body_col_offset, affine_patterns,
        )
        # Same §8.3 check for @unit_affine_conversion.
        if f_anns and _line_in_decl(target_line):
            for fa in f_anns:
                wrong_statement_kinds.append(WrongStatementKind(
                    line=fa.line, column=fa.column,
                    end_column=fa.end_column,
                    directive_found=_AFFINE_DIR_NAME,
                    landed_on="declaration",
                    expected_directive=_UNIT_DIR_NAME,
                ))
            f_anns = []
        affine_conversions.extend(f_anns)
        errors.extend(f_errs)
        pattern_conflicts.extend(f_confs)

        if kind is AnnotationKind.PRE:
            pre_block_lines.add(line_no)

    return ScanResult(
        annotations=tuple(annotations),
        errors=tuple(errors),
        pre_block_lines=frozenset(pre_block_lines),
        declarations=tuple(declarations),
        routine_scopes=tuple(routine_scopes),
        assumes=tuple(assumes),
        affine_conversions=tuple(affine_conversions),
        pattern_conflicts=tuple(pattern_conflicts),
        wrong_statement_kinds=tuple(wrong_statement_kinds),
        assignment_line_ranges=tuple(assignment_ranges),
    )


def _classify_plain_comment(
    line: str, col: int, line_no: int,
    decl_starts: dict[int, DeclarationSite],
    decl_covered: dict[int, DeclarationSite],
    assignment_starts: frozenset[int],
    assignment_covered: frozenset[int],
) -> AnnotationKind | None:
    """Classify a plain ``!`` comment per spec §3 / §5.

    Args:
        line: The full source line containing the comment.
        col: Zero-based column of the comment-opening ``!``.
        line_no: 1-based physical line number of the comment.
        decl_starts: Map of 1-based line → declaration starting on
            that line.
        decl_covered: Map of 1-based line → declaration covering that
            line (including continuation lines).
        assignment_starts: 1-based lines on which an
            ``assignment_statement`` begins.
        assignment_covered: 1-based lines covered by an
            ``assignment_statement`` (including continuation lines).

    Returns:
        :class:`AnnotationKind.POST` when the comment trails a
        statement-bearing line — a declaration (for ``@unit{}``) or an
        assignment (for ``@unit_assume`` / ``@unit_affine_conversion``).
        :class:`AnnotationKind.PRE` when the comment is standalone on
        its own line (only whitespace before ``!``) AND the very next
        line begins a declaration or assignment. ``None`` if the plain
        ``!`` comment is not in an eligible position.

    Note:
        Per spec §5, eligibility is the union of declaration- and
        assignment-targeted positions; the kind-correctness check
        (§8.3 → U023) happens after extraction.
    """
    is_standalone = not line[:col].strip()
    if not is_standalone:
        if line_no in decl_covered or line_no in assignment_covered:
            return AnnotationKind.POST
        return None
    next_ln = line_no + 1
    if next_ln in decl_starts or next_ln in assignment_starts:
        return AnnotationKind.PRE
    return None


# ---------------------------------------------------------------------------
# Tree-sitter declaration scanner
# ---------------------------------------------------------------------------
#
# We let tree-sitter's Fortran grammar identify declaration statements
# instead of hand-rolling a regex matcher. Each ``variable_declaration``
# node already spans the right physical line range (including
# ``&``-continuations) and exposes top-level ``identifier`` children
# for the names being declared. Names inside a ``type_qualifier``
# (e.g. the ``n`` in ``real, dimension(n) :: arr``) are nested deeper
# and are correctly ignored by taking only direct children.
#
# Type-block scoping (``type :: Foo ... end type``) comes from the
# ``derived_type_definition`` wrapper node; any ``variable_declaration``
# whose start byte sits within a derived_type_definition's span is a
# field of that type, not a free-standing variable.


# The leading identifier of a declared entity can arrive wrapped in one
# of three shapes, depending on whether the declaration carries an
# array spec, an initializer, or neither::
#
#   real :: a                → direct ``identifier`` child
#   real :: a(3)             → ``sized_declarator`` wrapping ``identifier``
#   real :: a = 1.0          → ``init_declarator`` wrapping ``identifier``
#   real :: a(3) = (/.../)   → ``init_declarator`` wrapping ``sized_declarator``
#
# We accept all three and recurse one level if the immediate child is
# a wrapper. Identifiers inside attribute expressions (the ``n`` in
# ``real, dimension(n) :: arr``) live under ``type_qualifier`` and are
# not visited.
_NAME_WRAPPERS = {"sized_declarator", "init_declarator"}


def _ts_decl_names(decl_node: Node) -> list[str]:
    """Return the variable names declared by a ``variable_declaration``.

    Args:
        decl_node: A tree-sitter ``variable_declaration`` node.

    Returns:
        Names of the declared entities, in source order. Identifiers
        inside attribute expressions (the ``n`` in ``real, dimension(n)
        :: arr``) are not included.
    """
    names: list[str] = []
    for c in decl_node.children:
        if c.type == "identifier":
            names.append((c.text or b"").decode("utf-8", "replace"))
            continue
        if c.type in _NAME_WRAPPERS:
            inner = _ts_declarator_name(c)
            if inner is not None:
                names.append(inner)
    return names


def _ts_declarator_name(node: Node) -> str | None:
    """Find the leading identifier inside a declarator wrapper.

    Args:
        node: A ``sized_declarator`` or ``init_declarator`` node.

    Returns:
        Name of the declared entity (the first ``identifier`` reached
        in a one-level recursive walk), or ``None`` if no identifier
        is present. Everything after the leading identifier is a
        dimension spec or initializer.
    """
    for c in node.children:
        if c.type == "identifier":
            return (c.text or b"").decode("utf-8", "replace")
        if c.type in _NAME_WRAPPERS:
            inner = _ts_declarator_name(c)
            if inner is not None:
                return inner
    return None


def _ts_type_name(type_def_node: Node) -> str | None:
    """Pull the type name out of a ``derived_type_definition`` node.

    Args:
        type_def_node: A tree-sitter ``derived_type_definition`` node.

    Returns:
        User-visible type name preserving case (per Fortran source
        convention), or ``None`` if the expected
        ``derived_type_statement`` / ``type_name`` children are absent.
    """
    stmt = next(
        (c for c in type_def_node.children if c.type == "derived_type_statement"),
        None,
    )
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "type_name"), None)
    return (name_node.text or b"").decode("utf-8", "replace") if name_node else None


def _ts_routine_name(node: Node) -> str | None:
    """Pull the name of a ``subroutine`` / ``function`` node.

    Args:
        node: A tree-sitter ``subroutine`` or ``function`` node.

    Returns:
        Routine name (preserving case), or ``None`` if the expected
        statement / ``name`` children are absent.
    """
    stmt_type = (
        "subroutine_statement"
        if node.type == "subroutine"
        else "function_statement"
    )
    stmt = next((c for c in node.children if c.type == stmt_type), None)
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    return (name_node.text or b"").decode("utf-8", "replace") if name_node else None


def _scan_declarations(
    source: str,
    *,
    tree: Tree | None = None,
) -> tuple[
    list[DeclarationSite],
    list[tuple[int, int, str]],
    list[tuple[int, int]],
]:
    """Walk a tree-sitter Fortran tree and emit one :class:`DeclarationSite` per decl.

    Args:
        source: Full text of one Fortran source file.
        tree: Optional pre-parsed tree-sitter ``Tree`` over the same
            source. When supplied, the internal
            ``_ts.parse_text(source)`` call is skipped.

    Returns:
        A three-tuple ``(declarations, routine_ranges,
        assignment_ranges)``:

        * ``declarations`` — one :class:`DeclarationSite` per
          ``variable_declaration`` node, in textual order.
        * ``routine_ranges`` — byte-range cover of every
          ``subroutine`` / ``function`` as ``(start_byte, end_byte,
          name_lc)``, sorted by ``start_byte`` so consumers can map a
          byte offset to its enclosing routine scope without
          re-walking.
        * ``assignment_ranges`` — 1-based inclusive line range of
          every ``assignment_statement`` in textual order.

    Note:
        The scanner is deliberately tolerant: a parse with ``ERROR``
        nodes (e.g. a syntactically broken statement somewhere in the
        file) still yields declarations from the well-formed regions.
        This matters for real-world Fortran files that occasionally
        contain idioms the grammar doesn't fully model.
    """
    if tree is None:
        tree = _ts.parse_text(source)
    root = tree.root_node

    # First pass: every derived-type definition's byte-range → its name,
    # and every subroutine/function's byte-range → its lower-cased name.
    # Storing byte-ranges (not line ranges) lets us nest correctly when
    # one type definition starts on the same line another ends on.
    type_ranges: list[tuple[int, int, str]] = []
    routine_ranges: list[tuple[int, int, str]] = []
    assignment_ranges: list[tuple[int, int]] = []
    for n in _ts.walk(root):
        if n.type == "derived_type_definition":
            name = _ts_type_name(n)
            if name is not None:
                type_ranges.append((n.start_byte, n.end_byte, name))
        elif n.type in ("subroutine", "function"):
            name = _ts_routine_name(n)
            if name is not None:
                routine_ranges.append((n.start_byte, n.end_byte, name.lower()))
        elif n.type == "assignment_statement":
            a_start = _ts.position_for(n).line
            a_end = _ts.end_position_for(n).line
            # Same trailing-newline correction as for declarations.
            if a_end > a_start and n.end_point[1] == 0:
                a_end -= 1
            assignment_ranges.append((a_start, a_end))

    routine_ranges.sort(key=lambda r: r[0])

    def enclosing_type_at(byte_offset: int) -> str | None:
        """Return the innermost derived-type name covering ``byte_offset``."""
        # Smallest containing range wins (handles nested definitions).
        best: tuple[int, int, str] | None = None
        for lo, hi, name in type_ranges:
            if lo <= byte_offset < hi and (
                best is None or (hi - lo) < (best[1] - best[0])
            ):
                best = (lo, hi, name)
        return best[2] if best else None

    def enclosing_routine_at(byte_offset: int) -> str | None:
        """Return the innermost routine name covering ``byte_offset``."""
        # Innermost (smallest) containing routine wins; handles a
        # CONTAINS-nested procedure inside its parent's range.
        best: tuple[int, int, str] | None = None
        for lo, hi, name in routine_ranges:
            if lo <= byte_offset < hi and (
                best is None or (hi - lo) < (best[1] - best[0])
            ):
                best = (lo, hi, name)
        return best[2] if best else None

    # Second pass: every variable_declaration becomes a DeclarationSite.
    out: list[DeclarationSite] = []
    for n in _ts.walk(root):
        if n.type != "variable_declaration":
            continue
        names = _ts_decl_names(n)
        if not names:
            continue  # e.g. a malformed half-declaration we shouldn't attach to
        start = _ts.position_for(n).line
        end = _ts.end_position_for(n).line
        # If the node ends exactly at the start of the next line (because
        # tree-sitter includes the trailing newline), prefer the previous
        # physical line as the real end.
        if end > start:
            end_col = n.end_point[1]
            if end_col == 0:
                end -= 1
        out.append(
            DeclarationSite(
                line_start=start,
                line_end=end,
                names=tuple(names),
                enclosing_type=enclosing_type_at(n.start_byte),
                scope=enclosing_routine_at(n.start_byte),
                intrinsic_type=_ts_decl_intrinsic_type(n),
            )
        )
    return out, routine_ranges, assignment_ranges


# Tree-sitter Fortran wraps the type qualifier of an intrinsic-typed
# declaration in an ``intrinsic_type`` node whose first content child
# carries the qualifier as its node type — ``real``, ``integer``,
# ``logical``, ``character``, ``complex``, or ``double`` (for ``double
# precision``). We return the qualifier lower-cased; derived-type
# declarations (``type(particle) :: …``) return ``None``.
_INTRINSIC_QUALIFIER_TYPES = frozenset({
    "real", "integer", "logical", "character", "complex", "double",
})


def _ts_decl_intrinsic_type(decl_node: Node) -> str | None:
    """Return the lower-cased intrinsic type qualifier of a declaration.

    Args:
        decl_node: A tree-sitter ``variable_declaration`` node.

    Returns:
        Lower-cased qualifier — one of ``"real"``, ``"integer"``,
        ``"logical"``, ``"character"``, ``"complex"``, or
        ``"double precision"`` — or ``None`` for derived-type
        declarations (``type(particle) :: …``) and when no
        ``intrinsic_type`` child is present.
    """
    for c in decl_node.children:
        if c.type != "intrinsic_type":
            continue
        for gc in c.children:
            if gc.type in _INTRINSIC_QUALIFIER_TYPES:
                return "double precision" if gc.type == "double" else gc.type
        return None
    return None


def scan_file(path: str | Path) -> ScanResult:
    """Scan a Fortran source file from disk.

    Args:
        path: Path to a Fortran source file.

    Returns:
        A :class:`ScanResult` with the same contents as
        :func:`scan_text` would produce for the file's contents.
    """
    from dimfort.core._source_io import read_text
    return scan_text(read_text(path))
