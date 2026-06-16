"""Attach :class:`RawAnnotation` records to source-side declarations.

Stage 2 of the annotation pipeline. Stage 1 (``annotations.py``)
produced both unattached ``@unit{...}`` occurrences and a list of
:class:`DeclarationSite` records covering every Fortran declaration
statement. This module joins them.

Why source-side declarations: LFortran 0.63 had a position-tracking
bug where each ``&``-continued statement collapsed to 2 reported
lines internally, shifting subsequent declarations' ``type_line``
backward. That made ASR's positions unsuitable as the source of
truth for annotation matching. We compute declaration extents from
the source text itself; ASR is reserved for semantic work (type
inference, intrinsic resolution) that needs proper compiler
understanding.

Attachment rules (0.2.7 per-line rule — see
``docs/design/shipped/per-variable-continuation-attach.md``):

- POST (``!<``) on physical line N attaches to the variables whose
  declaration tokens *end* on line N (read from
  :attr:`DeclarationSite.name_spans`). On a single-line declaration
  every name ends on the same line, so the rule degenerates to
  today's "attach to all of ``decl.names``" behaviour.
- PRE (``!>`` / ``!!``) on a single-line declaration: attaches to
  all of ``decl.names`` (unambiguous). On a *multi-line*
  declaration: refused with :class:`PreOnMultiLineDeclaration`
  (U024), with a hint to move the annotation to inline POST per-
  line form.

Diagnostic **U025** (info, permanent migration-detection) fires
when an annotation sits on a non-last continuation line and later
continuation lines have no annotation and their names are
unannotated. The pattern is the recurring footgun of the per-line
migration: an author wrote one annotation thinking it would attach
to the whole declaration, and half the names ended up U005.

Diagnostic **U010** retired in 0.2.7 — its only failure mode
(POST on an intermediate continuation line) is now a successful
attach under the per-line rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dimfort.core.annotations import (
    AnnotationKind,
    DeclarationSite,
    ScanResult,
)

# Forward declaration to keep ``RawAnnotationView``'s annotation
# satisfied when used inside ``attach``; the real definition lives
# at module bottom alongside the U025 post-pass helpers.


@dataclass(frozen=True)
class OrphanAnnotation:
    """An annotation that could not be matched to a declaration.

    Attributes:
        line: 1-based line of the annotation's source comment.
        column: 1-based column of the annotation's leading delimiter.
        unit_text: Raw text inside the annotation's delimiters.
        reason: Human-readable diagnostic explaining why the
            annotation did not attach.
        target_line: 1-based source line where the annotation tried
            to attach (the comment line itself for POST,
            ``_block_end + 1`` for PRE). The multifile diagnostic
            emitter consults this to reroute orphans to U023 when the
            target line hosts a non-declaration statement (spec §8.3).
        end_column: 1-based column one past the closing delimiter of
            the annotation's source token (e.g. one past ``}`` in
            ``@unit{m/s}``). The U023 reroute emitter widens its
            diagnostic range with this so the squiggle covers the
            whole token rather than a single character.
    """

    line: int
    column: int
    unit_text: str
    reason: str
    # 1-based source line where the annotation tried to attach (the
    # comment line itself for POST, ``_block_end + 1`` for PRE). The
    # multifile diagnostic emitter consults this to reroute orphans
    # to U023 when the target line hosts a non-declaration statement
    # (spec §8.3).
    target_line: int = 0
    # 1-based column one past the closing delimiter of the annotation's
    # source token (e.g. one past ``}`` in ``@unit{m/s}``). The U023
    # reroute emitter widens its diagnostic range with this so the
    # squiggle covers the whole token rather than a single character.
    end_column: int = 0


@dataclass(frozen=True)
class ConflictingAnnotation:
    """A variable that received two different unit annotations.

    Attributes:
        variable: Variable name; for derived-type fields rendered as
            ``"type%field"``.
        first_unit: Unit text from the first annotation seen.
        second_unit: Unit text from the conflicting annotation.
        second_line: 1-based source line of the second annotation.
    """

    variable: str
    first_unit: str
    second_unit: str
    second_line: int


@dataclass(frozen=True)
class PreOnMultiLineDeclaration:
    """U024: PRE unit annotation above a ``&``-continued declaration.

    The PRE block contains a real unit annotation but the
    declaration spans multiple physical lines. Under the per-line
    rule the annotation's intent is ambiguous (does it apply to
    every name, or only the first?), so the annotation is refused
    and the author is asked to switch to inline POST per-line form.

    Attributes:
        line: 1-based source line of the PRE annotation comment.
        column: 1-based column of the annotation's leading delimiter.
        unit_text: Raw text inside the annotation's delimiters.
        decl_line_start: 1-based first line of the target declaration.
        decl_line_end: 1-based last line of the target declaration.
    """

    line: int
    column: int
    unit_text: str
    decl_line_start: int
    decl_line_end: int

    @property
    def reason(self) -> str:
        """Human-readable U024 message with the inline-POST suggestion."""
        return (
            f"PRE annotation on a multi-line declaration "
            f"(lines {self.decl_line_start}-{self.decl_line_end}) is "
            "ambiguous under the per-line attach rule. Move to inline "
            "POST annotations on each continuation line, or collapse "
            "the declaration to a single line."
        )


@dataclass(frozen=True)
class MigrationDetectionAnnotation:
    """U025 (info): annotation on a non-last continuation line whose later names are unannotated.

    The pattern catches the recurring migration footgun: an author
    wrote one annotation thinking it would attach to the whole
    declaration, but under the per-line rule only the names ending
    on the annotation's line received it; names ending on later
    continuation lines remained unannotated. Severity is info, not
    warning — the code is correct as written (partial annotation
    may be intentional); U025 just surfaces the asymmetry.

    Attributes:
        line: 1-based source line of the original annotation.
        column: 1-based column of the annotation's leading delimiter.
        unit_text: Raw text of the annotation's unit content.
        decl_line_start: 1-based first line of the declaration.
        decl_line_end: 1-based last line of the declaration.
        unannotated_names: Names whose declaration tokens end on
            later continuation lines and remain unannotated.
    """

    line: int
    column: int
    unit_text: str
    decl_line_start: int
    decl_line_end: int
    unannotated_names: tuple[str, ...]

    @property
    def reason(self) -> str:
        """Human-readable U025 message naming the still-unannotated variables."""
        joined = ", ".join(repr(n) for n in self.unannotated_names)
        return (
            f"This annotation attaches to names on its line under the "
            f"per-line attach rule. Variables on later continuation "
            f"lines ({joined}) are unannotated; if you intended to "
            f"cover them, add per-line annotations on each line."
        )


@dataclass
class AttachmentResult:
    """Output of :func:`attach`: matched annotations plus rejected cases.

    Per-field rationale is kept inline beside each field — refer to
    those comments for the detailed contract of each table.

    Attributes:
        var_units: Flat first-seen-wins variable-to-unit-text view.
        var_units_by_scope: Authoritative scope-aware variable table
            keyed by ``(scope_lc, name)``.
        var_units_span: Source span of the annotation token that set
            each flat ``var_units`` entry.
        routine_scopes: Byte-range cover of each routine in the file,
            carried through from the scan.
        field_units: Derived-type field annotations, keyed by
            ``(type_name, field_name)``.
        var_unit_sources: Provenance tag for each
            ``var_units_by_scope`` entry — ``"explicit"`` or
            ``"intrinsic_default"``.
        orphans: Annotations that did not match any declaration.
        conflicts: Same-scope re-declarations with disagreeing units.
        pre_on_multiline: U024 — PRE unit annotation above a
            multi-line declaration (the design's conditional refuse).
        migration_detections: U025 — annotation on a non-last
            continuation line whose later names remain unannotated
            (info-level pattern detector).
    """

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
    pre_on_multiline: list[PreOnMultiLineDeclaration] = field(
        default_factory=list
    )
    migration_detections: list[MigrationDetectionAnnotation] = field(
        default_factory=list
    )


def _decl_containing_line(
    line: int, declarations: tuple[DeclarationSite, ...]
) -> DeclarationSite | None:
    """Return the declaration whose physical range contains ``line``.

    Args:
        line: 1-based source line number.
        declarations: Declaration sites to search, in scan order.

    Returns:
        The first matching :class:`DeclarationSite`, or ``None`` if no
        declaration covers ``line``.
    """
    for d in declarations:
        if d.line_start <= line <= d.line_end:
            return d
    return None


def _decl_starting_at_line(
    line: int, declarations: tuple[DeclarationSite, ...]
) -> DeclarationSite | None:
    """Return the declaration whose ``line_start`` equals ``line``.

    Args:
        line: 1-based source line number.
        declarations: Declaration sites to search, in scan order.

    Returns:
        The first matching :class:`DeclarationSite`, or ``None`` if no
        declaration starts on ``line``.
    """
    for d in declarations:
        if d.line_start == line:
            return d
    return None


def _block_end(line: int, pre_block_lines: frozenset[int]) -> int:
    """Return the largest ``L`` such that ``{line, line+1, …, L}`` lies in ``pre_block_lines``.

    Args:
        line: 1-based source line that starts the contiguous block.
        pre_block_lines: Set of lines hosting PRE-style annotation
            comments (``!>`` / ``!!``).

    Returns:
        The 1-based line number of the block's last contiguous member.
    """
    while line + 1 in pre_block_lines:
        line += 1
    return line


def _assign(
    result: AttachmentResult,
    name: str,
    unit_text: str,
    line: int,
    column: int,
    end_column: int,
    *,
    enclosing_type: str | None,
    scope: str | None,
) -> None:
    """Record one ``(name, unit_text)`` attachment, detecting per-scope conflicts.

    Mutates ``result`` in place. Derived-type fields are routed to
    ``field_units``; module / routine variables update both the
    scope-aware ``var_units_by_scope`` table and the flat
    first-seen-wins ``var_units`` view, plus the token span and the
    ``"explicit"`` provenance tag.

    Args:
        result: Attachment accumulator to mutate.
        name: Variable or field name (case as scanned).
        unit_text: Raw text of the unit annotation.
        line: 1-based source line of the annotation token.
        column: 1-based column of the annotation's leading delimiter.
        end_column: 1-based column one past the closing delimiter.
        enclosing_type: Derived-type name when ``name`` is a field,
            else ``None`` for ordinary variable declarations.
        scope: Lower-cased enclosing routine name, or ``None`` for
            module-level / file-level declarations.
    """
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
    # Token span: ``column`` is the 1-based column of the leading
    # delimiter (typically ``@``); ``end_column`` is the exclusive
    # 1-based end (one past the closing delimiter), threaded in from
    # the RawAnnotation so configurable comment delimiters (e.g.
    # ``[m/s]``) produce correctly-positioned U002 squiggles and LSP
    # hover ranges. Hardcoding ``len(unit_text) + 7`` here assumed the
    # canonical six-char ``@unit{`` + one-char ``}`` delimiters.
    result.var_units_span.setdefault(name, (line, column, end_column))


def attach(scan: ScanResult) -> AttachmentResult:
    """Match a stage-1 :class:`ScanResult`'s annotations to its declarations.

    Emits the full attachment surface:

    - The intrinsic-default policy below — INTEGER (and LOGICAL /
      CHARACTER) declarations that carry no explicit annotation default
      to dimensionless. The Fortran convention is that INTEGER variables
      are indices, counts, iteration bounds, enumerations, or flags —
      all dim'less. Treating them as dim'less by default keeps the U005
      "missing annotation" signal focused on REAL variables, where unit
      mismatches actually matter. A user who needs a unit-bearing
      integer (epoch seconds, say) writes the annotation explicitly.
    - ``PreOnMultiLineDeclaration`` (U024) — PRE unit annotation
      above a ``&``-continued declaration is refused under the
      per-line rule; the author is asked to switch to inline POST.
    - ``MigrationDetectionAnnotation`` (U025, info) — annotation on
      a non-last continuation line whose later names remain
      unannotated; surfaces the per-line migration footgun.
    - ``OrphanAnnotation`` (U023 rerouting) — annotations that don't
      attach to any declaration, carrying ``target_line`` /
      ``end_column`` so the LSP can re-point the squiggle.
    - Per-scope ``ConflictingAnnotation`` handling — multiple
      annotations on the same name within a routine scope.
    - ``var_unit_sources`` — provenance pointer from each attached name
      back to the originating :class:`RawAnnotation`.

    Args:
        scan: Output of the stage-1 source scan, holding raw
            annotations, declaration sites, the PRE-block line set,
            and the routine-scope cover.

    Returns:
        The fully populated :class:`AttachmentResult`.
    """
    result = AttachmentResult(routine_scopes=scan.routine_scopes)

    # Track ``(decl, ann)`` pairs whose annotation landed on a
    # non-last continuation line — input to the post-pass that
    # surfaces the U025 migration-detection pattern. Keyed by
    # ``id(decl)`` so duplicate decls (impossible in practice but
    # possible under hand-built test fixtures) don't merge.
    pending_u025: dict[int, tuple[DeclarationSite, list[RawAnnotationView]]] = {}

    for ann in scan.annotations:
        target_line: int
        if ann.kind is AnnotationKind.POST:
            decl = _decl_containing_line(ann.line, scan.declarations)
            orphan_reason = (
                "no declaration spans this line" if decl is None else ""
            )
            target_line = ann.line
        else:
            target = _block_end(ann.line, scan.pre_block_lines) + 1
            decl = _decl_starting_at_line(target, scan.declarations)
            orphan_reason = (
                f"no declaration immediately follows the !> block "
                f"(expected on line {target})"
                if decl is None
                else ""
            )
            target_line = target

        if decl is None:
            result.orphans.append(
                OrphanAnnotation(
                    line=ann.line,
                    column=ann.column,
                    unit_text=ann.unit_text,
                    reason=orphan_reason,
                    target_line=target_line,
                    end_column=ann.end_column,
                )
            )
            continue

        if ann.kind is AnnotationKind.POST:
            # Per-line attach: names whose tokens end on ann.line.
            attaching = [
                s for s in decl.name_spans if s.end_line == ann.line
            ]
            if not attaching:
                # The annotation is inside the decl's range but no
                # name's tokens end on this line — silent no-op. The
                # U025 post-pass picks up the broader pattern if it
                # fits; today this was the U010-reject case.
                continue
            for span in attaching:
                _assign(
                    result, span.name, ann.unit_text,
                    ann.line, ann.column, ann.end_column,
                    enclosing_type=decl.enclosing_type,
                    scope=decl.scope,
                )
            # Stash for the U025 post-pass when the annotation is on
            # a non-last continuation line of a multi-line decl.
            if (
                decl.line_start < decl.line_end
                and ann.line < decl.line_end
            ):
                entry = pending_u025.setdefault(
                    id(decl), (decl, []),
                )
                entry[1].append(
                    RawAnnotationView(
                        line=ann.line,
                        column=ann.column,
                        unit_text=ann.unit_text,
                    )
                )
            continue

        # PRE branch.
        if decl.line_start < decl.line_end:
            # Multi-line decl + PRE unit annotation: U024 refuse.
            result.pre_on_multiline.append(
                PreOnMultiLineDeclaration(
                    line=ann.line,
                    column=ann.column,
                    unit_text=ann.unit_text,
                    decl_line_start=decl.line_start,
                    decl_line_end=decl.line_end,
                )
            )
            continue
        # Single-line decl: PRE attaches to all names (unambiguous).
        for name in decl.names:
            _assign(
                result, name, ann.unit_text,
                ann.line, ann.column, ann.end_column,
                enclosing_type=decl.enclosing_type,
                scope=decl.scope,
            )

    # U025 post-pass: for each multi-line decl with a non-last-line
    # attach, surface the names that end on later continuation lines
    # and remain unannotated.
    for decl, anns in pending_u025.values():
        # Use the latest non-last attach line as the diagnostic anchor.
        anns.sort(key=lambda a: a.line)
        anchor = anns[-1]
        later_unannotated = tuple(
            s.name for s in decl.name_spans
            if s.end_line > anchor.line
            and (decl.scope, s.name) not in result.var_units_by_scope
        )
        if not later_unannotated:
            continue
        result.migration_detections.append(
            MigrationDetectionAnnotation(
                line=anchor.line,
                column=anchor.column,
                unit_text=anchor.unit_text,
                decl_line_start=decl.line_start,
                decl_line_end=decl.line_end,
                unannotated_names=later_unannotated,
            )
        )

    _apply_intrinsic_defaults(result, scan.declarations)
    return result


@dataclass(frozen=True)
class RawAnnotationView:
    """Compact ``RawAnnotation`` view kept inside the attach pass.

    The full :class:`RawAnnotation` carries extra fields the U025
    post-pass doesn't need (``end_column``, ``kind``); the view
    keeps only the trio used to anchor the diagnostic.
    """

    line: int
    column: int
    unit_text: str


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
    """Fill in ``@unit{1}`` for unannotated INTEGER / LOGICAL / CHARACTER declarations.

    Annotated declarations win — if the user wrote ``integer :: t  !<
    @unit{s}`` the existing ``s`` annotation is kept. Derived-type
    fields skip the default (each field still needs an explicit
    annotation; the default would interfere with U002 on the
    type-block).

    Args:
        result: Attachment accumulator to mutate in place.
        declarations: Declaration sites from the stage-1 scan.
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
