"""Attach :class:`RawAnnotation` records to ASR ``Variable`` nodes.

Stage 2 of the annotation pipeline. Stage 1 (``annotations.py``)
produced unattached ``@unit{...}`` occurrences; this module joins them
to the variables they document.

Key idea (inherited from V4): join by the ``Variable.type.loc.first_line``
rather than the variable's own loc. For ``real :: a, b, c !< @unit{m}``
all three Variable nodes share the same type-node line, so a single
trailing annotation naturally maps to all three (declaration-list
apply-to-all rule).

Attachment rules:

- POST (``!<``): the annotation's line equals the declaration's type
  first_line.
- PRE (``!>`` / ``!!``): walk forward through the contiguous
  ``pre_block_lines`` set computed by stage 1; the annotation attaches
  to the declaration whose type first_line equals ``block_end + 1``.

Diagnostics produced:

- :class:`OrphanAnnotation` — annotation line carries no matching
  declaration.
- :class:`ConflictingAnnotation` — same variable receives two
  ``@unit{...}`` values from different annotations.

Not yet implemented:

- POST on the last line of a ``&``-continued declaration (form B/C in
  the PROJECT_LOG continuation rule). Needs careful handling of
  LFortran's normalised line numbers.
- ``--strict-declist`` (U011) — flag multi-variable lists with a single
  annotation.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dimfort.core.annotations import AnnotationKind, ScanResult
from dimfort.core.lfortran import walk


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


def _variables_by_type_line(asr: dict) -> dict[int, list[str]]:
    """Map ``type.first_line`` to the list of variable names declared there.

    A declaration list (``real :: a, b, c``) puts multiple Variable nodes
    on the same type-line, so each line maps to a list.
    """
    out: dict[int, list[str]] = {}
    for node in walk(asr):
        if not isinstance(node, dict) or node.get("node") != "Variable":
            continue
        fields = node.get("fields") or {}
        name = fields.get("name")
        type_node = fields.get("type")
        if not name or not isinstance(type_node, dict):
            continue
        loc = type_node.get("loc")
        if not isinstance(loc, dict):
            continue
        line = loc.get("first_line")
        if not isinstance(line, int):
            continue
        out.setdefault(line, []).append(name)
    return out


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


def attach(scan: ScanResult, asr: dict) -> AttachmentResult:
    """Join a stage-1 :class:`ScanResult` to an ASR tree."""
    result = AttachmentResult()
    vars_by_line = _variables_by_type_line(asr)

    for ann in scan.annotations:
        if ann.kind is AnnotationKind.POST:
            target_line = ann.line
            orphan_reason = "no declaration on this line"
        else:
            block_end = _block_end(ann.line, scan.pre_block_lines)
            target_line = block_end + 1
            orphan_reason = (
                f"no declaration immediately following the "
                f"!> block (expected on line {target_line})"
            )

        names = vars_by_line.get(target_line)
        if not names:
            result.orphans.append(
                OrphanAnnotation(
                    line=ann.line,
                    column=ann.column,
                    unit_text=ann.unit_text,
                    reason=orphan_reason,
                )
            )
            continue
        for name in names:
            _assign(result, name, ann.unit_text, ann.line)

    return result
