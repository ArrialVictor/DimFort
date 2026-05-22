"""Attach :class:`RawAnnotation` records to source-side declarations.

Stage 2 of the annotation pipeline. Stage 1 (``annotations.py``)
produced both unattached ``@unit{...}`` occurrences and a list of
:class:`DeclarationSite` records covering every Fortran declaration
statement. This module joins them.

Why source-side declarations: LFortran 0.63 has a position-tracking
bug where each ``&``-continued statement collapses to 2 reported lines
internally, shifting subsequent declarations' ``type_line`` backward.
That makes ASR's positions unsuitable as the source of truth for
annotation matching. We compute declaration extents from the source
text itself; ASR is reserved for semantic work (type inference,
intrinsic resolution) that needs proper compiler understanding.

Attachment rules:

- POST (``!<``) on any physical line in
  ``[decl.line_start, decl.line_end]`` attaches to all of ``decl.names``.
- PRE (``!>`` / ``!!``): walk forward through the contiguous
  ``pre_block_lines`` set; the annotation attaches to the declaration
  whose ``line_start`` equals ``block_end + 1``.

Diagnostic **U010** fires when a POST annotation sits on an
*intermediate* line of a ``&``-continued declaration (strictly between
``line_start`` and ``line_end``). The annotation is rejected — the
target variables are *not* assigned — because the position suggests a
per-variable scope that we do not support. Users must move the
annotation to either the first or the last line of the continuation,
or split the declaration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dimfort.core.annotations import (
    AnnotationKind,
    DeclarationSite,
    ScanResult,
)


@dataclass(frozen=True)
class OrphanAnnotation:
    """An annotation that could not be matched to a declaration."""

    line: int
    column: int
    unit_text: str
    reason: str


@dataclass(frozen=True)
class ConflictingAnnotation:
    """A variable that received two different unit annotations."""

    variable: str
    first_unit: str
    second_unit: str
    second_line: int


@dataclass(frozen=True)
class IntermediateContinuationAnnotation:
    """U010: ``!<`` on a continuation line that is neither first nor last.

    The annotation is *not* applied — the user must move it to the first
    or the last line of the continuation.
    """

    line: int
    column: int
    unit_text: str
    declaration_line_start: int
    declaration_line_end: int

    @property
    def reason(self) -> str:
        return (
            f"'!<' on an intermediate continuation line (declaration spans "
            f"lines {self.declaration_line_start}-{self.declaration_line_end}); "
            f"move the annotation to line {self.declaration_line_start} or "
            f"line {self.declaration_line_end}"
        )


@dataclass
class AttachmentResult:
    # Flat first-seen-wins view, kept for callers that don't care about
    # scope (LSP hover fallback, U005 annotated-set, U002 parse loop).
    var_units: dict[str, str] = field(default_factory=dict)
    # Scope-aware view. Key: ``(scope_lc, name)`` where ``scope_lc`` is
    # the lower-cased enclosing subroutine/function name, or ``None``
    # for module-level / file-level declarations. This is the
    # authoritative table for checker work; ``var_units`` is derived
    # from it for back-compat.
    var_units_by_scope: dict[tuple[str | None, str], str] = field(
        default_factory=dict
    )
    # Source span of the ``@unit{...}`` token that set each flat
    # ``var_units`` entry, first-seen-wins to match ``var_units``.
    # Value: ``(line, start_col, end_col)``, all 1-based, in the
    # checker's Position convention. Lets a U002 (unparseable
    # annotation) squiggle land on the annotation token itself rather
    # than a zero-width point at the declaration line.
    var_units_span: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    # Byte-range cover of every subroutine/function in the file, sorted
    # by start_byte. Carried through from the scan so the checker can
    # resolve a node's enclosing scope without re-walking the tree.
    routine_scopes: tuple[tuple[int, int, str], ...] = ()
    # Derived-type field annotations live under their own table so they
    # don't collide with same-named local variables. Keyed by
    # ``(type_name, field_name)``.
    field_units: dict[tuple[str, str], str] = field(default_factory=dict)
    # Provenance tag for each ``var_units_by_scope`` entry: how was its
    # unit assigned? ``"explicit"`` from a user-written ``@unit{...}``;
    # ``"intrinsic_default"`` from the INTEGER / LOGICAL / CHARACTER
    # default-dim'less rule. Used by the LSP hover to surface "this is
    # the implicit default" to the user.
    var_unit_sources: dict[tuple[str | None, str], str] = field(
        default_factory=dict
    )
    orphans: list[OrphanAnnotation] = field(default_factory=list)
    conflicts: list[ConflictingAnnotation] = field(default_factory=list)
    intermediate_continuations: list[IntermediateContinuationAnnotation] = field(
        default_factory=list
    )


def _decl_containing_line(
    line: int, declarations: tuple[DeclarationSite, ...]
) -> DeclarationSite | None:
    """Return the declaration whose physical range contains ``line``, or None."""
    for d in declarations:
        if d.line_start <= line <= d.line_end:
            return d
    return None


def _decl_starting_at_line(
    line: int, declarations: tuple[DeclarationSite, ...]
) -> DeclarationSite | None:
    for d in declarations:
        if d.line_start == line:
            return d
    return None


def _block_end(line: int, pre_block_lines: frozenset[int]) -> int:
    """Largest L such that {line, line+1, …, L} ⊆ ``pre_block_lines``."""
    while line + 1 in pre_block_lines:
        line += 1
    return line


def _assign(
    result: AttachmentResult,
    name: str,
    unit_text: str,
    line: int,
    column: int,
    *,
    enclosing_type: str | None,
    scope: str | None,
) -> None:
    if enclosing_type is not None:
        key = (enclosing_type, name)
        existing_f = result.field_units.get(key)
        if existing_f is not None and existing_f != unit_text:
            result.conflicts.append(
                ConflictingAnnotation(
                    variable=f"{enclosing_type}%{name}",
                    first_unit=existing_f,
                    second_unit=unit_text,
                    second_line=line,
                )
            )
            return
        result.field_units[key] = unit_text
        return
    # Per-scope: U-conflict fires only when the SAME scope re-declares
    # ``name`` with a different unit. Same name in two different
    # subroutines is normal and silent.
    scope_key = (scope, name)
    existing_scoped = result.var_units_by_scope.get(scope_key)
    if existing_scoped is not None and existing_scoped != unit_text:
        result.conflicts.append(
            ConflictingAnnotation(
                variable=name,
                first_unit=existing_scoped,
                second_unit=unit_text,
                second_line=line,
            )
        )
        return
    result.var_units_by_scope[scope_key] = unit_text
    result.var_unit_sources[scope_key] = "explicit"
    # Flat view: first-seen-wins across the whole file. Callers that
    # consult ``var_units`` accept that ambiguity; the authoritative
    # answer lives in ``var_units_by_scope``.
    result.var_units.setdefault(name, unit_text)
    # Token span: ``@unit{`` (6) + inner text + ``}`` (1). ``column``
    # is the 1-based column of the leading ``@``; ``end_col`` is the
    # exclusive 1-based end (one past ``}``) in the checker's
    # diagnostic convention.
    result.var_units_span.setdefault(
        name, (line, column, column + len(unit_text) + 7)
    )


def attach(scan: ScanResult) -> AttachmentResult:
    """Match a stage-1 :class:`ScanResult`'s annotations to its declarations.

    INTEGER (and LOGICAL / CHARACTER) declarations that carry no
    explicit ``@unit{}`` annotation default to dimensionless. The
    Fortran convention is that INTEGER variables are indices, counts,
    iteration bounds, enumerations, or flags — all dim'less. Treating
    them as dim'less by default keeps the U005 "missing annotation"
    signal focused on REAL variables, where unit mismatches actually
    matter. A user who needs a unit-bearing integer (epoch seconds,
    say) writes the annotation explicitly.
    """
    result = AttachmentResult(routine_scopes=scan.routine_scopes)
    for ann in scan.annotations:
        if ann.kind is AnnotationKind.POST:
            decl = _decl_containing_line(ann.line, scan.declarations)
            orphan_reason = (
                "no declaration spans this line" if decl is None else ""
            )
            # U010: POST on a strictly-interior continuation line is rejected.
            if (
                decl is not None
                and decl.line_start < ann.line < decl.line_end
            ):
                result.intermediate_continuations.append(
                    IntermediateContinuationAnnotation(
                        line=ann.line,
                        column=ann.column,
                        unit_text=ann.unit_text,
                        declaration_line_start=decl.line_start,
                        declaration_line_end=decl.line_end,
                    )
                )
                continue
        else:
            target = _block_end(ann.line, scan.pre_block_lines) + 1
            decl = _decl_starting_at_line(target, scan.declarations)
            orphan_reason = (
                f"no declaration immediately follows the !> block "
                f"(expected on line {target})"
                if decl is None
                else ""
            )

        if decl is None:
            result.orphans.append(
                OrphanAnnotation(
                    line=ann.line,
                    column=ann.column,
                    unit_text=ann.unit_text,
                    reason=orphan_reason,
                )
            )
            continue
        for name in decl.names:
            _assign(
                result,
                name,
                ann.unit_text,
                ann.line,
                ann.column,
                enclosing_type=decl.enclosing_type,
                scope=decl.scope,
            )
    _apply_intrinsic_defaults(result, scan.declarations)
    return result


# Intrinsic Fortran types whose declared variables are dim'less by
# language convention when the user provides no ``@unit{}`` annotation:
#
#   integer       — indices, counts, iteration variables, flags
#   logical       — boolean; not a measured quantity
#   character     — text; not a measured quantity
#
# REAL, COMPLEX, and DOUBLE PRECISION carry physical measurements
# by convention and are NOT defaulted — those declarations still
# fire U005 when unannotated.
_DIMLESS_DEFAULT_TYPES = frozenset({"integer", "logical", "character"})


def _apply_intrinsic_defaults(
    result: AttachmentResult, declarations: tuple[DeclarationSite, ...]
) -> None:
    """Fill in ``@unit{1}`` for unannotated INTEGER / LOGICAL / CHARACTER
    declarations.

    Annotated declarations win — if the user wrote ``integer :: t  !<
    @unit{s}`` we keep ``s``. Derived-type fields skip the default
    (each field still needs an explicit annotation; the default would
    interfere with U002 on the type-block).
    """
    for decl in declarations:
        if decl.intrinsic_type not in _DIMLESS_DEFAULT_TYPES:
            continue
        if decl.enclosing_type is not None:
            # Field of a derived type — leave to the user.
            continue
        for name in decl.names:
            scope_key = (decl.scope, name)
            if scope_key in result.var_units_by_scope:
                continue  # explicit annotation already attached
            result.var_units_by_scope[scope_key] = "1"
            result.var_unit_sources[scope_key] = "intrinsic_default"
            if name not in result.var_units:
                result.var_units[name] = "1"
