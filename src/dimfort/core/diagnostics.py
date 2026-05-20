"""Diagnostic representation shared by CLI and LSP."""
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
    trace: tuple["Provenance", ...] = field(default_factory=tuple)
