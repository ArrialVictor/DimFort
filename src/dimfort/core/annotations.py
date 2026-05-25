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

Restrictions in v1 (documented; relax later if needed):

- ``@unit{...}`` must fit on one source line. Multi-line forms like
  ``@unit{`` … ``}`` across ``!>`` continuation lines are not parsed.
- At most one ``@unit{...}`` per comment line. A second one is reported
  as :class:`MalformedAnnotation`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dimfort.core import ts_parser as _ts


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
class RawAssume:
    """One ``@unit_assume{ <unit> : <reason> }`` occurrence.

    The escape hatch: on an assignment line it tells the checker to stop
    deriving the RHS unit (suppressing D1.4 and any interior fire) and
    instead treat the result as ``unit_text``, still consistency-checked
    against the LHS. ``reason`` is mandatory (a category + free text, e.g.
    ``empirical-fit: Brandes 2007``) so every assumption is auditable.
    """

    line: int        # 1-based physical line of the comment
    column: int      # 1-based column where `@unit_assume{` begins
    unit_text: str   # the asserted unit, e.g. "kg/m^3"
    reason: str      # mandatory justification (category + text)


@dataclass(frozen=True)
class RawAffineConv:
    """One ``@unit_affine_conversion{ <src> -> <tgt> }`` occurrence.

    A *verified* affine-conversion directive (Phase 2c, scale.md §11): on
    an assignment line it asserts the statement converts a ``src``-typed
    quantity into the ``tgt`` frame (e.g. ``degC -> K``). Unlike
    ``@unit_assume`` it carries no reason and needs no registry — the
    checker *verifies* the arithmetic against the known offsets and errors
    (S003) if it doesn't fit. ``->`` is primary; ``,`` is accepted as a
    synonym separator.
    """

    line: int        # 1-based physical line of the comment
    column: int      # 1-based column where `@unit_affine_conversion{` begins
    src: str         # source unit name, e.g. "degC"
    tgt: str         # target unit name, e.g. "K"


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

    Fast path: the vast majority of Fortran lines contain no string
    literal, so a single ``str.find("!")`` is enough. Only when a quote
    is actually present do we fall back to the quote-aware character
    scan. Profiling on a large workspace (1.75M calls) showed this function and its
    per-iteration ``len(line)`` accounted for ~9 seconds; the fast
    path drops it under 1.
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

# Match `@unit` followed by `{`. The opening brace is required.
# NB: `@unit_assume{` does NOT match this (a `_` sits between `@unit`
# and `{`), so the two scanners never collide.
_UNIT_RE = re.compile(r"@unit\s*\{")

# Match `@unit_assume` followed by `{` — the escape-hatch directive.
_ASSUME_RE = re.compile(r"@unit_assume\s*\{")

# Match `@unit_affine_conversion` followed by `{` — the verified
# affine-conversion directive (Phase 2c). Like the two above, the `_`
# after `@unit` keeps it from colliding with ``_UNIT_RE``; the longer
# ``_affine_conversion`` keeps it distinct from ``_ASSUME_RE``.
_AFFINE_RE = re.compile(r"@unit_affine_conversion\s*\{")


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


def _find_assume_invocations(
    comment_text: str, line_no: int, base_column: int
) -> tuple[list[RawAssume], list[MalformedAnnotation]]:
    """Pull every ``@unit_assume{ <unit> : <reason> }`` out of one comment.

    Like :func:`_find_unit_invocations`, requires a Doxygen marker (so the
    directive anchors to a statement via a trailing ``!<`` or a preceding
    ``!>``/``!!``). The inner text is split on the first ``:`` into the
    asserted unit and a mandatory reason; either part missing is malformed.
    """
    kind = _doxygen_kind(comment_text)
    if kind is None:
        return [], []
    body = comment_text[1:]  # strip the Doxygen marker char
    body_col_offset = base_column + 1
    found: list[RawAssume] = []
    errors: list[MalformedAnnotation] = []
    for m in _ASSUME_RE.finditer(body):
        start = m.start()
        close = body.find("}", m.end())
        col = body_col_offset + start
        if close == -1:
            errors.append(
                MalformedAnnotation(line_no, col, "unclosed '{' in @unit_assume")
            )
            continue
        inner = body[m.end():close]
        if ":" not in inner:
            errors.append(MalformedAnnotation(
                line_no, col,
                "@unit_assume requires '{ <unit> : <reason> }' "
                "(missing ':' separating unit from reason)",
            ))
            continue
        unit_part, reason_part = inner.split(":", 1)
        unit_text = unit_part.strip()
        reason = reason_part.strip()
        if not unit_text:
            errors.append(
                MalformedAnnotation(line_no, col, "empty unit in @unit_assume")
            )
            continue
        if not reason:
            errors.append(MalformedAnnotation(
                line_no, col, "empty reason in @unit_assume (a justification is required)",
            ))
            continue
        found.append(
            RawAssume(line=line_no, column=col, unit_text=unit_text, reason=reason)
        )
    if len(found) > 1:
        errors.extend(
            MalformedAnnotation(a.line, a.column, "more than one @unit_assume on one line")
            for a in found[1:]
        )
        found = found[:1]
    return found, errors


