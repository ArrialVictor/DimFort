"""Diagnostic representation shared by CLI and LSP."""
from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dimfort.core.trace import Provenance


class Severity(StrEnum):
    """Diagnostic severity levels exchanged between CLI and LSP.

    Attributes:
        ERROR: Hard failure; the CLI exits non-zero.
        WARNING: Soft failure; rendered as a squiggle, exit code
            unaffected.
        INFO: Informational note (e.g. ``@unit_assume`` U020).
        HINT: Editor-level hint with no CLI surface.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


# ---------------------------------------------------------------------------
# Per-rule severity overrides (Phase B follow-up)
# ---------------------------------------------------------------------------
#
# A project's ``dimfort.toml`` may carry a ``[diagnostics]`` section
# mapping a diagnostic *code* (``H001``, ``H010``) or a *rule marker*
# (``D1.4``, ``D1.7``) to one of ``"error"``, ``"warning"``, or
# ``"off"``. Rule markers take precedence so a user can promote D1.7
# to error without affecting other H010 diagnostics.
#
# The CLI loads the config once and calls ``set_severity_overrides``
# before any check runs; the LSP does the same at initialize.
# ``finalize_diagnostics`` applies the overrides in one pass at the
# end of a check.

_severity_overrides: dict[str, str] = {}
_MARKER_RE = re.compile(r"\(D\d+\.\d+\)")


def set_severity_overrides(overrides: dict[str, str]) -> None:
    """Install project-level severity overrides.

    Replaces the module-level table wholesale; passing an empty dict
    resets to defaults.

    Args:
        overrides: Mapping from diagnostic code (``"H001"``) or rule
            marker (``"D1.7"``) to one of ``"error"``, ``"warning"``,
            ``"info"``, or ``"off"``.
    """
    _severity_overrides.clear()
    _severity_overrides.update(overrides)


_SEVERITY_BY_OVERRIDE = {
    "error": Severity.ERROR,
    "warning": Severity.WARNING,
    "info": Severity.INFO,
}


def effective_severity(code: str, default: Severity) -> Severity | None:
    """Resolve a diagnostic code's effective severity under active overrides.

    For non-diagnostic surfaces (panel / hover markers) that want to
    mirror the squiggle's severity without synthesising a Diagnostic.
    Code-level only — rule-marker (``Dx.y``) overrides aren't consulted
    here.

    Args:
        code: Diagnostic code to resolve (e.g. ``"H010"``).
        default: Severity to return when no override applies.

    Returns:
        The overridden severity, ``default`` when nothing overrides
        ``code``, or ``None`` when ``code`` is overridden to ``"off"``.

    Note:
        Recognised override values: ``"error"``, ``"warning"``,
        ``"info"``, ``"off"``. ``"info"`` was added in 0.2.3 (the docs
        at docs/reference/dimfort-toml.md already shipped the example,
        but the parser silently dropped it pre-0.2.3).
    """
    override = _severity_overrides.get(code)
    if override is None:
        return default
    if override == "off":
        return None
    return _SEVERITY_BY_OVERRIDE.get(override, Severity.WARNING)


def _extract_marker(message: str) -> str | None:
    """Pull the ``(Dx.y)`` rule marker from a diagnostic message.

    Args:
        message: Full diagnostic message text.

    Returns:
        Marker text without the surrounding parentheses (``"D1.4"``),
        or ``None`` when no marker is present.
    """
    m = _MARKER_RE.search(message)
    return m.group(0).strip("()") if m else None


def _resolve_override(diag: Diagnostic) -> str | None:
    """Return the configured override string for ``diag``, or ``None``.

    Rule-marker overrides take precedence over code-level overrides
    (a user can promote ``D1.7`` without affecting other ``H010``
    diagnostics).

    Args:
        diag: Diagnostic to consult overrides for.

    Returns:
        The matching override string, or ``None`` when nothing matches.
    """
    marker = _extract_marker(diag.message)
    if marker is not None:
        override = _severity_overrides.get(marker)
        if override is not None:
            return override
    return _severity_overrides.get(diag.code)


def finalize_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Apply ``[diagnostics]`` overrides to a list of diagnostics.

    Args:
        diagnostics: Diagnostics emitted by the checker, in any order.

    Returns:
        A new list. Original diagnostics are unmodified (each
        :class:`Diagnostic` is frozen). Entries overridden to
        ``"off"`` are dropped; ``"error"`` / ``"warning"`` /
        ``"info"`` rewrite the severity.
    """
    if not _severity_overrides:
        return diagnostics
    out: list[Diagnostic] = []
    for d in diagnostics:
        override = _resolve_override(d)
        if override is None:
            out.append(d)
            continue
        if override == "off":
            continue
        target = _SEVERITY_BY_OVERRIDE.get(override, Severity.WARNING)
        out.append(dataclasses.replace(d, severity=target))
    return out


