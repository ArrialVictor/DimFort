"""Round-trip tests for cache artefact serialization.

For each cacheable type, build a non-trivial instance, dump → JSON →
load, then assert equality with the original. Equality is sufficient
because every type involved is a frozen dataclass with structural
equality.
"""
from __future__ import annotations

import json
from fractions import Fraction

import pytest

from dimfort.core.cache_serde import (
    dump_diagnostic,
    dump_exponent,
    dump_funcsig,
    dump_module_exports,
    dump_unit_expr,
    load_diagnostic,
    load_exponent,
    load_funcsig,
    load_module_exports,
    load_unit_expr,
)
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.symbols import FuncSig, ModuleExports
from dimfort.core.units import ZERO_DIM, Exponent, ExpWrap, LogWrap, Unit


def _roundtrip(dump, load, value):
    """Dump → JSON-string → JSON-parse → load; assert eq."""
    blob = json.dumps(dump(value))
    parsed = json.loads(blob)
    return load(parsed)


def test_exponent_pure_constant():
    e = Exponent.from_value(Fraction(3, 7))
    assert _roundtrip(dump_exponent, load_exponent, e) == e


def test_exponent_with_symbols():
    e = Exponent.build({"kappa": Fraction(2, 7), "lambda": -1}, constant=5)
    assert _roundtrip(dump_exponent, load_exponent, e) == e


def test_exponent_zero():
    assert _roundtrip(dump_exponent, load_exponent, Exponent.from_value(0)) == (
        Exponent.from_value(0)
    )


def test_unit_simple():
    u = Unit(ZERO_DIM, Fraction(1))
    assert _roundtrip(dump_unit_expr, load_unit_expr, u) == u


def test_unit_with_factor_and_symbolic_dim():
    dim = (
        Exponent.from_value(1),                       # mass kg
        Exponent.build({"kappa": Fraction(2, 7)}),    # symbolic length
        Exponent.from_value(-2),                      # time
        Exponent.from_value(0),
        Exponent.from_value(0),
        Exponent.from_value(0),
        Exponent.from_value(0),
    )
    u = Unit(dim, Fraction(1000))
    assert _roundtrip(dump_unit_expr, load_unit_expr, u) == u


def test_logwrap_and_expwrap():
    u = Unit(
        (Exponent.from_value(1),) + (Exponent.from_value(0),) * 6,
        Fraction(1),
    )
    log_u = LogWrap(u)
    exp_u = ExpWrap(u)
    assert _roundtrip(dump_unit_expr, load_unit_expr, log_u) == log_u
    assert _roundtrip(dump_unit_expr, load_unit_expr, exp_u) == exp_u


def test_nested_logwrap():
    u = Unit(
        (Exponent.from_value(1),) + (Exponent.from_value(0),) * 6,
        Fraction(1),
    )
    nested = LogWrap(ExpWrap(u))
    assert _roundtrip(dump_unit_expr, load_unit_expr, nested) == nested


def test_funcsig_full():
    u = Unit(
        (Exponent.from_value(1),) + (Exponent.from_value(0),) * 6,
        Fraction(1),
    )
    sig = FuncSig(
        arg_names=("a", "b", "c"),
        arg_units=(u, None, u),
        return_unit=u,
        is_subroutine=False,
    )
    assert _roundtrip(dump_funcsig, load_funcsig, sig) == sig


def test_funcsig_subroutine_no_return():
    sig = FuncSig(
        arg_names=("x",),
        arg_units=(None,),
        return_unit=None,
        is_subroutine=True,
    )
    assert _roundtrip(dump_funcsig, load_funcsig, sig) == sig


def test_module_exports():
    u = Unit(
        (Exponent.from_value(1),) + (Exponent.from_value(0),) * 6,
        Fraction(1),
    )
    sig = FuncSig(
        arg_names=("x",), arg_units=(u,), return_unit=u, is_subroutine=False,
    )
    exports = ModuleExports(
        name="mymod",
        var_units={"alpha": u, "beta": u},
        signatures={"f": sig},
        all_var_names=("alpha", "beta", "gamma"),
    )
    assert _roundtrip(dump_module_exports, load_module_exports, exports) == exports


def test_diagnostic_drops_trace():
    diag = Diagnostic(
        file="x.f90",
        start=Position(line=3, column=5),
        end=Position(line=3, column=10),
        severity=Severity.ERROR,
        code="H001",
        message="assignment unit mismatch (D1.4): kg ◂ m",
    )
    rt = _roundtrip(dump_diagnostic, load_diagnostic, diag)
    assert rt == diag
    # Trace is empty on rehydrate (explicit guarantee — the cache
    # is for diagnostic delivery, not provenance replay).
    assert rt.trace == ()


def test_dump_unit_expr_rejects_non_unit_expr():
    """The dispatch should refuse to silently mis-serialize a
    non-UnitExpr — better to crash at write time than load garbage."""
    with pytest.raises(TypeError):
        dump_unit_expr("not a unit")  # type: ignore[arg-type]


def test_load_unit_expr_rejects_unknown_tag():
    """An unknown tag in a payload is a forward-compat / corruption
    signal; the loader must refuse rather than build a wrong object."""
    with pytest.raises(ValueError):
        load_unit_expr({"_t": "NotARealTag"})
