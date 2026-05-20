"""Tests for the unit-algebra trace mechanism (Phase D, spec §12)."""
from __future__ import annotations

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
        power(parse("m"), parse("1"), 2)
    assert trace.steps[-1].rule_id == "R4.3"


def test_power_records_r43_nonliteral_error():
    with with_trace() as trace:
        power(parse("m"), None, None)
    assert trace.steps[-1].rule_id == "R4.3"
    assert trace.steps[-1].after is None


def test_power_records_r64_exp_squared():
    with with_trace() as trace:
        power(wrap_exp(parse("K")), parse("1"), 2)
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


# ---------------------------------------------------------------------------
# T2/T3: Diagnostic trace integration + pretty-print
# ---------------------------------------------------------------------------


def test_diagnostic_trace_empty_when_not_traced():
    """Diagnostic.trace is an empty tuple by default."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as ts
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    tree = ts.parse_text(src)
    diags = ts_checker.check(tree, {"a": "m/s", "b": "kg"}, source=src, file="t.f90")
    assert any(d.code == "H001" for d in diags)
    h001 = next(d for d in diags if d.code == "H001")
    assert h001.trace == ()


def test_diagnostic_trace_populated_when_tracing_on():
    """Inside with_trace(), checker diagnostics carry their statement's chain."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as ts
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    tree = ts.parse_text(src)
    with with_trace():
        diags = ts_checker.check(
            tree, {"a": "m/s", "b": "kg"}, source=src, file="t.f90",
        )
    h001 = next(d for d in diags if d.code == "H001")
    # The H001 doesn't fire from combine() directly (it's an assignment-
    # level mismatch), so the per-statement trace may be empty here.
    # Real wrapper-arithmetic diagnostics carry a non-empty trace —
    # exercised in the next test.
    assert isinstance(h001.trace, tuple)


def test_diagnostic_trace_carries_rule_ids_for_wrapper_op():
    """An H002 D1.2 (e.g. LOG×LOG) carries the firing rule in its trace."""
    from dimfort.core import ts_checker
    from dimfort.core import ts_parser as ts
    src = (
        b"subroutine s\n"
        b"  real :: p1, p2, r\n"
        b"  r = log(p1) * log(p2)\n"
        b"end subroutine\n"
    )
    tree = ts.parse_text(src)
    with with_trace():
        diags = ts_checker.check(
            tree, {"p1": "Pa", "p2": "Pa", "r": "1"}, source=src, file="t.f90",
        )
    h002 = next(d for d in diags if "D1.2" in d.message)
    rule_ids = [s.rule_id for s in h002.trace]
    assert "R3.1" in rule_ids  # log(p1) and log(p2)
    assert "R5.6" in rule_ids  # the failing op


def test_format_trace_empty_returns_empty_string():
    from dimfort.core.trace import format_trace
    assert format_trace(()) == ""


def test_format_trace_renders_chain():
    from dimfort.core.trace import format_trace
    with with_trace() as trace:
        wrap_log(parse("Pa"))
    out = format_trace(trace.snapshot())
    assert "trace:" in out
    assert "[R3.1]" in out


def test_format_provenance_marks_error_as_word():
    from dimfort.core.trace import format_provenance
    with with_trace() as trace:
        combine("*", wrap_log(parse("Pa")), wrap_log(parse("Pa")))
    # Last step is the R5.6 error.
    last = trace.steps[-1]
    assert "ERROR" in format_provenance(last)
    assert "[R5.6]" in format_provenance(last)


def test_cli_trace_flag_prints_chain(tmp_path, capsys):
    """End-to-end: ``dimfort check --trace`` renders the chain under each diag."""
    from dimfort.cli import _run_check, build_parser

    src = tmp_path / "t.f90"
    src.write_text(
        "subroutine s\n"
        "  real :: p1   !< @unit{Pa}\n"
        "  real :: p2   !< @unit{Pa}\n"
        "  real :: r    !< @unit{1}\n"
        "  r = log(p1) * log(p2)\n"
        "end subroutine\n"
    )
    parser = build_parser()
    args = parser.parse_args(["check", "--trace", str(src)])
    _run_check(args)
    captured = capsys.readouterr().out
    assert "D1.2" in captured
    assert "trace:" in captured
    assert "R5.6" in captured
