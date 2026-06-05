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
from typing import Any

from dimfort.core.diagnostics import Severity
from dimfort.core.units import UnitExpr

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
    "U020": CodeSpec(
        "U020", Severity.INFO,
        "RHS unit assumed via @unit_assume (derivation suppressed)",
    ),
    # P-codes: parse-state findings. P001 marks a region tree-sitter could
    # not parse — DimFort makes no unit guarantee there. INFO (blue squiggle);
    # see docs/design/unparsed-regions.md.
    "P001": CodeSpec(
        "P001", Severity.INFO,
        "region could not be parsed — no unit guarantee here",
    ),
    # X-codes: cross-site (whole-symbol) findings produced on demand by the
    # ``interactions`` query, not by the per-statement ``check`` pass.
    "X001": CodeSpec(
        "X001", Severity.ERROR,
        "conflicting unit claims across a symbol's use-sites",
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
    # Units may be wrapped (LOG/EXP), e.g. a function annotated
    # ``@unit{LOG(Pa)}`` — hence UnitExpr, not just Unit.
    arg_units: tuple[UnitExpr | None, ...]
    return_unit: UnitExpr | None
    is_subroutine: bool = False


# ---------------------------------------------------------------------------
# Intrinsic categories (ported from V4)
# ---------------------------------------------------------------------------

# Require dimensionless input; produce dimensionless output.
# Note: ``exp`` / ``log`` / ``log10`` were here before Phase B but moved
# out — they now type via the unit-algebra wrapper rules (R3.1, R3.2)
# rather than requiring a dim'less argument.
DIMENSIONLESS_INTRINSICS: frozenset[str] = frozenset({
    "sin", "cos", "tan",
    "asin", "acos", "atan",
    "sinh", "cosh", "tanh",
})

# LOG / LOG10 / LOG2 — produce ``LogWrap(arg-unit)`` per R3.1 / R3.3.
LOG_INTRINSICS: frozenset[str] = frozenset({"log", "log10", "log2"})

# EXP — produces ``ExpWrap(arg-unit)`` per R3.2.
EXP_INTRINSICS: frozenset[str] = frozenset({"exp"})

# Raise the argument's unit to a fixed exponent. Keys are intrinsic
# names; values are the exponent to apply.
TRANSFORMING_INTRINSICS: dict[str, Fraction] = {
    "sqrt": Fraction(1, 2),
}

# Result has the first argument's unit; remaining args (if any) don't
# constrain it. Covers kind conversions, ``sign(a, b)``, and ``abs``
# (which preserves the unit unconditionally — including under LogWrap /
# ExpWrap, where the TRANSFORMING-via-``pow(1)`` path would have
# rejected the wrapper).
TRANSPARENT_INTRINSICS: frozenset[str] = frozenset({
    "abs",
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

    ``var_units`` lists only annotated variables. ``all_var_names``
    records every module-level variable declaration so the LSP can
    surface "this module declares X but it has no @unit{}" in hover
    summaries without re-walking the tree.

    ``inner_uses`` are the ``use`` clauses *inside* the module body —
    needed to compute the transitive re-export closure for the panel's
    Imports section (see :func:`compute_transitive_exports`). Typed as
    ``tuple[Any, ...]`` to avoid an import cycle with
    ``workspace_index.UseRef``; each element duck-types to that shape
    (``.module``, ``.only``, ``.renames``).

    Visibility (Fortran's PUBLIC / PRIVATE access control):
    ``default_private`` is ``True`` when the module body carries a bare
    ``private`` statement (default flips to private). ``public_names``
    and ``private_names`` are the lower-cased symbols named in
    per-symbol ``public :: …`` / ``private :: …`` overrides.
    """

    name: str
    var_units: dict[str, UnitExpr]
    signatures: dict[str, FuncSig]
    all_var_names: tuple[str, ...] = ()
    inner_uses: tuple[Any, ...] = ()
    default_private: bool = False
    public_names: frozenset[str] = frozenset()
    private_names: frozenset[str] = frozenset()


def deps_consumed_from_uses(
    uses: tuple[Any, ...],
    unresolved: frozenset[str],
    external_modules: frozenset[str],
) -> frozenset[str]:
    """Return the set of workspace modules a file depends on for caching.

    Per-module dep granularity for the content-hash cache: a file's
    cached entry is dirty when any module in this set has its exports
    *or its resolution state* changed.

    **Unresolved modules ARE included.** A module that's unresolved
    today (its file not yet added, or temporarily un-indexable) may
    resolve tomorrow, and that transition can introduce or remove a
    diagnostic in this file (e.g. a ``use`` import that suddenly gives
    a previously-unknown variable a unit, turning ``y = x`` into an
    H001). The module's export digest is the sentinel ``"absent"``
    while unresolved; when it resolves the digest changes and the
    dependent file's cache invalidates. Excluding unresolved modules
    (the old behaviour) left a stale-cache hole — a newly-applicable
    diagnostic was silently dropped on the next warm run.

    Only ``external_modules`` are excluded: they live outside the
    workspace by definition and never resolve into it, so their state
    can't change a file's diagnostics.
    """
    return frozenset(
        use.module.lower() for use in uses
    ) - external_modules


def apply_use_clauses(
    uses: tuple[Any, ...],
    module_exports: dict[str, ModuleExports],
    base_var_units: dict[str, UnitExpr],
    base_signatures: dict[str, FuncSig],
    *,
    external_modules: frozenset[str] = frozenset(),
) -> tuple[dict[str, UnitExpr], dict[str, FuncSig], frozenset[str]]:
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

        # ``base_var_units`` is built from declarations scanned in
        # source case (Fortran convention). The local-wins check must be
        # case-insensitive to match Fortran's own resolution rules;
        # otherwise an imported ``foo`` from another module would
        # shadow a local ``Foo`` even though Fortran considers them
        # the same name. Build a lower-case mirror once per use clause.
        base_var_units_lc = {n.lower() for n in base_var_units}
        for local, remote in pairs:
            if local.lower() in base_var_units_lc:
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


# ---------------------------------------------------------------------------
# Transitive re-export closure
# ---------------------------------------------------------------------------


def compute_transitive_exports(
    module_exports: dict[str, ModuleExports],
) -> tuple[
    dict[str, dict[str, tuple[UnitExpr | None, str]]],
    dict[str, dict[str, tuple[FuncSig, str]]],
]:
    """Resolve each module's transitive re-export surface.

    Returns ``(vars_by_module, sigs_by_module)`` where:

    - ``vars_by_module[mod_lc][name_lc]`` is ``(unit_or_None,
      origin_module_lc)`` — the unit annotation (``None`` when the
      original declaration carried no ``@unit{}``) and the module that
      *originally* declared the symbol. A consumer of ``mod_lc`` sees
      this map (filtered by their own ``only:`` / renames) as their
      import surface.
    - ``sigs_by_module[mod_lc][name_lc]`` is ``(FuncSig, origin_lc)``.

    Rules honoured (Fortran 2008 §11.2):

    1. **Default visibility is PUBLIC** — a module re-exports everything
       it imports, unless a bare ``private`` flips the default.
    2. ``use foo, only: …`` along the chain narrows what's re-exported.
    3. ``use foo, local => remote`` renames carry through to consumers.
    4. ``public :: name`` re-opens a single name after a bare
       ``private``; ``private :: name`` shuts a single name.
    5. **Cycle-safe** via an in-progress set — modules in a cycle see
       only their own locally-declared symbols on the back-edge (the
       forward edge fills in the rest on first visit).

    Memoised: each module is resolved once. The work is O(modules ×
    inner-uses × symbols-per-module), independent of the depth of the
    use chain.
    """
    vars_out: dict[str, dict[str, tuple[UnitExpr | None, str]]] = {}
    sigs_out: dict[str, dict[str, tuple[FuncSig, str]]] = {}
    in_progress: set[str] = set()

    def visit(mod_lc: str) -> tuple[
        dict[str, tuple[UnitExpr | None, str]],
        dict[str, tuple[FuncSig, str]],
    ]:
        if mod_lc in vars_out:
            return vars_out[mod_lc], sigs_out[mod_lc]
        if mod_lc in in_progress:
            return {}, {}  # cycle break
        exports = module_exports.get(mod_lc)
        if exports is None:
            vars_out[mod_lc] = {}
            sigs_out[mod_lc] = {}
            return vars_out[mod_lc], sigs_out[mod_lc]
        in_progress.add(mod_lc)

        v: dict[str, tuple[UnitExpr | None, str]] = {}
        s: dict[str, tuple[FuncSig, str]] = {}

        # Local declarations first — they win over transitively-imported
        # entries on a name clash.
        annotated_lc = {k.lower(): u for k, u in exports.var_units.items()}
        for vn in exports.all_var_names:
            nm = vn.lower()
            v[nm] = (annotated_lc.get(nm), mod_lc)
        # Defensive: include any annotated names not listed in
        # ``all_var_names`` (shouldn't happen with the current collector,
        # but keeps the two maps coherent).
        for nm_orig, u in exports.var_units.items():
            nm = nm_orig.lower()
            if nm not in v:
                v[nm] = (u, mod_lc)
        for nm_orig, sig in exports.signatures.items():
            s[nm_orig.lower()] = (sig, mod_lc)

        # Pull in each transitive ``use``, filtered by its own
        # ``only:`` / renames.
        for use in exports.inner_uses:
            tgt_lc = str(use.module).lower()
            tgt_v, tgt_s = visit(tgt_lc)
            only = getattr(use, "only", None)
            renames = getattr(use, "renames", ())
            if only is None:
                for nm, v_entry in tgt_v.items():
                    v.setdefault(nm, v_entry)
                for nm, s_entry in tgt_s.items():
                    s.setdefault(nm, s_entry)
            else:
                rename_map = {
                    str(local).lower(): str(remote).lower()
                    for local, remote in renames
                }
                for local in only:
                    local_lc = str(local).lower()
                    remote_lc = rename_map.get(local_lc, local_lc)
                    if remote_lc in tgt_v:
                        v.setdefault(local_lc, tgt_v[remote_lc])
                    if remote_lc in tgt_s:
                        s.setdefault(local_lc, tgt_s[remote_lc])

        # Visibility filter — gate what THIS module re-exports.
        priv = exports.private_names
        pub = exports.public_names
        default_private = exports.default_private

        def visible(name_lc: str) -> bool:
            if name_lc in priv:
                return False
            if name_lc in pub:
                return True
            return not default_private

        v = {n: val for n, val in v.items() if visible(n)}
        s = {n: val for n, val in s.items() if visible(n)}

        vars_out[mod_lc] = v
        sigs_out[mod_lc] = s
        in_progress.discard(mod_lc)
        return v, s

    for mod_lc in module_exports:
        visit(mod_lc)
    return vars_out, sigs_out


__all__ = [
    "CODES",
    "CodeSpec",
    "DIMENSIONLESS_INTRINSICS",
    "EXP_INTRINSICS",
    "LOG_INTRINSICS",
    "FuncSig",
    "ModuleExports",
    "PRODUCT_INTRINSICS",
    "REDUCTION_INTRINSICS",
    "SAME_UNIT_ARG_INTRINSICS",
    "TRANSFORMING_INTRINSICS",
    "TRANSPARENT_INTRINSICS",
    "apply_use_clauses",
    "compute_transitive_exports",
    "deps_consumed_from_uses",
]
