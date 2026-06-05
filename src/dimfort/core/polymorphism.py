"""AG-unification over the unit algebra (Kennedy 1996).

Given a list of ``(formal, actual)`` :class:`Unit` equations where the
formals may carry free tyvars (``'a``, ``'b``, ...) and the actuals
may carry tyvars too, find a substitution σ: tyvar → concrete Unit
such that ``σ(formal) = actual`` for every equation. The system
decomposes: per SI dimension, the unknowns ``σ('α)``'s
exponent-in-that-dim form one linear system over ℚ, solvable by
Gaussian elimination. The 7 SI dims are independent, so 7 small
systems are solved one at a time and the results stitched back into
one Unit per tyvar.

Per-slot net coefficients (``formal_tyvar - actual_tyvar``) make
self-recursive polymorphic calls work cleanly: when both sides
reference the same tyvar from the enclosing scope the net is zero and
the tyvar stays unbound. ``Substitution.apply`` leaves unbound tyvars
in place, so ``σ(formal) = formal = actual`` trivially.

Used by :mod:`dimfort.core.ts_checker` for the call-site dispatch
(H020) and indirectly for the body-level H023 detection.

Known limitations (currently raise :class:`UnsupportedPolymorphism` —
callers fall back to the concrete-check path):

- Formal tyvar exponents must reduce to literal rationals. Symbolic
  exponents on tyvars (``'a^κ``) are not supported.
- ``Unit.factor`` is not unified. Factor mismatches with tyvars in
  play are silently accepted.
- ``LogWrap`` / ``ExpWrap`` operands. Wrapper-typed polymorphism would
  require unification under the wrapper.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

from dimfort.core.units import (
    DIM_LEN,
    Exponent,
    ExpWrap,
    LogWrap,
    Unit,
    UnitExpr,
)

if TYPE_CHECKING:
    from dimfort.core.symbols import FuncSig


def free_tyvars_of_sig(sig: FuncSig) -> frozenset[str]:
    """Collect every tyvar name appearing in a function signature's
    arg + return units. Unwraps LogWrap / ExpWrap to reach the inner
    Unit. Shared by the checker (call-site dispatch + H023 detection)
    and the LSP (polymorphic-signature rendering).
    """
    out: set[str] = set()
    units_iter: list[UnitExpr | None] = list(sig.arg_units)
    units_iter.append(sig.return_unit)
    for u in units_iter:
        if u is None:
            continue
        inner = u
        while isinstance(inner, (LogWrap, ExpWrap)):
            inner = inner.inner
        if isinstance(inner, Unit):
            for name, _ in inner.tyvars:
                out.add(name)
    return frozenset(out)


class UnsupportedPolymorphism(Exception):
    """Raised when a signature uses a polymorphism shape the unifier
    does not yet handle (symbolic tyvar exponents, wrapper-typed slots,
    etc.). Callers should fall back to treating the call site as a
    concrete-mismatch / U005.
    """


# ---------------------------------------------------------------------------
# Public datatypes


@dataclass(frozen=True)
class SlotEquation:
    """One call-site equation ``formal_i ≡ actual_i``.

    ``slot_index`` and ``slot_name`` are carried for downstream
    diagnostic rendering — the checker uses them when building H020's
    symmetric "collides with arg N: name" trailer. The unifier itself
    only uses them to label conflict contributions.
    """
    slot_index: int
    slot_name: str | None
    formal: Unit
    actual: Unit


@dataclass(frozen=True)
class Contribution:
    """One slot's per-tyvar implied binding.

    Recorded for every slot that constrains a given tyvar. The
    checker aggregates contributions per tyvar across slots and, on
    conflict, lists every contributing slot as a collider in the H020
    message.
    """
    slot_index: int
    slot_name: str | None
    implied: Unit  # the slot's view of what σ('α) must be


@dataclass(frozen=True)
class Conflict:
    """Unification failed for a tyvar: at least two slots imply
    inconsistent values.
    """
    tyvar: str
    contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class Substitution:
    """σ: tyvar name → concrete Unit. Empty for callers with no tyvars."""
    bindings: dict[str, Unit]

    def apply(self, u: UnitExpr) -> UnitExpr:
        """Substitute σ into ``u``.

        For a :class:`Unit`, every tyvar entry whose name is in
        ``bindings`` contributes ``σ('α)^t_α`` to the product; the
        Unit's SI dim, factor, and offset are kept. Tyvars not in
        ``bindings`` stay as-is (used when partial substitutions chain
        in nested polymorphic calls). For :class:`LogWrap` /
        :class:`ExpWrap`, recurse into the operand and re-canonicalize
        the result via ``wrap_log`` / ``wrap_exp`` so substitutions that
        collapse the inner to dim'less (e.g. ``σ('a) = {1}``) take the
        R2.1 / R2.3 path and don't leave a stale ``LogWrap({1})`` /
        ``ExpWrap({1})`` behind.
        """
        if isinstance(u, Unit):
            return _apply_to_unit(u, self.bindings)
        if isinstance(u, LogWrap):
            # Route through ``wrap_log`` so the result honours R2.1 /
            # R2.3 canonicalization — substituting ``σ('a) = {1}`` into
            # ``LogWrap('a)`` should collapse to ``{1}``, not a stale
            # ``LogWrap({1})`` that would take wrong R5.x branches
            # downstream.
            from dimfort.core.units import wrap_log
            return wrap_log(self.apply(u.inner))
        if isinstance(u, ExpWrap):
            from dimfort.core.units import wrap_exp
            return wrap_exp(self.apply(u.inner))
        return u


@dataclass(frozen=True)
class UnificationResult:
    """Outcome of :func:`unify`.

    Exactly one of ``substitution`` (success) or ``conflicts``
    (failure) is non-empty. ``all_contributions`` is always populated:
    it records every slot's per-tyvar contribution regardless of
    success, so the LSP hover / CLI tree can render the per-slot
    ``'a = m`` annotations the spec calls for.
    """
    substitution: Substitution | None
    conflicts: tuple[Conflict, ...]
    all_contributions: dict[str, tuple[Contribution, ...]]

    @property
    def ok(self) -> bool:
        return self.substitution is not None


# ---------------------------------------------------------------------------
# Substitution application


def _apply_to_unit(u: Unit, bindings: dict[str, Unit]) -> Unit:
    if not u.tyvars:
        return u
    # Start from the SI-dim + factor + offset part, then multiply in
    # σ('α)^t_α for each bound tyvar; keep unbound tyvars in the
    # residual.
    result = Unit(u.dimension, u.factor, u.offset)
    residual: list[tuple[str, Exponent]] = []
    for name, exp in u.tyvars:
        if name not in bindings:
            residual.append((name, exp))
            continue
        bound = bindings[name]
        q = exp.as_fraction()
        if q is None:
            # Symbolic tyvar exponent (e.g. 'a^κ) — not yet supported;
            # surface so the caller can fall back.
            raise UnsupportedPolymorphism(
                f"symbolic exponent on tyvar {name!r} not supported"
            )
        result = result * bound.pow(q)
    if residual:
        result = Unit(
            result.dimension, result.factor, result.offset, tyvars=tuple(residual),
        )
    return result


# ---------------------------------------------------------------------------
# Unification


def unify(
    equations: list[SlotEquation], free_tyvars: tuple[str, ...],
) -> UnificationResult:
    """Solve the linear system for ``free_tyvars`` against ``equations``.

    Algorithm (M6 semantics):

    1. For each SI dim ``k`` (0..6):
       - Build matrix ``M[i][α] = t_{i,α}^k`` — the *net* per-slot
         coefficient of free tyvar ``α`` at dim ``k``, one column per
         free tyvar (not per opaque actual-side contributor).
       - Build RHS ``b[i] = (actual_i.dim[k] − formal_i.dim[k])`` —
         tyvar exponents that appear on both sides cancel into that
         net coefficient; whatever remains on the actual side lands in
         the RHS.
       - Gaussian-eliminate to a provisional ``s_α^k`` per tyvar.
    2. Stitch the per-dim solutions into a provisional ``Substitution``.
    3. Re-apply σ to every original equation and compare against the
       actual unit; any equation that doesn't match drives conflict
       reporting. Conflict detection happens here in the post-validation
       pass, **not** from vanishing-pivot rows during ``_solve``.

    Free tyvars that no slot's *net* coefficient touches stay
    **unbound** — they are absent from ``Substitution.bindings`` and
    :meth:`Substitution.apply` leaves them in place. This is what
    makes self-recursive polymorphic calls work cleanly: when both
    formal and actual reference the same tyvar the net is zero, the
    tyvar stays out of σ, and ``σ(formal) = formal = actual``
    trivially.

    Conflicts are reported per-tyvar: if any SI dim's system rejected
    the value for ``'α``, the per-slot Contribution records (collected
    upstream of elimination) name every slot that pushed an inconsistent
    value, ready for the checker to render symmetric
    ``(collides with arg N)`` trailers in H020.
    """
    # Early-out: nothing to solve.
    if not free_tyvars:
        return UnificationResult(
            substitution=Substitution(bindings={}),
            conflicts=(),
            all_contributions={},
        )

    # Reject wrapper / non-Unit operands up front. Polymorphism under
    # LogWrap / ExpWrap is not yet supported; the caller falls back to
    # the concrete-check path.
    for eq in equations:
        if not isinstance(eq.formal, Unit) or not isinstance(eq.actual, Unit):
            raise UnsupportedPolymorphism(
                f"wrapper-typed polymorphic slot at index "
                f"{eq.slot_index} is not yet supported"
            )

    # Collect per-tyvar per-slot contributions — for hover rendering
    # AND for conflict reporting. A slot contributes to tyvar α iff
    # α appears with non-zero exponent in eq.formal.tyvars.
    contributions: dict[str, list[Contribution]] = {
        name: [] for name in free_tyvars
    }
    for eq in equations:
        formal_tyvar_map = dict(eq.formal.tyvars)
        # For a slot whose formal is exactly ``'α`` (single tyvar,
        # exponent 1, no SI dims), the implied value of σ('α) is
        # directly the actual unit minus the formal's other tyvar
        # contributions. Compute the "implied" purely for diagnostics:
        # it's the per-slot reading of what σ('α) would need to be if
        # this slot were the only constraint and every other tyvar
        # vanished.
        for name in free_tyvars:
            t = formal_tyvar_map.get(name)
            if t is None:
                continue
            q = t.as_fraction()
            if q is None:
                raise UnsupportedPolymorphism(
                    f"symbolic exponent on formal tyvar {name!r} "
                    f"(slot {eq.slot_index}) not supported"
                )
            if q == 0:
                continue
            implied = _slot_implied_value(eq.formal, eq.actual, name, q)
            contributions[name].append(
                Contribution(eq.slot_index, eq.slot_name, implied),
            )

    # Per-slot net coefficient = formal_tyvar - actual_tyvar. This is
    # what makes self-recursive polymorphic calls work cleanly: when
    # both formal and actual reference the SAME tyvar (the caller is
    # inside the function being defined), the net contribution is zero
    # and the tyvar stays unbound — σ(formal) = formal = actual,
    # trivially satisfied.
    #
    # Side benefit: a tyvar that no slot mentions has net coefficient
    # zero everywhere, so it stays unbound. ``Substitution.apply``
    # leaves unbound tyvars in place, which is the right semantics
    # (no slot constrains them, so their downstream behaviour is
    # whatever the body says).
    net_per_slot: list[dict[str, Fraction]] = []
    for eq in equations:
        formal_map = dict(eq.formal.tyvars)
        actual_map = dict(eq.actual.tyvars)
        slot_net: dict[str, Fraction] = {}
        for name in free_tyvars:
            fc = formal_map.get(name)
            ac = actual_map.get(name)
            if fc is None and ac is None:
                continue
            if fc is None:
                net = -ac  # type: ignore[operator]
            elif ac is None:
                net = fc
            else:
                net = fc - ac
            nq = net.as_fraction()
            if nq is None:
                raise UnsupportedPolymorphism(
                    f"symbolic exponent on tyvar {name!r} "
                    f"(slot {eq.slot_index}) not supported"
                )
            if nq != 0:
                slot_net[name] = nq
        net_per_slot.append(slot_net)

    # The constrained set: every tyvar whose net coefficient is
    # non-zero in at least one slot. These are the unknowns of the
    # linear system; unconstrained tyvars stay out of σ entirely.
    constrained: list[str] = sorted(
        {n for slot_net in net_per_slot for n in slot_net}
    )

    bindings_dim: dict[str, list[Fraction]] = {
        name: [Fraction(0)] * DIM_LEN for name in constrained
    }
    for k in range(DIM_LEN):
        rows: list[list[Fraction]] = []
        rhs: list[Fraction] = []
        for slot_idx, eq in enumerate(equations):
            slot_net = net_per_slot[slot_idx]
            row = [slot_net.get(name, Fraction(0)) for name in constrained]
            af = eq.actual.dimension[k].as_fraction()
            ff = eq.formal.dimension[k].as_fraction()
            if af is None or ff is None:
                raise UnsupportedPolymorphism(
                    f"symbolic SI exponent at slot {eq.slot_index} "
                    f"(dim {k}) not supported under polymorphism"
                )
            rows.append(row)
            rhs.append(Fraction(af) - Fraction(ff))
        sol = _solve(rows, rhs)
        # Even when the underlying system is inconsistent _solve
        # returns its best partial assignment for the pivot variables;
        # the re-validation pass below catches the real conflicts by
        # re-applying σ to every original equation.
        for j, name in enumerate(constrained):
            bindings_dim[name][k] = sol[j]

    # Build the provisional substitution over the CONSTRAINED tyvars
    # only. Unconstrained tyvars stay absent from σ.bindings; the
    # post-validation step below uses Substitution.apply which leaves
    # unbound tyvars unchanged in the result.
    provisional: dict[str, Unit] = {
        name: Unit(
            tuple(Exponent.from_value(bindings_dim[name][k]) for k in range(DIM_LEN)),
            Fraction(1),
        )
        for name in constrained
    }
    sigma = Substitution(bindings=provisional)
    conflicting_tyvars: set[str] = set()
    for eq in equations:
        applied = sigma.apply(eq.formal)
        if not isinstance(applied, Unit):
            continue
        if (
            tuple(applied.dimension) != tuple(eq.actual.dimension)
            or applied.tyvars != eq.actual.tyvars
        ):
            for name, exp in eq.formal.tyvars:
                if name in provisional and not exp.is_zero():
                    conflicting_tyvars.add(name)

    if conflicting_tyvars:
        conflicts = tuple(
            Conflict(tyvar=name, contributions=tuple(contributions[name]))
            for name in free_tyvars
            if name in conflicting_tyvars
        )
        return UnificationResult(
            substitution=None,
            conflicts=conflicts,
            all_contributions={n: tuple(cs) for n, cs in contributions.items()},
        )

    return UnificationResult(
        substitution=sigma,
        conflicts=(),
        all_contributions={n: tuple(cs) for n, cs in contributions.items()},
    )


# ---------------------------------------------------------------------------
# Internal helpers


def _slot_implied_value(
    formal: Unit, actual: Unit, target: str, exponent: Fraction,
) -> Unit:
    """For diagnostic display: what would σ('α) have to be if this slot
    were the only constraint and every other tyvar in the formal
    vanished?

    Computes ``(actual / formal_concrete_part) ^ (1/exponent)`` where
    ``formal_concrete_part`` strips ``'α`` from the formal's tyvar map.
    This is purely for the hover's per-slot ``'a = m`` annotation; the
    real binding comes from the linear-system solve.
    """
    # Pull α out of the formal.
    remaining = tuple((n, e) for n, e in formal.tyvars if n != target)
    formal_minus_target = Unit(
        formal.dimension, formal.factor, formal.offset, tyvars=remaining,
    )
    quotient = actual / formal_minus_target
    # Now quotient should equal σ('α)^exponent — extract σ('α).
    if exponent == 1:
        return _strip_to_unit(quotient)
    try:
        root = quotient.pow(Fraction(1, 1) / exponent)
    except Exception:
        # Non-integer root on a scaled factor or similar; fall back to
        # quotient as-is — display only.
        root = quotient
    return _strip_to_unit(root)


def _strip_to_unit(u: Unit) -> Unit:
    """Render-time helper: zero offset, drop residual tyvars, keep
    SI dims + factor. Used for diagnostic implied-value display only.
    """
    return Unit(u.dimension, u.factor)


def _solve(
    rows: list[list[Fraction]], rhs: list[Fraction],
) -> list[Fraction]:
    """Gauss-Jordan elimination on a rational matrix.

    Returns one assignment to the unknowns (pivot variables ← their
    eliminated RHS; free variables pinned to ``0``). Inconsistent rows
    (all coefficients zero, non-zero RHS) are NOT detected here — the
    caller is responsible for post-validation by re-applying the
    candidate substitution to every equation. That re-validation
    catches Gauss row-permutation effects correctly; a parallel
    inside-``_solve`` detection would be redundant and error-prone.
    """
    if not rows:
        return []
    n_rows = len(rows)
    n_cols = len(rows[0])
    # Copy so we don't mutate the caller's matrix.
    m = [list(row) + [rhs[i]] for i, row in enumerate(rows)]
    pivot_row = 0
    pivot_col_of_row: dict[int, int] = {}
    for col in range(n_cols):
        # Find a pivot row from pivot_row onwards with non-zero entry in col.
        pivot = None
        for r in range(pivot_row, n_rows):
            if m[r][col] != 0:
                pivot = r
                break
        if pivot is None:
            continue
        if pivot != pivot_row:
            m[pivot_row], m[pivot] = m[pivot], m[pivot_row]
        # Normalize pivot row.
        pv = m[pivot_row][col]
        m[pivot_row] = [x / pv for x in m[pivot_row]]
        # Eliminate from every other row.
        for r in range(n_rows):
            if r == pivot_row:
                continue
            f = m[r][col]
            if f == 0:
                continue
            m[r] = [m[r][i] - f * m[pivot_row][i] for i in range(n_cols + 1)]
        pivot_col_of_row[pivot_row] = col
        pivot_row += 1

    # Read out one solution: pivot variables → their RHS; free vars → 0.
    solution: list[Fraction] = [Fraction(0)] * n_cols
    for r, col in pivot_col_of_row.items():
        solution[col] = m[r][n_cols]
    return solution


__all__ = [
    "Conflict",
    "Contribution",
    "SlotEquation",
    "Substitution",
    "UnificationResult",
    "UnsupportedPolymorphism",
    "unify",
]
