"""Tests for the unit-algebra trace mechanism (Phase D, spec §12)."""
from __future__ import annotations

from fractions import Fraction

from dimfort.core import unit_config  # noqa: F401 — populates DEFAULT_TABLE
from dimfort.core.trace import current_trace, trace_step, with_trace
from dimfort.core.units import (
    LogWrap,
    combine,
    parse,
    power,
    wrap_exp,
    wrap_log,
)


def test_trace_inactive_by_default():
    assert current_trace() is None


def test_with_trace_activates_collector():
    with with_trace() as trace:
        assert current_trace() is trace
        assert trace.steps == []
    assert current_trace() is None


def test_wrap_log_records_r31():
    with with_trace() as trace:
        wrap_log(parse("Pa"))
    assert len(trace.steps) == 1
    step = trace.steps[0]
    assert step.rule_id == "R3.1"
    assert step.after == LogWrap(parse("Pa"))


def test_wrap_log_records_r21_cancellation():
    with with_trace() as trace:
        wrap_log(wrap_exp(parse("K")))
    rules = [s.rule_id for s in trace.steps]
    # EXP fires R3.2, then LOG cancels via R2.1.
    assert "R3.2" in rules
    assert "R2.1" in rules


def test_wrap_log_of_dimless_records_r23():
    with with_trace() as trace:
        wrap_log(parse("1"))
    rules = [s.rule_id for s in trace.steps]
    assert rules == ["R2.3"]


def test_combine_records_r41_match():
    with with_trace() as trace:
        combine("+", parse("Pa"), parse("Pa"))
    assert trace.steps[-1].rule_id == "R4.1"


def test_combine_records_r41_mismatch():
    with with_trace() as trace:
        result, diag = combine("+", parse("Pa"), parse("K"))
    assert diag == "D1.1"
    assert trace.steps[-1].rule_id == "R4.1"
    assert trace.steps[-1].after is None


def test_combine_records_r53_log_minus_dimless():
    with with_trace() as trace:
        combine("-", wrap_log(parse("Pa")), parse("1"))
    rules = [s.rule_id for s in trace.steps]
    assert "R5.3" in rules


def test_combine_records_r51_log_homomorphism():
    with with_trace() as trace:
        combine("+", wrap_log(parse("Pa")), wrap_log(parse("Pa")))
    rules = [s.rule_id for s in trace.steps]
    # Inner R4.2 fires (Pa * Pa), then outer R5.1 wraps it.
    assert "R4.2" in rules
    assert "R5.1" in rules


def test_combine_records_r56_log_times_log():
    with with_trace() as trace:
        result, diag = combine("*", wrap_log(parse("Pa")), wrap_log(parse("Pa")))
    assert diag == "D1.2"
    assert trace.steps[-1].rule_id == "R5.6"
    assert trace.steps[-1].after is None


def test_combine_records_r71_log_times_exp():
    with with_trace() as trace:
        combine("*", wrap_log(parse("Pa")), wrap_exp(parse("K")))
    assert trace.steps[-1].rule_id == "R7.1"


def test_power_records_r43_literal():
    with with_trace() as trace:
        power(parse("m"), 2, exponent_is_literal=True)
    assert trace.steps[-1].rule_id == "R4.3"


def test_power_records_r43_nonliteral_error():
    with with_trace() as trace:
        power(parse("m"), 2, exponent_is_literal=False)
    assert trace.steps[-1].rule_id == "R4.3"
    assert trace.steps[-1].after is None


def test_power_records_r64_exp_squared():
    with with_trace() as trace:
        power(wrap_exp(parse("K")), 2, exponent_is_literal=True)
    assert trace.steps[-1].rule_id == "R6.4"


def test_no_trace_overhead_when_inactive():
    """trace_step is a no-op when no trace is active."""
    # Should not raise or affect state.
    trace_step("R4.1", (parse("Pa"),), parse("Pa"))
    assert current_trace() is None


def test_nested_with_trace_isolates():
    with with_trace() as outer:
        wrap_log(parse("Pa"))
        with with_trace() as inner:
            wrap_log(parse("K"))
        assert len(inner.steps) == 1
        assert inner.steps[0].after == LogWrap(parse("K"))
        # Outer trace should not include the inner step.
        assert len(outer.steps) == 1
        assert outer.steps[0].after == LogWrap(parse("Pa"))


def test_full_chain_hydrostatic():
    """The cdrag idiom chain: LOG / -dimless / EXP cancellation."""
    p = parse("Pa")
    dimless = parse("1")
    with with_trace() as trace:
        lp = wrap_log(p)
        absorbed, _ = combine("-", lp, dimless)
        wrap_exp(absorbed)
    rules = [s.rule_id for s in trace.steps]
    assert "R3.1" in rules  # LOG
    assert "R5.3" in rules  # subtract dim'less
    assert "R2.2" in rules  # EXP cancels the LOG
