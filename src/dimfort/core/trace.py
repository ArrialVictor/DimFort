"""Provenance tracing for unit-algebra rule applications (Phase D, spec §12).

Records every rule fire (R2.x / R3.x / R4.x / R5.x / R6.x / R7.x) as a
``Provenance`` step so diagnostics can be explained by the chain of
rules that led to them. Off by default to avoid memory overhead; enabled
via context manager ``with_trace()`` or CLI ``dimfort check --trace``.

Design notes:

- ``Unit`` / ``LogWrap`` / ``ExpWrap`` are frozen dataclasses; structural
  equality must hold across the codebase. Provenance is therefore *not*
  carried on units themselves but in a separate per-trace step list.
- Activation uses a ``contextvars.ContextVar`` so concurrent checks
  (e.g. an LSP server handling parallel requests) don't bleed traces
  into one another.
- When no trace is active, ``trace_step()`` is a single dict lookup —
  cheap enough to leave unconditional in the hot rule-dispatch path.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dimfort.core.units import UnitExpr


@dataclass(frozen=True)
class Provenance:
    """One rule fire — what went in, what came out, which rule fired.

    ``rule_id`` is a string like ``"R3.1"`` / ``"R5.2"`` / ``"R2.3"``;
    ``before`` is a tuple of operands (one for unary rules, two for
    binary); ``after`` is the result unit (``None`` when the rule
    produced an error and the caller will emit a diagnostic);
    ``source_text`` is an optional source snippet for the rule's
    triggering expression — populated by the checker when an AST node
    is available.
    """
    rule_id: str
    before: tuple[UnitExpr, ...]
    after: UnitExpr | None
    source_text: str | None = None


@dataclass
class Trace:
    """An append-only ordered list of rule fires."""
    steps: list[Provenance] = field(default_factory=list)

    def append(self, step: Provenance) -> None:
        self.steps.append(step)

    def snapshot(self) -> tuple[Provenance, ...]:
        return tuple(self.steps)


_active_trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
    "dimfort_active_trace", default=None
)


def current_trace() -> Trace | None:
    """Return the trace collector active in this context, or ``None``."""
    return _active_trace.get()


def trace_step(
    rule_id: str,
    before: tuple[UnitExpr, ...],
    after: UnitExpr | None,
    source_text: str | None = None,
) -> None:
    """Record a rule fire if a trace is active; otherwise no-op."""
    trace = _active_trace.get()
    if trace is None:
        return
    trace.append(Provenance(rule_id, before, after, source_text))


@contextmanager
def with_trace() -> Iterator[Trace]:
    """Activate a fresh trace for the duration of the ``with`` block."""
    trace = Trace()
    token = _active_trace.set(trace)
    try:
        yield trace
    finally:
        _active_trace.reset(token)


def format_provenance(step: Provenance) -> str:
    """Render one rule fire as ``A <op> B  ⇒  Result   [Rx.y]`` (spec §12 T3).

    Wrapper types print using ``format_unit`` so the LOG(...)/EXP(...)
    layer is visible; "ERROR" stands in for ``after=None``.
    """
    from dimfort.core.units import format_unit
    before = ", ".join(format_unit(u) for u in step.before)
    after = format_unit(step.after) if step.after is not None else "ERROR"
    return f"{before}  ⇒  {after}   [{step.rule_id}]"


def format_trace(steps: tuple[Provenance, ...] | list[Provenance]) -> str:
    """Render a full trace chain as multi-line text suitable for CLI / hover.

    Consecutive duplicate steps are collapsed (the checker's parallel
    _resolve / _walk_expressions walks both invoke combine() on the
    same subexpressions, so each rule otherwise appears twice). Each
    step occupies one line, prefixed with ``→`` so the chain reads
    top-to-bottom. Empty traces return an empty string.
    """
    if not steps:
        return ""
    seen: set[tuple] = set()
    lines = ["trace:"]
    for step in steps:
        key = (step.rule_id, step.before, step.after)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  → {format_provenance(step)}")
    return "\n".join(lines)


__all__ = [
    "Provenance",
    "Trace",
    "current_trace",
    "format_provenance",
    "format_trace",
    "trace_step",
    "with_trace",
]
