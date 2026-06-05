"""JSON-serialisation of cacheable artefacts.

Each cacheable type has a ``dump_X`` / ``load_X`` pair. The format is a
small tagged-dict scheme: every non-trivial dict carries a ``"_t"`` key
naming the type, so a hand-edited cache file can be inspected.

These are the types currently round-tripped:

- :class:`Exponent`            (units.Exponent)
- :class:`Unit`                (units.Unit)
- :class:`LogWrap`, :class:`ExpWrap`
- :class:`FuncSig`             (symbols.FuncSig)
- :class:`ModuleExports`       (symbols.ModuleExports)
- :class:`Diagnostic`          (diagnostics.Diagnostic, *without* trace)

The ``trace`` field of :class:`Diagnostic` is deliberately dropped: it
exists for the active checker run's UI/debug surface and re-loading a
cached trace would re-attach provenance pointing at stale line numbers.
Cache consumers wanting full traces should bypass the cache.

The shape of a payload is **stable per CHECKER_OUTPUT_VERSION**. Any
change here that doesn't add a back-compat path must bump that constant
(see ``cache_key.CHECKER_OUTPUT_VERSION``).
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.symbols import FuncSig, ModuleExports
from dimfort.core.units import Exponent, ExpWrap, LogWrap, Unit, UnitExpr

# ---------------------------------------------------------------------------
# Fraction

def dump_fraction(f: Fraction | int) -> str:
    """Serialize a rational as ``"num/den"`` (or ``"num"`` if den==1)."""
    fr = f if isinstance(f, Fraction) else Fraction(f)
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator}"


def load_fraction(s: str) -> Fraction:
    if "/" in s:
        num, den = s.split("/", 1)
        return Fraction(int(num), int(den))
    return Fraction(int(s))


# ---------------------------------------------------------------------------
# Exponent

def dump_exponent(e: Exponent) -> dict[str, Any]:
    return {
        "_t": "Exp",
        "t": [[name, dump_fraction(coeff)] for name, coeff in e.terms],
        "c": dump_fraction(e.constant),
    }


def load_exponent(d: dict[str, Any]) -> Exponent:
    return Exponent.build(
        terms={name: load_fraction(coeff) for name, coeff in d["t"]},
        constant=load_fraction(d["c"]),
    )


# ---------------------------------------------------------------------------
# Unit / LogWrap / ExpWrap

def dump_unit_expr(u: UnitExpr) -> dict[str, Any]:
    if isinstance(u, Unit):
        out: dict[str, Any] = {
            "_t": "U",
            "d": [dump_exponent(x) for x in u.dimension],
            "f": dump_fraction(u.factor),
        }
        # Tyvars only emitted when non-empty: keeps every pre-polymorphism
        # cache entry byte-identical (smaller payload, no diff in
        # serialised form for concrete Units).
        if u.tyvars:
            out["v"] = [[name, dump_exponent(exp)] for name, exp in u.tyvars]
        return out
    if isinstance(u, LogWrap):
        return {"_t": "Log", "i": dump_unit_expr(u.inner)}
    if isinstance(u, ExpWrap):
        return {"_t": "Exp1", "i": dump_unit_expr(u.inner)}
    raise TypeError(f"not a UnitExpr: {type(u).__name__}")


def load_unit_expr(d: dict[str, Any]) -> UnitExpr:
    tag = d["_t"]
    if tag == "U":
        raw_v = d.get("v", [])
        tyvars = tuple(
            (name, load_exponent(exp)) for name, exp in raw_v
        )
        return Unit(
            dimension=tuple(load_exponent(x) for x in d["d"]),
            factor=load_fraction(d["f"]),
            tyvars=tyvars,
        )
    if tag == "Log":
        return LogWrap(inner=load_unit_expr(d["i"]))
    if tag == "Exp1":
        return ExpWrap(inner=load_unit_expr(d["i"]))
    raise ValueError(f"unknown unit-expr tag: {tag!r}")


# ---------------------------------------------------------------------------
# FuncSig

def dump_funcsig(s: FuncSig) -> dict[str, Any]:
    return {
        "_t": "Sig",
        "an": list(s.arg_names),
        "au": [None if u is None else dump_unit_expr(u) for u in s.arg_units],
        "ru": None if s.return_unit is None else dump_unit_expr(s.return_unit),
        "sub": s.is_subroutine,
    }


def load_funcsig(d: dict[str, Any]) -> FuncSig:
    return FuncSig(
        arg_names=tuple(d["an"]),
        arg_units=tuple(
            None if u is None else load_unit_expr(u) for u in d["au"]
        ),
        return_unit=(
            None if d["ru"] is None else load_unit_expr(d["ru"])
        ),
        is_subroutine=d["sub"],
    )


# ---------------------------------------------------------------------------
# ModuleExports

def dump_module_exports(m: ModuleExports) -> dict[str, Any]:
    return {
        "_t": "Exports",
        "n": m.name,
        "v": {k: dump_unit_expr(u) for k, u in m.var_units.items()},
        "s": {k: dump_funcsig(sig) for k, sig in m.signatures.items()},
        "av": list(m.all_var_names),
    }


def load_module_exports(d: dict[str, Any]) -> ModuleExports:
    return ModuleExports(
        name=d["n"],
        var_units={k: load_unit_expr(u) for k, u in d["v"].items()},
        signatures={k: load_funcsig(s) for k, s in d["s"].items()},
        all_var_names=tuple(d["av"]),
    )


# ---------------------------------------------------------------------------
# Diagnostic (sans trace)

def dump_diagnostic(g: Diagnostic) -> dict[str, Any]:
    return {
        "_t": "Diag",
        "f": g.file,
        "sl": g.start.line, "sc": g.start.column,
        "el": g.end.line, "ec": g.end.column,
        "sv": g.severity.value,
        "c": g.code,
        "m": g.message,
    }


def load_diagnostic(d: dict[str, Any]) -> Diagnostic:
    return Diagnostic(
        file=d["f"],
        start=Position(line=d["sl"], column=d["sc"]),
        end=Position(line=d["el"], column=d["ec"]),
        severity=Severity(d["sv"]),
        code=d["c"],
        message=d["m"],
    )