def _find_affine_invocations(
    comment_text: str, line_no: int, base_column: int
) -> tuple[list[RawAffineConv], list[MalformedAnnotation]]:
    """Pull every ``@unit_affine_conversion{ <src> -> <tgt> }`` out of one
    comment.

    Like the other directive scanners, requires a Doxygen marker. The inner
    text is split on the first ``->`` (primary) or ``,`` (synonym) into the
    source and target unit names; either part missing is malformed. The unit
    names are *not* resolved here — the checker does that at verify time so a
    bad name surfaces as S003 with the statement, not a scan error.
    """
    kind = _doxygen_kind(comment_text)
    if kind is None:
        return [], []
    body = comment_text[1:]  # strip the Doxygen marker char
    body_col_offset = base_column + 1
    found: list[RawAffineConv] = []
    errors: list[MalformedAnnotation] = []
    for m in _AFFINE_RE.finditer(body):
        start = m.start()
        close = body.find("}", m.end())
        col = body_col_offset + start
        if close == -1:
            errors.append(MalformedAnnotation(
                line_no, col, "unclosed '{' in @unit_affine_conversion"
            ))
            continue
        inner = body[m.end():close]
        if "->" in inner:
            src_part, tgt_part = inner.split("->", 1)
        elif "," in inner:
            src_part, tgt_part = inner.split(",", 1)
        else:
            errors.append(MalformedAnnotation(
                line_no, col,
                "@unit_affine_conversion requires '{ <src> -> <tgt> }' "
                "(missing '->' or ',' separating source from target)",
            ))
            continue
        src = src_part.strip()
        tgt = tgt_part.strip()
        if not src or not tgt:
            errors.append(MalformedAnnotation(
                line_no, col,
                "empty source or target unit in @unit_affine_conversion",
            ))
            continue
        found.append(RawAffineConv(line=line_no, column=col, src=src, tgt=tgt))
    if len(found) > 1:
        errors.extend(
            MalformedAnnotation(
                a.line, a.column,
                "more than one @unit_affine_conversion on one line",
            )
            for a in found[1:]
        )
        found = found[:1]
    return found, errors


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
    # Every ``@unit_affine_conversion{...}`` occurrence (Phase 2c).
    affine_conversions: tuple[RawAffineConv, ...] = ()


def scan_text(source: str) -> ScanResult:
    """Scan a single Fortran source string and return annotations + declarations."""
    lines = source.splitlines()
    annotations: list[RawAnnotation] = []
    errors: list[MalformedAnnotation] = []
    assumes: list[RawAssume] = []
    affine_conversions: list[RawAffineConv] = []
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
        asms, asm_errs = _find_assume_invocations(
            comment, line_no=line_no, base_column=col + 2
        )
        assumes.extend(asms)
        errors.extend(asm_errs)
        afcs, afc_errs = _find_affine_invocations(
            comment, line_no=line_no, base_column=col + 2
        )
        affine_conversions.extend(afcs)
        errors.extend(afc_errs)
        if kind is AnnotationKind.PRE:
            pre_block_lines.add(line_no)
    declarations, routine_scopes = _scan_declarations(source)
    return ScanResult(
        annotations=tuple(annotations),
        errors=tuple(errors),
        pre_block_lines=frozenset(pre_block_lines),
        declarations=tuple(declarations),
        routine_scopes=tuple(routine_scopes),
        assumes=tuple(assumes),
        affine_conversions=tuple(affine_conversions),
    )


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


def _ts_decl_names(decl_node) -> list[str]:
    """Return the variable names declared by a ``variable_declaration``."""
    names: list[str] = []
    for c in decl_node.children:
        if c.type == "identifier":
            names.append(c.text.decode("utf-8", "replace"))
            continue
        if c.type in _NAME_WRAPPERS:
            inner = _ts_declarator_name(c)
            if inner is not None:
                names.append(inner)
    return names


