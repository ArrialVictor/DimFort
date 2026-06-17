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
    # Catalog form (preferred):
    name = {
      dim          = "<SI slot product>",       # "M*L^-1*T^-2"
      factor       = <int> or "<p/q>",          # scale to base SI; default 1
      offset       = <num>,                     # affine offset; default 0
      quantitykind = "<vocabulary>",            # semantic tag; ignored at load
      aliases      = ["<name>", ...],           # alternate names
      prefixable   = false,                     # opt-in to prefix expansion
    }
    # Compact form (project-local convenience; same semantics):
    name = { expr = "<existing-unit-expr>", factor = <n>, offset = <n> }

Construction order:

1. Build base units (one slot each).
2. Build prefix table.
3. Resolve derived units. Catalog-form (``dim``) entries resolve immediately
   without dependency; ``expr``-form entries iterate until convergence.
4. Register declared aliases as additional names pointing at the same
   :class:`Unit` instance.
5. Expand ``prefix × prefixable`` and check no resulting name collides with
   an existing entry, prefix name, or registered alias.

Override gate: project TOML rejected if it attempts to redefine an existing
``[base]`` or ``[prefixes]`` entry. Adding new entries to ``[prefixes]`` and
``[derived]`` is permitted. ``[base]`` may not be extended (the seven SI base
units are fixed by the standard).
"""
from __future__ import annotations

import tomllib
import warnings
from fractions import Fraction
from pathlib import Path
from typing import Any

from dimfort.core import units as _units_mod
from dimfort.core.units import (
    DIM_LEN,
    Exponent,
    Unit,
    UnitAmbiguityWarning,
    UnitError,
    UnitTable,
    UnknownUnitError,
)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("default_units.toml")

_DIM_SLOT = {"M": 0, "L": 1, "T": 2, "Theta": 3, "I": 4, "N": 5, "J": 6}
_DIM_SLOT_NAMES = tuple(_DIM_SLOT.keys())  # for error messages

# Recognised optional keys on a [derived] entry (catalog form).
_DERIVED_KEYS = {
    "expr", "dim", "factor", "offset", "quantitykind", "aliases", "prefixable",
}


def _coerce_factor(value: object) -> Fraction:
    """Coerce a TOML scalar into an exact :class:`Fraction`.

    Accepts ``int`` and string forms (``"0.1"``, ``"1/3"``). Rejects
    ``bool`` and ``float`` to keep prefix / factor / offset values
    exact — floats round-trip lossy through :class:`Fraction` and
    would carry that imprecision into every diagnostic message.

    Args:
        value: Raw TOML value (int, str, float, bool, ...).

    Returns:
        Exact rational equivalent of ``value``.

    Raises:
        UnitError: ``value`` is a ``float``, a ``bool``, or any other
            non-string / non-int type.
    """
    if isinstance(value, bool):
        raise UnitError(f"prefix factor must be number/string, got {value!r}")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, str):
        return Fraction(value)
    if isinstance(value, float):
        # Floats round-trip lossy through Fraction (``Fraction(0.1)`` =
        # 3602879701896397/36028797018963968) — the resulting prefix
        # factor would carry that imprecision into every diagnostic
        # message. Force the author to use the string form ``"0.1"`` /
        # ``"1/3"`` etc. instead. The matching ``[units]`` doc comment
        # at config.py:115 already steers users to the string form;
        # this turns the silent footgun into a hard error.
        raise UnitError(
            f"prefix factor must be a string for non-integer values "
            f"(got float {value!r}); write it as a quoted decimal or "
            f"fraction, e.g. \"0.1\" or \"1/10\""
        )
    raise UnitError(f"prefix factor must be number/string, got {value!r}")


def _build_base(data: dict[str, Any]) -> dict[str, Unit]:
    """Build the base-unit map from a ``[base]`` TOML table.

    Each entry maps a base-unit symbol to one of the seven SI slot
    names (``"M"``, ``"L"``, ``"T"``, ``"Theta"``, ``"I"``, ``"N"``,
    ``"J"``).

    Args:
        data: The parsed ``[base]`` subtable.

    Returns:
        Map from base-unit name to a :class:`Unit` carrying a single
        unit exponent in its assigned slot.

    Raises:
        UnitError: An entry references an unknown SI slot name.
    """
    base: dict[str, Unit] = {}
    for name, slot_name in data.items():
        if slot_name not in _DIM_SLOT:
            raise UnitError(f"base unit {name!r}: unknown dimension slot {slot_name!r}")
        idx = _DIM_SLOT[slot_name]
        dim = tuple(Exponent.from_value(1 if i == idx else 0) for i in range(DIM_LEN))
        base[name] = Unit(dim, Fraction(1))
    return base


def _build_prefixes(data: dict[str, Any]) -> dict[str, Fraction]:
    """Build the prefix map from a ``[prefixes]`` TOML table.

    Args:
        data: The parsed ``[prefixes]`` subtable.

    Returns:
        Map from prefix symbol to its exact rational factor.

    Raises:
        UnitError: A value cannot be coerced to an exact rational
            (see :func:`_coerce_factor`).
    """
    return {name: _coerce_factor(value) for name, value in data.items()}


def _parse_dim_string(s: str, entry_name: str) -> tuple[Exponent, ...]:
    """Parse a ``dim`` string like ``"M*L^-1*T^-2"`` into a SI slot tuple.

    Empty product ``"1"`` denotes the dimensionless unit. Other forms
    are ``*``-joined slot terms; each term is ``<slot>`` or
    ``<slot>^<int>``. Slot symbols are ``M L T Theta I N J``.

    Args:
        s: The dim string.
        entry_name: Name of the entry being parsed (for error messages).

    Returns:
        Seven-tuple of :class:`Exponent` over the SI base slots in
        canonical order.

    Raises:
        UnitError: Unknown slot symbol, malformed exponent, or duplicate
            slot in the product.
    """
    s = s.strip()
    exponents: list[int] = [0] * DIM_LEN
    if s in ("1", ""):
        return tuple(Exponent.from_value(0) for _ in range(DIM_LEN))
    seen: set[str] = set()
    for raw_term in s.split("*"):
        term = raw_term.strip()
        if not term:
            raise UnitError(
                f"derived unit {entry_name!r}: empty term in dim {s!r}"
            )
        if "^" in term:
            slot, _, exp_str = term.partition("^")
            slot = slot.strip()
            exp_str = exp_str.strip()
            try:
                exp = int(exp_str)
            except ValueError as exc:
                raise UnitError(
                    f"derived unit {entry_name!r}: non-integer exponent "
                    f"in dim {s!r} term {term!r}"
                ) from exc
        else:
            slot = term
            exp = 1
        if slot not in _DIM_SLOT:
            raise UnitError(
                f"derived unit {entry_name!r}: unknown slot {slot!r} in "
                f"dim {s!r}; valid slots: {', '.join(_DIM_SLOT_NAMES)}"
            )
        if slot in seen:
            raise UnitError(
                f"derived unit {entry_name!r}: slot {slot!r} appears more "
                f"than once in dim {s!r}; combine into a single term"
            )
        seen.add(slot)
        exponents[_DIM_SLOT[slot]] = exp
    return tuple(Exponent.from_value(e) for e in exponents)


def _build_unit_from_dim(name: str, spec: dict[str, Any]) -> Unit:
    """Construct a :class:`Unit` from a catalog-form ``dim`` spec.

    No parser dependency; the entry stands alone. Applies optional
    ``factor`` and ``offset``.

    Args:
        name: Entry name (for error messages).
        spec: TOML dict with at least ``dim``; optional ``factor``, ``offset``.

    Returns:
        The constructed :class:`Unit`.

    Raises:
        UnitError: Malformed dim or factor/offset spec.
    """
    dim_str = spec["dim"]
    if not isinstance(dim_str, str):
        raise UnitError(
            f"derived unit {name!r}: 'dim' must be a string, got {dim_str!r}"
        )
    dim = _parse_dim_string(dim_str, name)
    factor = _coerce_factor(spec["factor"]) if "factor" in spec else Fraction(1)
    offset = _coerce_factor(spec["offset"]) if "offset" in spec else Fraction(0)
    return Unit(dim, factor, offset)


def _validate_derived_spec(name: str, spec: dict[str, Any]) -> None:
    """Reject unknown keys on a [derived] entry; surface typos early."""
    unknown = set(spec.keys()) - _DERIVED_KEYS
    if unknown:
        raise UnitError(
            f"derived unit {name!r}: unknown keys {sorted(unknown)}; "
            f"valid keys: {sorted(_DERIVED_KEYS)}"
        )
    has_dim = "dim" in spec
    has_expr = "expr" in spec
    if has_dim and has_expr:
        raise UnitError(
            f"derived unit {name!r}: cannot specify both 'dim' and 'expr'"
        )
    if not has_dim and not has_expr:
        raise UnitError(
            f"derived unit {name!r}: must specify either 'dim' or 'expr'"
        )


def _build_derived(
    data: dict[str, Any], base: dict[str, Unit], prefixes: dict[str, Fraction]
) -> tuple[dict[str, Unit], frozenset[str], dict[str, list[str]]]:
    """Resolve the ``[derived]`` TOML table against an in-progress table.

    Each entry uses either the catalog form (``dim`` string, resolved
    immediately) or the compact form (``expr`` string, resolved through
    the parser against the table-so-far). ``expr`` entries iterate until
    convergence; any cyclic / unresolved entry is reported.

    After construction, declared ``aliases`` are registered as additional
    names pointing at the same :class:`Unit` instance.

    Args:
        data: The parsed ``[derived]`` subtable.
        base: Already-built base-unit map.
        prefixes: Already-built prefix map.

    Returns:
        A triple ``(derived_units, prefixable_names, alias_origins)``:

        - ``derived_units`` — name → Unit (includes alias entries pointing
          at the same Unit instance as their canonical name)
        - ``prefixable_names`` — base units plus derived entries flagged
          ``prefixable = true``
        - ``alias_origins`` — canonical name → list of registered aliases
          (for diagnostics / hover; aliases don't have their own entry)

    Raises:
        UnitError: An entry is malformed, parses to a non-:class:`Unit`,
            an alias collides with another entry, or an ``expr`` entry
            cannot be resolved (cyclic or unknown references).
    """
    for name, spec in data.items():
        _validate_derived_spec(name, spec)

    derived: dict[str, Unit] = {}
    prefixable: set[str] = set(base)  # base units always prefixable
    alias_origins: dict[str, list[str]] = {}

    # Pass 1: build catalog-form (``dim``) entries — no dependencies.
    pending: dict[str, dict[str, Any]] = {}
    for name, spec in data.items():
        if "dim" in spec:
            derived[name] = _build_unit_from_dim(name, spec)
            if spec.get("prefixable", False):
                prefixable.add(name)
        else:
            pending[name] = spec  # expr-form, deferred

    # Pass 2: iterate over ``expr`` entries until convergence.
    while pending:
        progressed = False
        partial = UnitTable(
            base=base,
            derived=dict(derived),
            prefixable=frozenset(prefixable),
            prefixes=prefixes,
        )
        for name, spec in list(pending.items()):
            expr = spec["expr"]
            if not isinstance(expr, str):
                raise UnitError(f"derived unit {name!r}: 'expr' must be a string")
            try:
                parsed = _units_mod.parse(expr, partial)
            except UnknownUnitError:
                continue
            if not isinstance(parsed, Unit):
                raise UnitError(
                    f"derived unit {name!r}: expression {expr!r} is not a plain unit"
                )
            u = parsed
            factor_spec = spec.get("factor")
            offset_spec = spec.get("offset")
            if factor_spec is not None or offset_spec is not None:
                new_factor = (
                    u.factor * _coerce_factor(factor_spec)
                    if factor_spec is not None else u.factor
                )
                new_offset = (
                    u.offset + _coerce_factor(offset_spec)
                    if offset_spec is not None else u.offset
                )
                u = Unit(u.dimension, new_factor, new_offset)
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

    # Pass 3: register aliases. Each alias is added to ``derived`` pointing
    # at the same Unit instance as its canonical entry. Aliases inherit the
    # canonical's prefixable flag implicitly through name lookup at parse
    # time — we don't add them to ``prefixable`` (would multiply the prefix-
    # expansion namespace unnecessarily).
    for name, spec in data.items():
        aliases = spec.get("aliases")
        if aliases is None:
            continue
        if not isinstance(aliases, list):
            raise UnitError(
                f"derived unit {name!r}: 'aliases' must be a list, got {aliases!r}"
            )
        canonical_unit = derived[name]
        for alias in aliases:
            if not isinstance(alias, str):
                raise UnitError(
                    f"derived unit {name!r}: alias entries must be strings, "
                    f"got {alias!r}"
                )
            if alias in derived:
                raise UnitError(
                    f"alias collision: {alias!r} (declared as alias of "
                    f"{name!r}) is already a derived unit"
                )
            if alias in base:
                raise UnitError(
                    f"alias collision: {alias!r} (declared as alias of "
                    f"{name!r}) is already a base unit"
                )
            if alias in prefixes:
                raise UnitError(
                    f"alias collision: {alias!r} (declared as alias of "
                    f"{name!r}) is already a prefix"
                )
            derived[alias] = canonical_unit
            alias_origins.setdefault(name, []).append(alias)

    return derived, frozenset(prefixable), alias_origins


def _check_collisions(table: UnitTable) -> None:
    """Reject a unit table whose names collide after prefix expansion.

    Walks the base names, the derived names, and the cross-product of
    prefixes × prefixable entries; raises on the first duplicate.

    Args:
        table: Fully-built unit table to validate.

    Raises:
        UnitError: A unit name is defined twice (e.g. a prefix-expanded
            name shadowing an explicit derived entry).
    """
    seen: dict[str, str] = {}

    def add(name: str, origin: str) -> None:
        """Record ``name`` against ``origin``, rejecting collisions."""
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


def _check_override_gate(
    defaults: dict[str, Any], user_data: dict[str, Any]
) -> None:
    """Reject project overrides of base / prefix entries; warn on derived.

    The 7 SI base units and the 20 SI prefixes are foundational; user
    projects must not silently redefine them. Re-declaring a base or prefix
    in the project TOML is a hard error. Adding NEW base entries is also
    rejected (DimFort's algebra is fixed at the 7 SI dimensions; new base
    units would require a wider rewrite). Adding new prefixes is permitted.

    Derived units may be overridden; users sometimes have project-specific
    conventions. We warn so silent shadowing is impossible.

    Args:
        defaults: The shipped defaults' parsed TOML data.
        user_data: The project's parsed TOML data.

    Raises:
        UnitError: A user entry redefines a shipped base or prefix, or
            adds a new base entry.
    """
    user_base = user_data.get("base", {})
    for name in user_base:
        if name in defaults.get("base", {}):
            raise UnitError(
                f"project config cannot redefine base unit {name!r}; the seven "
                "SI base units are fixed by the standard"
            )
        # Adding new base units is also rejected — algebra is fixed at 7 slots.
        raise UnitError(
            f"project config cannot add new base unit {name!r}; DimFort's "
            "dimensional algebra is fixed at the 7 SI base units"
        )
    user_prefixes = user_data.get("prefixes", {})
    for name in user_prefixes:
        if name in defaults.get("prefixes", {}):
            raise UnitError(
                f"project config cannot redefine prefix {name!r}; the SI "
                "prefixes are fixed by the standard"
            )
        # Adding new prefixes is allowed (e.g. binary prefixes Ki/Mi/Gi).
    user_derived = user_data.get("derived", {})
    default_derived_names = set(defaults.get("derived", {}).keys())
    overridden = sorted(set(user_derived.keys()) & default_derived_names)
    if overridden:
        warnings.warn(
            f"project config redefines shipped derived units: {overridden}. "
            "Project-local values override the defaults; this may shadow "
            "values the doctor lint expects.",
            UnitAmbiguityWarning,
            stacklevel=3,
        )


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base`` with section-aware semantics.

    At the top level (``[base]`` / ``[prefixes]`` / ``[derived]``), entries
    from ``override`` are merged INTO the section dict — i.e. an override's
    `Pa` entry overrides the default's `Pa` entry ATOMICALLY (no recursive
    merging of the entry's own keys). This prevents the new dim/expr schema
    from producing entries with both fields after partial overrides.

    Neither argument is mutated.

    Args:
        base: Lower-precedence TOML data (the shipped defaults).
        override: Higher-precedence TOML data (the project file).

    Returns:
        A new dict carrying the merged result.
    """
    out = {k: dict(v) if isinstance(v, dict) else v for k, v in base.items()}
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            # Merge sections key-by-key; atomic at the entry level.
            section_out = dict(out[k])
            for sub_k, sub_v in v.items():
                section_out[sub_k] = sub_v
            out[k] = section_out
        else:
            out[k] = v
    return out


def load_config(user_path: Path | None = None) -> UnitTable:
    """Load the default unit config and optionally merge a user TOML on top.

    Args:
        user_path: Optional path to a project-local TOML file whose
            ``[base]``, ``[prefixes]``, and ``[derived]`` subtables
            extend or override the shipped defaults.

    Returns:
        Fully resolved :class:`UnitTable` with collision detection
        already run.

    Raises:
        UnitError: A name collision, an unresolved derived entry, a
            malformed scalar, or a project override of a shipped base /
            prefix entry.
        OSError: ``user_path`` cannot be opened.
        tomllib.TOMLDecodeError: ``user_path`` is not valid TOML.
    """
    with DEFAULT_CONFIG_PATH.open("rb") as f:
        defaults_data = tomllib.load(f)
    data: dict[str, Any] = dict(defaults_data)
    if user_path is not None:
        with user_path.open("rb") as f:
            user_data = tomllib.load(f)
        # Reject project overrides of base / prefixes before merge; warn on
        # derived overrides so silent shadowing is impossible.
        _check_override_gate(defaults_data, user_data)
        data = _merge(defaults_data, user_data)

    base = _build_base(data.get("base", {}))
    prefixes = _build_prefixes(data.get("prefixes", {}))
    derived, prefixable, _aliases = _build_derived(
        data.get("derived", {}), base, prefixes
    )
    table = UnitTable(base=base, derived=derived, prefixable=prefixable, prefixes=prefixes)
    _check_collisions(table)
    return table


def install_default(user_path: Path | None = None) -> UnitTable:
    """Build a unit table and install it as the module-level default.

    Subsequent ``units.parse(expr)`` calls (without an explicit table)
    and every downstream component that reads ``_units_mod.DEFAULT_TABLE``
    pick up the new table. The CLI and LSP call this after resolving
    ``dimfort.toml`` so project-specific units (``degree`` / ``hPa`` /
    ``day``, etc.) are honoured.

    Args:
        user_path: Optional path to a project-local unit-table TOML.

    Returns:
        The newly installed :class:`UnitTable` on success, or the
        previously installed table when ``user_path`` is broken.

    Note:
        On any error (file missing, malformed, conflicting names) the
        shipped default is left in place — a bad ``[units] file`` must
        not break the pipeline. The caller is expected to log a
        warning.
    """
    try:
        table = load_config(user_path)
    except (OSError, UnitError, tomllib.TOMLDecodeError):
        # Keep the current default (always initialised at module import below).
        current = _units_mod.DEFAULT_TABLE
        assert current is not None
        return current
    _units_mod.DEFAULT_TABLE = table
    return table


# Initialise the module-level default so ``units.parse(expr)`` works
# without callers threading a table through.
_units_mod.DEFAULT_TABLE = load_config()
