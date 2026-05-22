# Symbolic exponents — design notes for the `symbolic-exponents` branch

Status: **in design**, no implementation yet. Branch created 2026-05-22.

This document is for me (or any future maintainer) to pick up the work
from a cold start. It captures the *what*, the *why*, the data
structures, the algebraic rules, the step-by-step plan, and the
explicit open questions.

If anything in here turns out wrong during implementation, **update
this doc**, then write the code. The doc is the spec; code follows
the doc, not the other way around.


## Problem statement

The unit-algebra (`src/dimfort/core/units.py`) represents a unit as a
mapping from base-dimension symbols to **rational exponents**:

```python
Unit({"m": 1, "s": -2})    # m·s⁻² (acceleration)
Unit({"kg": 1, "m": -1, "s": -2})  # Pa
```

Power evaluation `base ** exponent` works only when `exponent` is a
literal rational (e.g. `p**2`, `p**(1/2)`). When the exponent is a
named variable, even one that's *statically constant* in the code
(e.g. `kappa = R/Cp` set once in `suphel.F`), the algebra has no way
to compute the resulting exponent and fires **D1.4** ("power exponent
is not a literal rational").

OQ4 (already on main) partially closes this for `REAL, PARAMETER`
declarations with literal initializers. It does **not** cover:

- `REAL` declared in one module, assigned at runtime in another
  (the Exner-kappa pattern in LMDZ-class codebases).
- Values that depend on namelists / config files / conditional
  branches at startup.
- Function-call returns into the would-be-constant slot.

The principled answer (the one mature unit systems — CamFort,
Mathematica Quantity, MATLAB Symbolic Units, Haskell/Rust
type-level numerics — converged on) is **symbolic opaque exponents**:
let the unit-algebra carry the *symbol* `kappa` instead of trying to
compute its rational value. Algebraic cancellation handles the rest.


## Worked example

```fortran
REAL :: pkappa, p1kappa, ratio
pkappa  = p_grid ** kappa
p1kappa = p_grid ** (1 - kappa)
ratio   = pkappa * p1kappa     ! should type to Pa^1 = Pa
```

Under the current (post-OQ4) algebra:
- `p_grid ** kappa` → D1.4 (kappa not a literal).
- Pipeline gives up; downstream typing missing.

Under symbolic exponents:
- `p_grid ** kappa` → `Pa^kappa` (opaque symbol).
- `p_grid ** (1 - kappa)` → `Pa^(1 - kappa)`.
- `Pa^kappa * Pa^(1 - kappa)` → `Pa^(kappa + 1 - kappa)` → `Pa^1` → `Pa`.
- `ratio = Pa` matches an `@unit{Pa}` annotation on `ratio`. ✓

The cancellation is **structural**: matching identifiers, linear
combinations adding to zero. No need to know `kappa = 2/7`.

A homogeneity violation surfaces honestly:

```fortran
ratio = p_grid ** kappa + q_grid ** lambda
```

→ `Pa^kappa + Pa^lambda` — different opaque symbols, cannot unify →
H002 with the message "operands carry different unknown exponents."

A mismatch surfaces too:

```fortran
ratio = p_grid ** kappa + p_grid
```

→ `Pa^kappa + Pa^1` — kappa is unknown, equal-to-1 is unverifiable →
H002 "the unknown exponent kappa is not provably 1."


## Data structure

Today (corrected from the original sketch in this doc — the real
`Unit` is a 7-slot tuple, not a dict):

```python
@dataclass(frozen=True)
class Unit:
    dimension: tuple[Number, Number, Number, Number, Number, Number, Number]
    factor: Fraction
# where Number = int | Fraction; tuple slots are M, L, T, Θ, I, N, J.
```

Proposed change (Step 2):

```python
@dataclass(frozen=True)
class Unit:
    dimension: tuple[Exponent, Exponent, Exponent, Exponent,
                     Exponent, Exponent, Exponent]
    factor: Fraction
```

The `Exponent` type is the linear form described in the "Algebra rules"
section above — committed in Step 1 (`41751aa`).

Backward compatibility:
- Existing call sites construct `Unit` with plain `Fraction`/`int`
  exponents. Step 2 keeps that interface working via implicit
  promotion at the constructor: anything not already an `Exponent`
  gets wrapped via `Exponent.from_value(...)` at `__post_init__`.
- `Unit.pow`, `Unit.__mul__`, `Unit.__truediv__` rewrite to use
  Exponent arithmetic. The pure-literal-Fraction case still flows
  through unchanged because Exponent({}, q) arithmetic agrees with
  Fraction arithmetic.


## Algebra rules (formal)

The algebra is defined over three layers: scalars (`Q`), exponents
(`E`, the linear forms above), and units (`U`, a 7-slot vector of
exponents plus a rational factor).

### Layer 1 — Exponent (E)

Exponent is a finite-dimensional vector space over Q, plus a constant:

  E = Q⟨X⟩ + Q   (linear forms in symbols X = {x₁, x₂, …} with a constant term)

| Operation | Domain | Codomain | Definition | Always defined? |
|---|---|---|---|---|
| `e + e'`  | E × E | E | per-symbol sum + constants sum | ✓ |
| `e - e'`  | E × E | E | per-symbol diff + constants diff | ✓ |
| `−e`      | E     | E | negate all coefficients + constant | ✓ |
| `q · e`   | Q × E | E | scale all coefficients + constant by q | ✓ |
| `e · e'`  | E × E | E | scalar multiplication when one side is in Q | partial — defined iff at least one side is pure-constant |

Canonical form: zero coefficients dropped, symbol names sorted. Two
canonical Es are equal iff their internal tuples are identical
(structural identity). Implemented Step 1; committed `41751aa`.

### Layer 2 — Unit (U)

A Unit is `(dim, factor)` where `dim ∈ E⁷` (one Exponent per SI base
slot M, L, T, Θ, I, N, J) and `factor ∈ Q`.

| Operation | Domain | Codomain | Definition | Always defined? |
|---|---|---|---|---|
| `u · u'`  | U × U | U | dim slots add component-wise (E + E); factors multiply | ✓ |
| `u / u'`  | U × U | U | dim slots subtract component-wise; factors divide | ✓ |
| `u^q`     | U × Q | U | dim slots multiply by q; factor stays (or `factor^q` if q is int) | ✓ |
| `u^e`     | U × E | U | dim slots multiply by e; factor stays (factor with symbolic exponent is unsupported) | partial — defined iff every dim slot is pure-constant **or** e is pure-constant (linear restriction inherited from E) |

Equality on U: equality on E⁷ componentwise, plus rational equality
on factors.

### Layer 3 — power(base, exponent_unit, exponent_value)

Generalizes the existing power rule from "exponent_value is a literal
rational" to "exponent_value is any Exponent."

```
power(base: U | LogWrap | ExpWrap,
      exponent_unit: U | None,
      exponent_value: E | None) -> (result | None, diagnostic | None)
```

Gates:

1. **D1.7** — exponent_unit must be dim'less (unchanged from current
   behaviour). Fires before the new symbolic path.
