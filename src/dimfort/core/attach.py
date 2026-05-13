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

POST on an intermediate continuation line (form B/C policy: only first
or last line of a continuation may carry ``!<``) is not yet diagnosed
as a distinct U010 — currently the annotation still applies. That
strict check arrives with ``--strict-declist`` and friends.
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


@dataclass
class AttachmentResult:
    var_units: dict[str, str] = field(default_factory=dict)
    orphans: list[OrphanAnnotation] = field(default_factory=list)
    conflicts: list[ConflictingAnnotation] = field(default_factory=list)


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


def _assign(result: AttachmentResult, name: str, unit_text: str, line: int) -> None:
    existing = result.var_units.get(name)
    if existing is not None and existing != unit_text:
        result.conflicts.append(
            ConflictingAnnotation(
                variable=name,
                first_unit=existing,
                second_unit=unit_text,
                second_line=line,
            )
        )
        return
    result.var_units[name] = unit_text


def attach(scan: ScanResult) -> AttachmentResult:
    """Match a stage-1 :class:`ScanResult`'s annotations to its declarations."""
    result = AttachmentResult()
    for ann in scan.annotations:
        if ann.kind is AnnotationKind.POST:
            decl = _decl_containing_line(ann.line, scan.declarations)
            orphan_reason = (
                "no declaration spans this line"
                if decl is None
                else ""
            )
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
            _assign(result, name, ann.unit_text, ann.line)
    return result
