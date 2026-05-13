"""Doxygen ``@unit{...}`` annotation scanner.

Stage 1: pure text pass over a Fortran source file. Recognises Doxygen
comment markers (``!>``, ``!<``, ``!!``) and extracts ``@unit{...}``
annotations carried inside them. Does **not** attach annotations to
declarations yet — that's stage 2 (joining with LFortran's AST/ASR).

Why a separate stage 1:

- The scanner is the only place that needs to understand Fortran string
  literals (so ``!`` inside a string isn't a comment).
- Stage 2 can then operate on a clean stream of ``(kind, line, unit)``
  triples without re-tokenising.
- Easier to unit-test against tiny fixtures.

Restrictions in v1 (documented; relax later if needed):

- ``@unit{...}`` must fit on one source line. Multi-line forms like
  ``@unit{`` … ``}`` across ``!>`` continuation lines are not parsed.
- At most one ``@unit{...}`` per comment line. A second one is reported
  as :class:`MalformedAnnotation`.

See ``Homogeneity/PROJECT_LOG.md`` (§4ter) for the attachment rules
that the upstream stage will apply.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class AnnotationKind(StrEnum):
    """Where the annotation attaches relative to its declaration."""

    PRE = "pre"   # !> or !! preceding the declaration
    POST = "post"  # !< trailing the declaration


@dataclass(frozen=True)
class RawAnnotation:
    """One ``@unit{...}`` occurrence as found by the scanner."""

    kind: AnnotationKind
    line: int        # 1-based physical line of the comment
    column: int      # 1-based column where `@unit{` begins
    unit_text: str   # raw inner text between `{` and `}`, untrimmed of surrounding spaces stripped


@dataclass(frozen=True)
class MalformedAnnotation:
    """A ``@unit`` invocation that the scanner could not parse."""

    line: int
    column: int
    reason: str


# ---------------------------------------------------------------------------
# String-aware comment detection
# ---------------------------------------------------------------------------


def _comment_start(line: str) -> int | None:
    """Column of the first ``!`` that opens a comment, or ``None``.

    Tracks single- and double-quoted strings so a ``!`` inside a literal
    isn't mistaken for a comment marker. Doubled quotes (``''`` / ``""``)
    inside a string are the Fortran escape and don't close it.

    Ported from V4's ``annotations.py``.
    """
    in_quote: str | None = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_quote is None:
            if c == "!":
                return i
            if c == "'" or c == '"':
                in_quote = c
        else:
            if c == in_quote:
                if i + 1 < len(line) and line[i + 1] == in_quote:
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

# Match `@unit` followed by `{`. The opening brace is required.
_UNIT_RE = re.compile(r"@unit\s*\{")


def _doxygen_kind(comment_text: str) -> AnnotationKind | None:
    """Classify a comment (everything after the opening ``!``).

    Returns ``None`` for plain ``!`` comments. The Doxygen marker
    character is consumed; the caller scans the rest of the line for
    ``@unit{...}``.
    """
    if not comment_text:
        return None
    return _DOX_MARKER.get(comment_text[0])


def _find_unit_invocations(
    comment_text: str, line_no: int, base_column: int
) -> tuple[list[RawAnnotation], list[MalformedAnnotation], AnnotationKind | None]:
    """Pull every ``@unit{...}`` out of one comment body.

    ``base_column`` is the 1-based column where ``comment_text`` starts
    in the source line. ``comment_text`` does NOT include the leading
    ``!``; it begins with the Doxygen marker character (``>``, ``<``,
    or ``!``).
    """
    kind = _doxygen_kind(comment_text)
    if kind is None:
        return [], [], None
    body = comment_text[1:]  # strip the Doxygen marker char
    body_col_offset = base_column + 1  # 1-based column where body[0] sits
    found: list[RawAnnotation] = []
    errors: list[MalformedAnnotation] = []
    for m in _UNIT_RE.finditer(body):
        start = m.start()
        close = body.find("}", m.end())
        col = body_col_offset + start
        if close == -1:
            errors.append(MalformedAnnotation(line_no, col, "unclosed '{' in @unit"))
            continue
        inner = body[m.end():close].strip()
        if not inner:
            errors.append(MalformedAnnotation(line_no, col, "empty @unit{}"))
            continue
        found.append(RawAnnotation(kind=kind, line=line_no, column=col, unit_text=inner))
    if len(found) > 1:
        # Keep the first; flag every extra. Stage 2 may treat duplicates
        # as ambiguous against the same declaration.
        errors.extend(
            MalformedAnnotation(a.line, a.column, "more than one @unit on one line")
            for a in found[1:]
        )
        found = found[:1]
    return found, errors, kind


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScanResult:
    annotations: tuple[RawAnnotation, ...]
    errors: tuple[MalformedAnnotation, ...]


def scan_text(source: str) -> ScanResult:
    """Scan a single Fortran source string and return all annotations."""
    annotations: list[RawAnnotation] = []
    errors: list[MalformedAnnotation] = []
    for line_no, line in enumerate(source.splitlines(), start=1):
        col = _comment_start(line)
        if col is None:
            continue
        comment = line[col + 1:]  # drop the `!`
        # `col` is 0-based column of `!`. The character right after the
        # `!` (which is `comment[0]`) sits at 1-based column `col + 2`.
        anns, errs, _ = _find_unit_invocations(
            comment, line_no=line_no, base_column=col + 2
        )
        annotations.extend(anns)
        errors.extend(errs)
    return ScanResult(tuple(annotations), tuple(errors))


def scan_file(path: str | Path) -> ScanResult:
    """Scan a Fortran source file from disk."""
    return scan_text(Path(path).read_text())
