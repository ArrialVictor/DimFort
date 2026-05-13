"""Diagnostic representation shared by CLI and LSP."""
from dataclasses import dataclass
from enum import StrEnum


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
