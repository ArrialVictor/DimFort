"""Attach :class:`RawAnnotation` records to ASR ``Variable`` nodes.

Stage 2 of the annotation pipeline. Stage 1 (``annotations.py``)
produced unattached ``@unit{...}`` occurrences. This module joins them
to the variables they document.

Key idea (inherited from V4): join by the ``Variable.type.loc.first_line``
rather than the variable's own loc. For ``real :: a, b, c !< @unit{m}``
all three Variable nodes share the same type-node line, so a single
trailing annotation naturally maps to all three (apply-to-all
declaration-list rule).

Currently implemented:

- POST (``!<``) annotations: match by ``type.first_line == annotation.line``.
- Orphan POST annotations (no matching declaration): collected as
  :class:`OrphanAnnotation`.

Not yet implemented (later stages):

- PRE (``!>``/``!!``) annotation blocks attached to the next declaration.
- POST on the last line of a ``&``-continued declaration (form B in the
  PROJECT_LOG continuation rule).
- ``--strict-declist`` (U011) — flag multi-variable lists with a single
  annotation.

These cases pass through silently for now: PRE annotations are kept
in :attr:`AttachmentResult.unattached_pre` so a later stage can pick
them up without re-scanning.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from dimfort.core.annotations import AnnotationKind, RawAnnotation, ScanResult
from dimfort.core.lfortran import walk


@dataclass(frozen=True)
class OrphanAnnotation:
    """A POST annotation whose line carries no variable declaration."""

    line: int
    column: int
    unit_text: str
    reason: str = "no variable declaration on this line"


@dataclass
class AttachmentResult:
    var_units: dict[str, str] = field(default_factory=dict)
    orphans: list[OrphanAnnotation] = field(default_factory=list)
    unattached_pre: list[RawAnnotation] = field(default_factory=list)


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


def attach(scan: ScanResult, asr: dict) -> AttachmentResult:
    """Join a stage-1 :class:`ScanResult` to an ASR tree.

    The current implementation handles only POST (``!<``) annotations on
    the same line as the declaration's type node. PRE annotations are
    returned untouched in :attr:`AttachmentResult.unattached_pre`.
    """
    result = AttachmentResult()
    vars_by_line = _variables_by_type_line(asr)

    for ann in scan.annotations:
        if ann.kind is AnnotationKind.PRE:
            result.unattached_pre.append(ann)
            continue
        # POST: same-line match.
        names = vars_by_line.get(ann.line)
        if not names:
            result.orphans.append(
                OrphanAnnotation(
                    line=ann.line, column=ann.column, unit_text=ann.unit_text
                )
            )
            continue
        for name in names:
            result.var_units[name] = ann.unit_text

    return result
