"""Parser-agnostic symbol data: signatures, intrinsic tables, modules.

This module holds the pieces of the checker that are independent of the
AST shape — pure data classes and lookup tables. The tree-sitter
checker (:mod:`dimfort.core.ts_checker`) consumes them; previously they
lived inside ``core.checker`` and ``core.ast_checker`` alongside
LFortran-specific code.

Diagnostic codes are kept here too (``CODES``) so anywhere that needs
the canonical severity/description for an H- or U- code has a single
import to reach for.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from dimfort.core.diagnostics import Severity
from dimfort.core.units import Unit

# ---------------------------------------------------------------------------
# Diagnostic-code metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeSpec:
    code: str
    severity: Severity
    description: str


CODES: dict[str, CodeSpec] = {
    "H001": CodeSpec("H001", Severity.ERROR, "assignment unit mismatch"),
    "H002": CodeSpec(
        "H002", Severity.ERROR, "operands have different dimensions"
    ),
    "H003": CodeSpec(
        "H003", Severity.ERROR, "intrinsic argument must be dimensionless"
    ),
    "H004": CodeSpec(
        "H004", Severity.ERROR, "function-call argument unit mismatch"
    ),
    "U002": CodeSpec(
        "U002", Severity.ERROR, "unit annotation could not be parsed"
    ),
    "U005": CodeSpec(
        "U005", Severity.WARNING,
        "variable used in a unit-checked expression has no annotation",
    ),
}


# ---------------------------------------------------------------------------
# Function / subroutine signatures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FuncSig:
    """A user-defined function or subroutine's unit interface.

    ``arg_names[i]`` and ``arg_units[i]`` describe the i-th formal
    argument; ``arg_units[i]`` is ``None`` when that argument has no
    unit annotation (the checker then doesn't constrain the actual).
    ``return_unit`` is ``None`` for subroutines and for functions whose
    return variable carries no annotation.
    """

    arg_names: tuple[str, ...]
    arg_units: tuple[Unit | None, ...]
    return_unit: Unit | None
    is_subroutine: bool = False


# ---------------------------------------------------------------------------
# Intrinsic categories (ported from V4)
# ---------------------------------------------------------------------------

# Require dimensionless input; produce dimensionless output.
DIMENSIONLESS_INTRINSICS: frozenset[str] = frozenset({
    "exp", "log", "log10",
    "sin", "cos", "tan",
    "asin", "acos", "atan",
    "sinh", "cosh", "tanh",
})

# Raise the argument's unit to a fixed exponent. Keys are intrinsic
# names; values are the exponent to apply.
TRANSFORMING_INTRINSICS: dict[str, Fraction] = {
    "sqrt": Fraction(1, 2),
    "abs": Fraction(1),
}

# Result has the first argument's unit; remaining args (if any) don't
# constrain it. Covers kind conversions and ``sign(a, b)``.
TRANSPARENT_INTRINSICS: frozenset[str] = frozenset({
    "floor", "ceiling", "nint", "int", "real", "dble", "sign",
    "aimag", "anint",
})

# All listed args must share a unit; result has that unit. For
# ``merge(tsource, fsource, mask)`` only the first two args are
# compared (the third is logical).
SAME_UNIT_ARG_INTRINSICS: frozenset[str] = frozenset({
    "min", "max", "mod", "modulo", "merge",
})

# Result = unit_of(arg[0]) * unit_of(arg[1]).
PRODUCT_INTRINSICS: frozenset[str] = frozenset({"dot_product", "matmul"})

# Reductions over an array; result has the array element's unit.
REDUCTION_INTRINSICS: frozenset[str] = frozenset({
    "sum", "minval", "maxval",
})


# ---------------------------------------------------------------------------
# Module exports + use-clause application
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModuleExports:
    """Public surface of one Fortran module, ready to splice into a
    consumer file's local scope via a ``use`` clause.

    Phase 2 treats every module-level declaration as exported (no
    ``private`` honouring yet) — refinement in a later phase.

    ``var_units`` lists only annotated variables. ``all_var_names``
    records every module-level variable declaration so the LSP can
    surface "this module declares X but it has no @unit{}" in hover
    summaries without re-walking the tree.
    """

    name: str
    var_units: dict[str, Unit]
    signatures: dict[str, FuncSig]
    all_var_names: tuple[str, ...] = ()


def apply_use_clauses(
    uses: tuple,
    module_exports: dict[str, ModuleExports],
    base_var_units: dict[str, Unit],
    base_signatures: dict[str, FuncSig],
    *,
    external_modules: frozenset[str] = frozenset(),
) -> tuple[dict[str, Unit], dict[str, FuncSig], frozenset[str]]:
    """Merge imported symbols into a file's scope.

    ``uses`` is the tuple of :class:`workspace_index.UseRef` produced
    by ``extract_uses``. Local declarations always win over imports
    (no shadow warning at this phase). Returns the merged
    ``(var_units, signatures)`` tables plus the set of module names
    referenced by ``use`` that we couldn't resolve — the caller can
    surface those as U007.

    ``external_modules`` is the allowlist of module names that live
    outside the workspace (intrinsic modules like ``iso_fortran_env``,
    libraries like ``netcdf``). Names in this set are silently
    skipped — no symbols are imported and no U007 is emitted.
    """
    var_units = dict(base_var_units)
    signatures = dict(base_signatures)
    unresolved: set[str] = set()
    for use in uses:
        mod_name = use.module.lower()
        if mod_name in external_modules:
            continue
        exports = module_exports.get(mod_name)
        if exports is None:
            unresolved.add(mod_name)
            continue

        # Build the in-scope (local_name, remote_name) pairs.
        if use.only is None:
            pairs = [(n, n) for n in exports.var_units]
            pairs.extend((n, n) for n in exports.signatures)
        else:
            # ``only`` already lower-cased; expand renames first, then
            # plain names. ``renames`` is the authoritative map for
            # any locally-renamed import.
            rename_map = {local: remote for local, remote in use.renames}
            pairs = []
            for local in use.only:
                remote = rename_map.get(local, local)
                pairs.append((local, remote))

        for local, remote in pairs:
            if local in base_var_units:
                continue  # local declaration wins
            # Variable lookup is case-sensitive against export keys;
            # try the lower-cased name too as a fallback because the
            # scanner reports names verbatim while ``use`` syntax is
            # case-insensitive in F90.
            if remote in exports.var_units:
                var_units.setdefault(local, exports.var_units[remote])
            else:
                for k, v in exports.var_units.items():
                    if k.lower() == remote:
                        var_units.setdefault(local, v)
                        break
            # Signatures are stored lower-cased; ``remote`` is already
            # lower from extract_uses.
            sig = exports.signatures.get(remote)
            if sig is not None:
                signatures.setdefault(local.lower(), sig)
    return var_units, signatures, frozenset(unresolved)


__all__ = [
    "CODES",
    "CodeSpec",
    "DIMENSIONLESS_INTRINSICS",
    "FuncSig",
    "ModuleExports",
    "PRODUCT_INTRINSICS",
    "REDUCTION_INTRINSICS",
    "SAME_UNIT_ARG_INTRINSICS",
    "TRANSFORMING_INTRINSICS",
    "TRANSPARENT_INTRINSICS",
    "apply_use_clauses",
]