@dataclass(frozen=True)
class Position:
    """1-based source coordinate.

    Attributes:
        line: 1-based line number.
        column: 1-based column number (character offset within the
            line).
    """

    line: int  # 1-based
    column: int  # 1-based


@dataclass(frozen=True)
class Diagnostic:
    """One emitted diagnostic in the wire format shared by CLI and LSP.

    Attributes:
        file: Source file path the diagnostic points at.
        start: Inclusive start position.
        end: Exclusive end position.
        severity: Effective severity after overrides are applied.
        code: Stable diagnostic code (``"H001"``, ``"U005"``, ...).
        message: Human-readable message (may contain the ``(Dx.y)``
            rule marker).
        trace: Optional unit-algebra provenance chain; see inline
            comment for emission rules.
        suggested_rewrite: Optional pipeline-rewrite candidate for
            U002; see inline comment for emission rules.
        polymorphism_conflict: Structured H020 conflict rows; see
            inline comment for the tuple shape.
    """

    file: str
    start: Position
    end: Position
    severity: Severity
    code: str
    message: str
    # Optional unit-algebra provenance chain (Phase D). Empty unless
    # tracing was enabled — the checker fills it in by snapshotting
    # the active trace at the diagnostic's emission site.
    trace: tuple[Provenance, ...] = field(default_factory=tuple)
    # Spec §12: when a U002 fires on an unparseable unit capture and
    # the rewrite detector finds a pipeline-transformed candidate
    # that parses cleanly, this carries the candidate. The CLI shows
    # it as "did you mean ...?"; the LSP turns it into a code
    # action. ``None`` for every other diagnostic and for U002s
    # without a suggestion.
    suggested_rewrite: str | None = None
    # Structured conflict data for H020 (polymorphic call-site
    # unification failure). Each row carries
    # ``(slot_index, slot_name, binding_unit_text, partner_slot_indices)`` —
    # the slot's index (0-based), the formal arg name if known, the unit
    # the slot would force the tyvar to (pretty-formatted), and the
    # *other* slot indices it collides with. The LSP panel-rendering
    # path reads this to draw the spec's ``'a = unit — collides with
    # arg N`` rendering on each conflicting arg row instead of the
    # generic ``(expected 'a)`` trailer that the concrete-signature
    # H004 path uses. ``None`` for every diagnostic other than H020;
    # never reaches the LSP wire (``_to_lsp_diagnostic`` doesn't
    # forward it).
    polymorphism_conflict: tuple[
        tuple[int, str | None, str, tuple[int, ...]], ...
    ] | None = None


@dataclass(frozen=True)
class AutocastEvent:
    """A literal RHS implicitly took on its assignment's LHS unit.

    Emitted by :func:`ts_checker.check` (rule R4.4) for every assignment
    where the leniency rule fires — currently any RHS that's a "pure
    numeric constant" (literal, parenthesised literal, unary-minus
    literal, or arithmetic of literals). The fact is recorded for any
    consumer that wants to:

    - audit / list every implicit-literal initialization in a workspace,
    - render the autocast as a status marker (LSP panel / hover do
      this),
    - opt-in to a strict-mode that promotes R4.4 to an Information- or
      Warning-severity diagnostic via the existing ``[diagnostics]``
      config infrastructure.

    The event is **not** a diagnostic — no severity, no message format
    — so it doesn't leak into the standard diagnostic stream. Adopters
    that want diagnostic-shaped output can synthesise one on the fly.

    Attributes:
        file: Source file path containing the assignment.
        start: 1-based start position of the literal value expression
            (not the whole assignment).
        end: 1-based end position of the literal value expression.
        literal_text: Source slice of the RHS (e.g. ``"2.0"`` or
            ``"0.5 * Cp"``).
        inferred_unit: ``format_unit(lhs_unit)`` rendering of the unit
            the literal implicitly took on.
        context: Site classifier — currently ``"assignment_rhs"``;
            extensible for future contexts.
    """

    file: str
    # 1-based source coordinates pointing at the literal value
    # expression (not the whole assignment).
    start: Position
    end: Position
    literal_text: str   # source slice of the RHS, e.g. "2.0" or "0.5 * Cp"
    inferred_unit: str  # ``format_unit(lhs_unit)`` rendering
    context: str        # "assignment_rhs" — extensible for future contexts