2. **Base-specific dispatch** — unchanged dispatch table (Rd / Rn /
   Ln / En), refined per cell:

   | Base | exponent_value | Result |
   |---|---|---|
   | Rd (dim'less) | any         | Rd (R4.3, the "0·k = 0" cell) |
   | Rn            | constant E  | Rn with dim slots × constant (existing path) |
   | Rn            | symbolic E, linear-compatible (per Layer 2) | Rn with dim slots × E (NEW) |
   | Rn            | symbolic E, would produce nonlinear | D1.4 |
   | Rn            | None        | D1.4 |
   | Ln            | E = 1       | Ln (R5.9) |
   | Ln            | E ≠ 1 const | D1.2 |
   | Ln            | symbolic E  | D1.2 (LogWrap algebra not extended in Step 2; stretch goal) |
   | En            | constant E  | ExpWrap with k·U (R6.4) |
   | En            | symbolic E  | D1.4 (same stretch consideration) |
   | En            | None        | D1.4 |

### Layer 4 — combine(op, a, b)

Per-operator generalisation:

| op | Rule on unit operands |
|---|---|
| `*` | result.dim = a.dim + b.dim (per slot E+E); result.factor = a×b factors |
| `/` | result.dim = a.dim − b.dim; result.factor = a÷b factors |
| `+`, `-` | requires `a.dim == b.dim` per E-equality on each slot AND factor equality. Diagnostic when not equal: H001/H002 with the operand-difference detail. |

New diagnostic case: if both operands carry symbolic E in some slot
but the symbols don't unify (e.g. `Pa^kappa + Pa^lambda`), this is
still an H002, but the message should be tailored:

> "operands carry distinct opaque exponents on `Pa` (`kappa` vs `lambda`); unverifiable without value lookup"

If one operand has a symbolic E and the other a constant E in the
same slot (e.g. `Pa^kappa + Pa`), the message:

> "operand carries opaque exponent `kappa` on `Pa`; cannot prove kappa = 1 (required for homogeneity)"

### Failure-mode summary

The algebra never silently propagates wrong answers. Every operation
either:
- Produces a correct unit (possibly symbolic), or
- Raises / returns None / emits a diagnostic.

The "linear restriction" on `e · e'` and `u^e` is the only place where
the algebra refuses to compute. In every observed case (Exner,
Tetens) the input never violates linearity — the refusal exists as
a guard against pathological inputs that would otherwise destroy
the algebra's decidability.

## combine(op, a, b)

The existing rules generalize directly:

| op | Rule |
|---|---|
| `*` | exponents add per base symbol (Exponent.__add__) |
| `/` | exponents subtract |
| `+` / `-` | requires structural equality of all Exponents |

Structural equality on Exponents: same `terms` dict (same symbols,
same Fraction coefficients) AND same `constant`. This is decidable in
O(symbols), no normalization needed.

### power(base, exponent_unit, exponent_value)

Today: `exponent_value` must be a literal `Fraction | int`. If it is,
multiply every dimension's exponent by it; if not, fire D1.4.

New rule: `exponent_value` may be any of:
- `Fraction | int` (literal — multiplication is component-wise).
- A named opaque symbol `name` (becomes a new Exponent term).
- An Exponent (already a linear form) — distributes through.

The "scalar multiplier on an Exponent" operation: `(q*x + c) * E` where
`E` is the base's exponent and `(qx+c)` is the scalar from the
`**`. This is *not* a linear operation in general: `kappa * lambda`
isn't in our Exponent type (it would need products of symbols, which
we *don't* support to keep the algebra linear).

→ **Rule**: `power(base, exp_unit, exp_value)` works iff
`exp_value` is "scalar-like" in the sense that either:
- It's a pure constant `c` (the existing literal case), or
- It's a single symbol with coefficient 1 (the `p**kappa` case), or
- It's a linear form WITHOUT pre-existing symbol terms in the base.

If the base already has symbol terms (it's already a `p**kappa`-like
unit) AND the new exponent also has symbol terms, the multiplication
produces symbol products → D1.4 (genuine algebraic limitation — we
explicitly chose to keep the algebra linear).

This limitation is *acceptable* because in practice nobody writes
`(p**kappa)**lambda` (it would be a tower of unknowns). The Exner /
Tetens patterns are all `(constant_or_linear)^(constant_or_linear)`
with at most one symbol in play at a time.

### equal_dim(a, b)

Structural equality: same dimensions dict, same Exponents per entry.
Already implied by the dataclass equality. The only subtlety is
constant-folding: `Exponent({}, 2)` and `Exponent({"x": 0}, 2)` should
be considered equal — we either canonicalize (drop zero terms at
construction time) or normalize on comparison. **Decision: canonicalize
at construction.** Construct via a smart constructor that drops zero
terms.


## Resolver wiring

In `ts_checker._resolve` for `**`:

```python
if op == "**":
    base = _resolve(left, ctx, source)
    if base is None or right is None:
        return None
    exponent_value = _resolve_constant_value(right, ctx, source)
    exponent_unit  = _resolve(right, ctx, source)

    if exponent_value is None:
        # Today: D1.4 fires. New behavior: try to extract a symbolic
        # exponent from the right-hand side.
        symbolic_exp = _resolve_symbolic_exponent(right, ctx, source)
        if symbolic_exp is not None:
            result, _ = power(base, exponent_unit, symbolic_exp)
            return result
        # else fall through to old D1.4 path

    result, _ = power(base, exponent_unit, exponent_value)
    return result
```

`_resolve_symbolic_exponent(right, ctx, source) -> Exponent | None`:

- Identifier whose annotated unit is dim'less (`@unit{1}`) → `Exponent({name: 1}, 0)`.
- Identifier annotated with non-dim'less unit → `None` (would already fire D1.2/D1.3).
- Unary `-expr` → negate the recursive result.
- Math expression `a +/- b` with both sides symbolic → component-wise.
- Math expression `a * b` with one side a literal constant and the other symbolic → scale.
- Anything else → `None` (fall through to D1.4 as before).


## Diagnostic interactions

| Old diagnostic | New behavior |
|---|---|
| D1.4 (exponent not literal rational) | Fires only when even symbolic resolution fails. Most LMDZ kappa cases stop firing. |
| H001 / H002 unit mismatch | Now also fires when symbolic exponents in operands don't structurally unify. New message: "operands carry inequivalent opaque exponents: `kappa` vs `lambda`". |
| H001 with constant-1 unverifiability | New diagnostic class: "operand carries unknown exponent `kappa`; cannot verify it equals 1 (required for homogeneity with the other operand)." |

Trace tree (for hover):
- A unit `Pa^kappa` renders as `Pa^kappa` in the trace.
- A node carrying `Pa^(1-kappa)` renders as `Pa^(1 - kappa)`.
- A node with a multi-symbol Exponent renders the full sum.


## Step-by-step implementation plan

Each step independently testable, independently committable.

### Step 1 — `Exponent` data structure (~50 lines + ~80 lines tests)

- Add the `Exponent` dataclass in `units.py`.
- Smart constructor that drops zero coefficients.
- Arithmetic operations: `__add__`, `__sub__`, `__neg__`, scalar `__mul__`.
- `is_zero`, `is_one`, `is_constant` queries.
- Backward-compat: helper `as_exponent(Fraction | int) -> Exponent` and
  `Exponent.as_fraction(self) -> Fraction | None`.
- Tests: arithmetic, equality, canonical form, scalar multiplication.

**No behavior change at this step.** The `Unit` class still uses
`Fraction` exponents. We're just building the new type next to it.

### Step 2 — `Unit` carries `Exponent` (~150 lines + tests)

- Change `Unit.dimensions` value type to `Exponent`.
- Update every constructor / parser / formatter to read/write
  `Exponent` values.
- Promote: when callers pass `Fraction`, wrap as `Exponent({}, q)`.
- `combine`, `power`, `equal_dim` updated to use Exponent arithmetic.
- `format_unit` learns to print `Pa^kappa` style.
- Tests: every existing units.py test should still pass (operating
  through the promotion). Add new tests for symbolic units.

**No new resolver behavior yet — still no symbolic exponents in
practice, but the type system is ready.**

### Step 3 — Symbolic exponent resolution in `_resolve` (~80 lines + tests)

- Add `_resolve_symbolic_exponent(node, ctx, source) -> Exponent | None`
  in `ts_checker.py`.
- Wire it into the `**` path: when literal-rational resolution fails,
  try symbolic. If symbolic succeeds, use it.
- New diagnostic message texts when H001/H002 fire on inequivalent
  opaque exponents.
- Tests: `p**kappa * p**(1-kappa) → Pa`; `p**kappa + p**lambda → H002`;
  `p**kappa + p → H002 with new message`.

### Step 4 — LMDZ verification

- Run the reference workspace trial.
- Expected: 3 Exner D1.4s gone; no new false positives.
- Expected: maybe 2-3 NEW H002s in code that was previously hidden
  behind a D1.4 silence. These are honest findings — investigate each.
- Update LMDZ_FINDINGS.md (off-branch — the log lives outside DimFort).

### Step 5 — Trace tree rendering (~20 lines)

- Hover trace renders `Pa^kappa` legibly.
- Verify in VSCode.

### Step 6 (stretch) — Cancellation simplifier

Structural identity may not be enough for cases like:

```fortran
r = p ** kappa
r = r * p ** (-kappa)        ! does Pa^kappa * Pa^(-kappa) = Pa^0 = 1?
```

Step 2's Exponent arithmetic already handles `kappa + (-kappa) = 0`
via the smart constructor. So this works out of the box. The "stretch"
is only needed if we hit cases where simplification needs to be
deeper — currently I don't expect any.


## Test plan

### Unit tests (`tests/unit/test_units.py`)

- `Exponent` arithmetic, equality, canonical form.
- `Unit` with symbolic exponents: equality, combine, power.
- `format_unit` round-trip for symbolic units.

### Integration tests (`tests/unit/test_ts_checker.py`)

- Single-symbol cases: `p**kappa` types cleanly; `p**kappa * p**(-kappa) = Pa`.
- Multi-symbol cases: `p**kappa * q**lambda` is a non-unifiable cross.
- Operator interactions: `+` requires structural identity, `*` adds
  exponents, `/` subtracts.
- Falls back to D1.4 only when symbolic resolution itself fails
  (e.g. `p ** sin(kappa)`).

### LMDZ regression

- The three Exner D1.4 sites convert to clean typings.
- The Tetens family (#009) — same pattern but in LogWrap algebra; may
  or may not be covered by this work depending on whether LogWrap
  multipliers go through the same scalar path. **TBD.**
- No new false positives anywhere in the trial workspace baseline.


## Failure modes to plan for

1. **Symbol explosion.** If every dim'less variable becomes a fresh
   opaque symbol, the algebra carries hundreds of symbols at file
   scope. Mitigation: only emit a symbol when the variable actually
   appears as an exponent. Don't pre-allocate.

2. **Symbol identity across files.** Two files both use `kappa` (same
   name, same source declaration via `use`). Their symbols must
   compare equal. Current design: symbol identity is by *name string*.
   Same-named variables from different scopes would alias — usually
   that's correct (it IS the same `kappa` via `use`), but could mask
   a shadowing case. Mitigation: scope qualifier in the symbol name
   (`comconst_mod::kappa`). Decide later, when first ambiguity surfaces.

3. **Performance.** Exponent arithmetic is O(symbols-in-form), bounded
   small. No expected blowup. Profile after Step 2.

4. **Diagnostic readability.** `Pa^(2*kappa - 1)` in a hover may be
   verbose. Mitigation: simplify the printed form (combine like
   terms, drop zero coefficients). Already a property of the smart
   constructor.


## Open decisions (to settle during implementation)

- **Symbol naming with scope.** Bare name vs `scope::name`? Default
  to bare unless we hit the shadowing case (1) above.
- **Should `**` with symbolic exponent on a non-dim'less symbol
  fire a separate diagnostic?** E.g. `p ** mass` where `mass` has
  unit `kg` — that's already D1.7 (exponent must be dim'less). No
  change needed.
- **`LOG(p) * kappa` (LogWrap × scalar).** The current LogWrap algebra
  fires D1.4 if `kappa` is not a literal. The symbolic path could
  produce `LogWrap(Pa^kappa)` if we generalize the wrapper similarly.
  TBD — start with `**` only, revisit LogWrap once the basics work.


## Migration / rollback story

- The branch lives independently of main.
- If the design works end-to-end (Step 4 passes), it merges to main
  as a single squash or as the staged Step 1–6 commits.
- If at any step we find a fundamental issue (unforeseen algebraic
  case, performance problem, diagnostic ugliness), we close the
  branch and main is unchanged.
- Main's behavior at the time of branching (post-OQ4) is the
  fallback ground truth.

## Branch hygiene

- Commit each step as its own commit on the branch.
- After Step 4, push the branch to origin so it's visible.
- Don't merge until Steps 1–4 are demonstrably stable.
