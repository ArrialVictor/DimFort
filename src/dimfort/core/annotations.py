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
class DeclarationSite:
    """A single Fortran declaration (possibly continued across lines)."""

    line_start: int            # 1-based: the line containing the type-spec
    line_end: int              # 1-based: the last physical line of the statement
    names: tuple[str, ...]     # variable names declared, in source order
    enclosing_type: str | None = None  # type-block name, if inside `type :: …`


@dataclass(frozen=True)
class ScanResult:
    annotations: tuple[RawAnnotation, ...]
    errors: tuple[MalformedAnnotation, ...]
    # Lines whose comment starts with `!>` or `!!` — used to find where a
    # PRE block ends so it can be attached to the next declaration. POST
    # (`!<`) is treated as one-line and is NOT included here.
    pre_block_lines: frozenset[int] = frozenset()
    # Every declaration statement found in the source, in textual order.
    declarations: tuple[DeclarationSite, ...] = ()


def scan_text(source: str) -> ScanResult:
    """Scan a single Fortran source string and return annotations + declarations."""
    lines = source.splitlines()
    annotations: list[RawAnnotation] = []
    errors: list[MalformedAnnotation] = []
    pre_block_lines: set[int] = set()
    for line_no, line in enumerate(lines, start=1):
        col = _comment_start(line)
        if col is None:
            continue
        comment = line[col + 1:]
        # `col` is 0-based column of `!`. The character right after the
        # `!` (i.e. `comment[0]`) sits at 1-based column `col + 2`.
        anns, errs, kind = _find_unit_invocations(
            comment, line_no=line_no, base_column=col + 2
        )
        annotations.extend(anns)
        errors.extend(errs)
        if kind is AnnotationKind.PRE:
            pre_block_lines.add(line_no)
    declarations = tuple(_scan_declarations(lines))
    return ScanResult(
        annotations=tuple(annotations),
        errors=tuple(errors),
        pre_block_lines=frozenset(pre_block_lines),
        declarations=declarations,
    )


# ---------------------------------------------------------------------------
# Source-side declaration scanner
# ---------------------------------------------------------------------------
#
# Why source-side? LFortran 0.63 has a position-tracking bug where each
# `&`-continued statement collapses to 2 reported lines internally,
# shifting subsequent declarations' `type_line` backward by 1 per prior
# continuation. That makes ASR positions unusable as the source of truth
# for annotation→declaration matching. Probing fixtures with continued
# decls confirms the drift: the second decl after a 3-line continued one
# is reported at physical_line - 1.
#
# So: we identify declarations from text directly. The grammar handled
# below is the common F90 subset; it is intentionally narrow and will
# need extension for LMDZ-grade real code.


_DECL_PREFIX_RE = re.compile(
    r"""^\s*
        ( real \b
        | integer \b
        | logical \b
        | complex \b
        | character \b
        | double \s+ precision \b
        | type      \s* \( [^)]+ \)
        | class     \s* \( [^)]+ \)
        )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# `type :: foo` / `type, attr :: foo` / `type foo` — the start of a
# derived-type definition block. Must be matched against the code part
# of the line (comments stripped) since we case-fold via re.IGNORECASE.
# A trailing `(` after `type` (as in `type(foo) :: bar`) is a *use*, not
# a definition; the regex disallows that by requiring whitespace,
# punctuation, or end-of-string immediately after the keyword.
_TYPE_BLOCK_START_RE = re.compile(
    r"""^\s*
        type
        \s*
        (?: , [^:]* :: \s* | :: \s* | \s+ )
        ([A-Za-z_]\w*)
    """,
    re.IGNORECASE | re.VERBOSE,
)

_TYPE_BLOCK_END_RE = re.compile(
    r"^\s* end \s* type \b", re.IGNORECASE | re.VERBOSE
)


_ENTITY_NAME_RE = re.compile(r"\s*([A-Za-z_]\w*)")


def _split_top_level_commas(s: str) -> list[str]:
    """Split ``s`` on commas that sit at paren-depth 0."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _extract_names_from_entity_list(entity_list: str) -> list[str]:
    """Return the variable names from a Fortran ``entity-list``.

    ``a, b, c`` → ``["a", "b", "c"]``.
    ``a = 1, b(3), c = (/0,0,0/)`` → ``["a", "b", "c"]``.
    """
    names: list[str] = []
    for entity in _split_top_level_commas(entity_list):
        m = _ENTITY_NAME_RE.match(entity)
        if m:
            names.append(m.group(1))
    return names


def _scan_declarations(lines: list[str]) -> list[DeclarationSite]:
    """Walk physical lines and emit one :class:`DeclarationSite` per stmt.

    Tracks ``type :: NAME`` blocks so each emitted site knows whether
    it sits inside a derived-type definition (and which one). Nested
    type definitions are unusual in F90; we use a stack defensively.
    """
    out: list[DeclarationSite] = []
    type_stack: list[str] = []
    i = 0
    while i < len(lines):
        col = _comment_start(lines[i])
        code = lines[i][:col] if col is not None else lines[i]

        # Track entering / leaving a `type :: NAME ... end type` block.
        if _TYPE_BLOCK_END_RE.match(code):
            if type_stack:
                type_stack.pop()
            i += 1
            continue
        m_open = _TYPE_BLOCK_START_RE.match(code)
        if m_open:
            type_stack.append(m_open.group(1))
            i += 1
            continue

        if not _DECL_PREFIX_RE.match(code):
            i += 1
            continue

        # Found a declaration start at line i+1. Walk forward through
        # `&`-continuation lines until the statement ends.
        line_start = i + 1
        chunks: list[str] = []
        j = i
        while j < len(lines):
            ccol = _comment_start(lines[j])
            ccode = lines[j][:ccol] if ccol is not None else lines[j]
            stripped = ccode.rstrip()
            if stripped.endswith("&"):
                chunks.append(stripped[:-1])
                j += 1
                if j >= len(lines):
                    break
            else:
                chunks.append(stripped)
                j += 1
                break
        line_end = j  # j is 1-past-last in 0-based → equals 1-based last line

        joined = " ".join(chunks)
        sep = joined.find("::")
        if sep != -1:
            names = _extract_names_from_entity_list(joined[sep + 2:])
            if names:
                enclosing = type_stack[-1] if type_stack else None
                out.append(
                    DeclarationSite(
                        line_start, line_end, tuple(names),
                        enclosing_type=enclosing,
                    )
                )
        i = j
    return out


def scan_file(path: str | Path) -> ScanResult:
    """Scan a Fortran source file from disk."""
    from dimfort.core._source_io import read_text
    return scan_text(read_text(path))
