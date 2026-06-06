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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from dimfort.core.units import UnitExpr


@dataclass(frozen=True)
class Provenance:
    """One rule fire — what went in, what came out, which rule fired.

    Attributes:
        rule_id: Rule identifier like ``"R3.1"`` / ``"R5.2"`` / ``"R2.3"``.
        before: Tuple of input operands. One element for unary rules
            (R3.x wrapper untag, R2.x wrapper tag), two for binary rules
            (R5.x add/sub, R6.x mul/div, R7.x power).
        after: Result unit, or ``None`` when the rule produced an error
            and the caller will emit a diagnostic in its stead.
        source_text: Optional source snippet for the triggering
            expression. Populated by the checker when an AST node is
            available; left ``None`` for rules fired from synthetic
            contexts (e.g. cross-routine resolution).
    """
    rule_id: str
    before: tuple[UnitExpr, ...]
    after: UnitExpr | None
    source_text: str | None = None


@dataclass
class Trace:
    """An append-only ordered list of rule fires.

    Attributes:
        steps: The recorded ``Provenance`` entries, in fire order.
    """
    steps: list[Provenance] = field(default_factory=list)

    def append(self, step: Provenance) -> None:
        """Append a single rule fire to the trace.

        Args:
            step: Recorded provenance to append.
        """
        self.steps.append(step)

    def snapshot(self) -> tuple[Provenance, ...]:
        """Return an immutable copy of the steps recorded so far.

        Returns:
            Tuple of steps in fire order. Subsequent appends to the
            trace do not affect this snapshot.
        """
        return tuple(self.steps)


_active_trace: contextvars.ContextVar[Trace | None] = contextvars.ContextVar(
    "dimfort_active_trace", default=None
)


def current_trace() -> Trace | None:
    """Return the trace collector active in this context.

    Returns:
        The active :class:`Trace`, or ``None`` if tracing is not
        enabled in the current context.
    """
    return _active_trace.get()


def trace_step(
    rule_id: str,
    before: tuple[UnitExpr, ...],
    after: UnitExpr | None,
    source_text: str | None = None,
) -> None:
    """Record a rule fire on the active trace if one exists; otherwise no-op.

    Designed to be cheap enough to leave unconditional in the hot
    rule-dispatch path — when no trace is active, this collapses to a
    single ``ContextVar.get()`` lookup.

    Args:
        rule_id: Rule identifier like ``"R3.1"`` / ``"R5.2"``.
        before: Input operands to the rule, in source order.
        after: Result unit, or ``None`` if the rule produced an error.
        source_text: Optional source snippet for the triggering
            expression.
    """
    trace = _active_trace.get()
    if trace is None:
        return
    trace.append(Provenance(rule_id, before, after, source_text))


@contextmanager
def with_trace() -> Iterator[Trace]:
    """Activate a fresh trace for the duration of the ``with`` block.

    Uses ``contextvars`` for activation, so concurrent checks running
    in distinct contexts (e.g. an LSP server handling parallel
    requests) do not bleed traces into one another.

    Yields:
        The freshly-allocated :class:`Trace`, available for inspection
        after the block exits.
    """
    trace = Trace()
    token = _active_trace.set(trace)
    try:
        yield trace
    finally:
        _active_trace.reset(token)


def format_provenance(step: Provenance) -> str:
    """Render one rule fire as ``A <op> B  ⇒  Result   [Rx.y]`` (spec §12 T3).

    Wrapper types print using ``format_unit`` so the ``LOG(...)`` /
    ``EXP(...)`` layer is visible. The literal string ``"ERROR"``
    stands in for ``after=None``.

    Args:
        step: Recorded rule fire.

    Returns:
        Single-line human-readable rendering.
    """
    from dimfort.core.units import format_unit
    before = ", ".join(format_unit(u) for u in step.before)
    after = format_unit(step.after) if step.after is not None else "ERROR"
    return f"{before}  ⇒  {after}   [{step.rule_id}]"


def format_trace(steps: tuple[Provenance, ...] | list[Provenance]) -> str:
    """Render a full trace chain as multi-line text suitable for CLI / hover.

    Consecutive duplicate steps are collapsed — the checker's parallel
    ``_resolve`` / ``_walk_expressions`` walks both invoke ``combine()``
    on the same subexpressions, so each rule otherwise appears twice.
    Each surviving step occupies one line, prefixed with ``→`` so the
    chain reads top-to-bottom. Empty traces return an empty string.

    Args:
        steps: Recorded provenance steps, in fire order.

    Returns:
        Multi-line rendering led by a ``"trace:"`` header line, or an
        empty string when ``steps`` is empty.
    """
    if not steps:
        return ""
    seen: set[tuple[Any, ...]] = set()
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