def _ts_declarator_name(node) -> str | None:
    """Find the leading identifier inside a declarator wrapper.

    Walks descendants until the first ``identifier`` — that's the name
    of the declared entity (everything after it is dimension spec or
    initializer).
    """
    for c in node.children:
        if c.type == "identifier":
            return c.text.decode("utf-8", "replace")
        if c.type in _NAME_WRAPPERS:
            inner = _ts_declarator_name(c)
            if inner is not None:
                return inner
    return None


def _ts_type_name(type_def_node) -> str | None:
    """Pull the type name out of a ``derived_type_definition`` node.

    The first ``derived_type_statement`` child carries a ``type_name``
    child whose text is the user-visible name (preserving case, per
    Fortran source convention).
    """
    stmt = next(
        (c for c in type_def_node.children if c.type == "derived_type_statement"),
        None,
    )
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "type_name"), None)
    return name_node.text.decode("utf-8", "replace") if name_node else None


def _ts_routine_name(node) -> str | None:
    """Pull the name of a ``subroutine`` / ``function`` node."""
    stmt_type = (
        "subroutine_statement"
        if node.type == "subroutine"
        else "function_statement"
    )
    stmt = next((c for c in node.children if c.type == stmt_type), None)
    if stmt is None:
        return None
    name_node = next((c for c in stmt.children if c.type == "name"), None)
    return name_node.text.decode("utf-8", "replace") if name_node else None


def _scan_declarations(
    source: str,
) -> tuple[list[DeclarationSite], list[tuple[int, int, str]]]:
    """Walk a tree-sitter Fortran tree and emit one :class:`DeclarationSite` per decl.

    Also returns the byte-range cover of every ``subroutine`` /
    ``function`` (sorted by ``start_byte``) so consumers can map a
    byte offset to its enclosing routine scope without re-walking.

    The scanner is deliberately tolerant: a parse with ``ERROR`` nodes
    (e.g. a syntactically broken statement somewhere in the file) still
    yields declarations from the well-formed regions. This matters for
    real-world Fortran files that occasionally contain F77 idioms the
    grammar doesn't fully model.
    """
    tree = _ts.parse_text(source)
    root = tree.root_node

    # First pass: every derived-type definition's byte-range → its name,
    # and every subroutine/function's byte-range → its lower-cased name.
    # Storing byte-ranges (not line ranges) lets us nest correctly when
    # one type definition starts on the same line another ends on.
    type_ranges: list[tuple[int, int, str]] = []
    routine_ranges: list[tuple[int, int, str]] = []
    for n in _ts.walk(root):
        if n.type == "derived_type_definition":
            name = _ts_type_name(n)
            if name is not None:
                type_ranges.append((n.start_byte, n.end_byte, name))
        elif n.type in ("subroutine", "function"):
            name = _ts_routine_name(n)
            if name is not None:
                routine_ranges.append((n.start_byte, n.end_byte, name.lower()))

    routine_ranges.sort(key=lambda r: r[0])

    def enclosing_type_at(byte_offset: int) -> str | None:
        # Smallest containing range wins (handles nested definitions).
        best: tuple[int, int, str] | None = None
        for lo, hi, name in type_ranges:
            if lo <= byte_offset < hi and (
                best is None or (hi - lo) < (best[1] - best[0])
            ):
                best = (lo, hi, name)
        return best[2] if best else None

    def enclosing_routine_at(byte_offset: int) -> str | None:
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
    return out, routine_ranges


# Tree-sitter Fortran wraps the type qualifier of an intrinsic-typed
# declaration in an ``intrinsic_type`` node whose first content child
# carries the qualifier as its node type — ``real``, ``integer``,
# ``logical``, ``character``, ``complex``, or ``double`` (for ``double
# precision``). We return the qualifier lower-cased; derived-type
# declarations (``type(particle) :: …``) return ``None``.
_INTRINSIC_QUALIFIER_TYPES = frozenset({
    "real", "integer", "logical", "character", "complex", "double",
})


def _ts_decl_intrinsic_type(decl_node) -> str | None:
    for c in decl_node.children:
        if c.type != "intrinsic_type":
            continue
        for gc in c.children:
            if gc.type in _INTRINSIC_QUALIFIER_TYPES:
                return "double precision" if gc.type == "double" else gc.type
        return None
    return None


def scan_file(path: str | Path) -> ScanResult:
    """Scan a Fortran source file from disk."""
    from dimfort.core._source_io import read_text
    return scan_text(read_text(path))
