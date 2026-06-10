"""Round-trip tests for the M5 module-exports cache disk codec.

Mirrors the M4 ProjectionCache persistence tests. Each test constructs
a representative payload, saves it, loads it back, and asserts
structural equality. The tricky cases — affine units (degC offset),
polymorphic 'a, LogWrap / ExpWrap, prefactors, empty-but-present
collections — get their own focused tests because they're the ones
most likely to silently lose information through a half-baked codec.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from dimfort.core.multifile_cache import ExportsKey, ModuleExportsCache
from dimfort.core.multifile_exports_cache_persist import (
    _dump_module_exports,
    _dump_unit_expr,
    _load_module_exports,
    _load_unit_expr,
    load_persistent_exports_cache,
    save_persistent_exports_cache,
)
from dimfort.core.symbols import FuncSig, ModuleExports
from dimfort.core.units import (
    DEFAULT_TABLE,
    Exponent,
    ExpWrap,
    LogWrap,
    Unit,
    parse,
)
from dimfort.core.workspace_index import UseRef

# ---------------------------------------------------------------------------
# UnitExpr codec — direct round-trip
# ---------------------------------------------------------------------------


def _rt(u):
    """Encode + decode one UnitExpr; return the reconstructed value."""
    return _load_unit_expr(_dump_unit_expr(u))


def test_plain_si_unit_round_trips() -> None:
    """``m/s`` survives encode + decode."""
    u = parse("m/s", DEFAULT_TABLE)
    assert _rt(u) == u


def test_unit_with_prefactor_round_trips() -> None:
    """``hPa`` carries a prefactor of 100 — must survive."""
    u = parse("hPa", DEFAULT_TABLE)
    assert _rt(u) == u
    rt = _rt(u)
    assert isinstance(rt, Unit)
    assert rt.factor == u.factor  # type: ignore[union-attr]


def test_affine_unit_round_trips() -> None:
    """``degC`` carries an offset of 273.15 — must survive (the lossy
    ``format_unit_source`` path drops it)."""
    u = parse("degC", DEFAULT_TABLE)
    rt = _rt(u)
    assert isinstance(rt, Unit)
    assert rt == u
    assert rt.offset == u.offset  # type: ignore[union-attr]
    assert rt.offset != Fraction(0)  # type: ignore[union-attr]


def test_log_wrap_round_trips() -> None:
    """``LOG(Pa)`` survives."""
    u = LogWrap(parse("Pa", DEFAULT_TABLE))
    rt = _rt(u)
    assert isinstance(rt, LogWrap)
    assert rt == u


def test_exp_wrap_round_trips() -> None:
    """``EXP(LOG(Pa))`` (nested wrappers) survives."""
    u = ExpWrap(LogWrap(parse("Pa", DEFAULT_TABLE)))
    rt = _rt(u)
    assert isinstance(rt, ExpWrap)
    assert rt == u


def test_polymorphic_tyvar_round_trips() -> None:
    """A polymorphic unit ``'a * m`` survives.

    Builds a Unit with one ``'a`` tyvar of exponent 1 plus a metres
    dimension slot.
    """
    metres = parse("m", DEFAULT_TABLE)
    tyvars = (
        ("'a", Exponent(terms=(), constant=Fraction(1))),
    )
    u = Unit(
        dimension=metres.dimension,
        factor=metres.factor,
        offset=metres.offset,
        tyvars=tyvars,
    )
    rt = _rt(u)
    assert isinstance(rt, Unit)
    assert rt.tyvars == tyvars
    assert rt == u


def test_exponent_with_symbolic_terms_round_trips() -> None:
    """An Exponent carrying both a symbolic term and a constant survives."""
    e = Exponent(
        terms=(("kappa", Fraction(2, 7)),),
        constant=Fraction(1, 3),
    )
    u = parse("m", DEFAULT_TABLE)
    # Inject e as the M slot so the codec exercises the full Exponent.
    swapped = Unit(
        dimension=(e,) + u.dimension[1:],
        factor=u.factor,
        offset=u.offset,
        tyvars=u.tyvars,
    )
    rt = _rt(swapped)
    assert rt == swapped


# ---------------------------------------------------------------------------
# ModuleExports codec — round-trip
# ---------------------------------------------------------------------------


def test_module_exports_round_trips() -> None:
    """A populated ModuleExports with vars + signatures + uses + flags survives."""
    pa = parse("Pa", DEFAULT_TABLE)
    ms = parse("m/s", DEFAULT_TABLE)
    sig = FuncSig(
        arg_names=("p", "v"),
        arg_units=(pa, ms),
        return_unit=LogWrap(pa),
        is_subroutine=False,
    )
    m = ModuleExports(
        name="phys_constants",
        var_units={"g": ms, "p_ref": pa},
        signatures={"compute": sig},
        all_var_names=("g", "p_ref", "unannotated_var"),
        inner_uses=(
            UseRef(module="mod_base", only=("x", "y"),
                   renames=(("local", "remote"),)),
            UseRef(module="mod_other", only=None, renames=()),
        ),
        default_private=True,
        public_names=frozenset({"g", "compute"}),
        private_names=frozenset({"internal_x"}),
    )
    rt = _load_module_exports(_dump_module_exports(m))
    assert rt == m


def test_empty_module_exports_round_trips() -> None:
    """A ModuleExports with no vars / no sigs / no uses survives."""
    m = ModuleExports(name="empty_mod", var_units={}, signatures={})
    rt = _load_module_exports(_dump_module_exports(m))
    assert rt == m


# ---------------------------------------------------------------------------
# Full save / load
# ---------------------------------------------------------------------------


def _populate_cache() -> ModuleExportsCache:
    cache = ModuleExportsCache()
    pa = parse("Pa", DEFAULT_TABLE)
    sig = FuncSig(arg_names=("x",), arg_units=(pa,), return_unit=pa)
    mods = {
        "mod1": ModuleExports(
            name="mod1",
            var_units={"v": pa},
            signatures={"f": sig},
        ),
    }
    cache.put(
        ExportsKey(content_hash="abc123", merged_units_digest="ctx-1"),
        ({"f": sig}, mods),
    )
    cache.put(
        ExportsKey(content_hash="def456", merged_units_digest="ctx-1"),
        ({}, {}),
    )
    return cache


def test_save_then_load_round_trips(tmp_path: Path) -> None:
    """End-to-end: populate, save, load, compare entries."""
    cache = _populate_cache()
    save_persistent_exports_cache(cache, tmp_path)
    loaded = load_persistent_exports_cache(tmp_path)
    assert loaded is not None
    # Compare entries directly — keys are frozen dataclasses, values
    # are dicts of frozen dataclasses, all support structural ==.
    assert dict(loaded._entries) == dict(cache._entries)  # noqa: SLF001


def test_load_missing_returns_none(tmp_path: Path) -> None:
    """No file on disk → None (caller starts cold)."""
    assert load_persistent_exports_cache(tmp_path) is None


def test_load_garbage_returns_none(tmp_path: Path) -> None:
    """Corrupt JSON → None, no exception."""
    (tmp_path / "module-exports-cache.json").write_text("{this is not json}")
    assert load_persistent_exports_cache(tmp_path) is None


def test_load_wrong_version_returns_none(tmp_path: Path) -> None:
    """Schema version mismatch → None."""
    (tmp_path / "module-exports-cache.json").write_text(
        '{"version": 9999, "entries": []}',
    )
    assert load_persistent_exports_cache(tmp_path) is None


def test_save_creates_cache_root(tmp_path: Path) -> None:
    """Cache directory is created if missing."""
    nested = tmp_path / "deeply" / "nested" / "cache"
    cache = _populate_cache()
    save_persistent_exports_cache(cache, nested)
    assert (nested / "module-exports-cache.json").exists()
