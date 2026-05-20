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
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


# ---------------------------------------------------------------------------
# Per-rule severity overrides (Phase B follow-up)
# ---------------------------------------------------------------------------
#
# A project's ``.dimfort.toml`` may carry a ``[diagnostics]`` section
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
    """Install project-level severity overrides. Empty dict resets."""
    _severity_overrides.clear()
    _severity_overrides.update(overrides)


def _extract_marker(message: str) -> str | None:
    """Pull the ``(Dx.y)`` rule marker from a diagnostic message, if present."""
    m = _MARKER_RE.search(message)
    return m.group(0).strip("()") if m else None


def _resolve_override(diag: Diagnostic) -> str | None:
    """Return the configured override string for ``diag``, or ``None``."""
    marker = _extract_marker(diag.message)
    if marker is not None:
        override = _severity_overrides.get(marker)
        if override is not None:
            return override
    return _severity_overrides.get(diag.code)


def finalize_diagnostics(diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Apply ``[diagnostics]`` overrides to a list of diagnostics.

    Returns a new list; original diagnostics are unmodified (each
    Diagnostic is frozen). Entries overridden to ``"off"`` are
    dropped. ``"error"`` / ``"warning"`` rewrite the severity.
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
        target = Severity.ERROR if override == "error" else Severity.WARNING
        out.append(dataclasses.replace(d, severity=target))
    return out


@dataclass(frozen=True)
class Position:
    line: int  # 1-based
    column: int  # 1-based


@dataclass(frozen=True)
class Diagnostic:
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
