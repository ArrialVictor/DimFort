"""Unit-table config loader.

Builds a :class:`UnitTable` from one or more TOML files. The default table
is shipped at ``default_units.toml`` next to this module; users may provide
a project-local TOML that extends or overrides it.

TOML schema::

    [base]
    name = "M" | "L" | "T" | "Theta" | "I" | "N" | "J"

    [prefixes]
    name = <int> | "p/q"            # exact rational

    [derived]
    name = { expr = "<unit-expr>", prefixable = false }

Construction order:

1. Build base units (one slot each).
2. Build prefix table.
3. Resolve derived units by repeatedly parsing each ``expr`` against the
   table-in-progress; defer entries whose dependencies aren't yet defined.
   The loop terminates when nothing changes; any remaining entry is an error.
4. Expand ``prefix × prefixable`` and check no resulting name collides with
   an existing entry.
"""
from __future__ import annotations

import tomllib
from fractions import Fraction
from pathlib import Path

from dimfort.core import units as _units_mod
from dimfort.core.units import DIM_LEN, Unit, UnitError, UnitTable, UnknownUnitError

DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_units.toml")

_DIM_SLOT = {"M": 0, "L": 1, "T": 2, "Theta": 3, "I": 4, "N": 5, "J": 6}


def _coerce_factor(value: object) -> Fraction:
    if isinstance(value, bool):
        raise UnitError(f"prefix factor must be number/string, got {value!r}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(value)
    raise UnitError(f"prefix factor must be number/string, got {value!r}")


def _build_base(data: dict) -> dict[str, Unit]:
    base: dict[str, Unit] = {}
    for name, slot_name in data.items():
        if slot_name not in _DIM_SLOT:
            raise UnitError(f"base unit {name!r}: unknown dimension slot {slot_name!r}")
        idx = _DIM_SLOT[slot_name]
        dim = tuple(1 if i == idx else 0 for i in range(DIM_LEN))
        base[name] = Unit(dim, Fraction(1))
    return base


def _build_prefixes(data: dict) -> dict[str, Fraction]:
    return {name: _coerce_factor(value) for name, value in data.items()}


def _build_derived(
    data: dict, base: dict[str, Unit], prefixes: dict[str, Fraction]
) -> tuple[dict[str, Unit], frozenset[str]]:
    derived: dict[str, Unit] = {}
    prefixable: set[str] = set(base)  # base units always prefixable

    pending: dict[str, dict] = dict(data)
    while pending:
        progressed = False
        partial = UnitTable(
            base=base,
            derived=dict(derived),
            prefixable=frozenset(prefixable),
            prefixes=prefixes,
        )
        for name, spec in list(pending.items()):
            expr = spec.get("expr")
            if not isinstance(expr, str):
                raise UnitError(f"derived unit {name!r}: missing 'expr'")
            try:
                u = _units_mod.parse(expr, partial)
            except UnknownUnitError:
                continue
            # Optional scalar ``factor`` multiplies the parsed unit's
            # factor. Used for non-SI units whose value can't be
            # expressed by combining symbols (mbar = 100 Pa, atm =
            # 101325 Pa, etc.). Without this the only way to introduce
            # a scale was via the prefix table.
            factor_spec = spec.get("factor")
            if factor_spec is not None:
                u = Unit(u.dimension, u.factor * _coerce_factor(factor_spec))
            derived[name] = u
            if spec.get("prefixable", False):
                prefixable.add(name)
            del pending[name]
            progressed = True
        if not progressed:
            unresolved = ", ".join(sorted(pending))
            raise UnitError(
                f"derived units could not be resolved (cyclic or unknown "
                f"references): {unresolved}"
            )
    return derived, frozenset(prefixable)


def _check_collisions(table: UnitTable) -> None:
    seen: dict[str, str] = {}

    def add(name: str, origin: str) -> None:
        if name in seen:
            raise UnitError(
                f"name collision: {name!r} defined as {seen[name]} and {origin}"
            )
        seen[name] = origin

    for name in table.base:
        add(name, "base unit")
    for name in table.derived:
        add(name, "derived unit")
    for p in table.prefixes:
        for unit_name in table.prefixable:
            add(p + unit_name, f"prefix '{p}' + '{unit_name}'")


def _merge(base: dict, override: dict) -> dict:
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(user_path: Path | None = None) -> UnitTable:
    """Load the default unit config and (optionally) merge a user TOML on top."""
    with DEFAULT_CONFIG_PATH.open("rb") as f:
        data = tomllib.load(f)
    if user_path is not None:
        with user_path.open("rb") as f:
            user_data = tomllib.load(f)
        data = _merge(data, user_data)

    base = _build_base(data.get("base", {}))
    prefixes = _build_prefixes(data.get("prefixes", {}))
    derived, prefixable = _build_derived(data.get("derived", {}), base, prefixes)
    table = UnitTable(base=base, derived=derived, prefixable=prefixable, prefixes=prefixes)
    _check_collisions(table)
    return table


def install_default(user_path: Path | None = None) -> UnitTable:
    """Build a unit table and install it as the module-level default.

    Subsequent ``units.parse(expr)`` calls (without an explicit table) and
    every downstream component that reads ``_units_mod.DEFAULT_TABLE``
    pick up the new table. The CLI and LSP call this after resolving
    ``.dimfort.toml`` so project-specific units like LMDZ's ``degree`` /
    ``hPa`` / ``day`` are honoured.

    On any error (file missing, malformed, conflicting names) the
    shipped default is left in place — a bad ``[units] file`` must not
    break the pipeline. The caller is expected to log a warning.
    """
    try:
        table = load_config(user_path)
    except (OSError, UnitError, tomllib.TOMLDecodeError):
        return _units_mod.DEFAULT_TABLE
    _units_mod.DEFAULT_TABLE = table
    return table


# Initialise the module-level default so ``units.parse(expr)`` works
# without callers threading a table through.
_units_mod.DEFAULT_TABLE = load_config()
