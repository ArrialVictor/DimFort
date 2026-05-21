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

Today:

```python
@dataclass(frozen=True)
class Unit:
    dimensions: dict[BaseSymbol, Fraction]
```

Proposed:

```python
# An exponent is no longer just a Fraction. It's a small linear form
# over the rationals with named opaque generators (the "symbols").
#
#     exponent = sum_i (q_i * x_i) + c
#
# where q_i ∈ Q, x_i are symbol names, c ∈ Q (the constant term).
#
# Examples:
#   2/7                 -> Exponent({}, 2/7)
#   kappa               -> Exponent({"kappa": 1}, 0)
#   1 - kappa           -> Exponent({"kappa": -1}, 1)
#   2*kappa + 3         -> Exponent({"kappa": 2}, 3)
#   kappa + lambda      -> Exponent({"kappa": 1, "lambda": 1}, 0)
#
# Two Exponents are equal iff their (terms, constant) coincide.
# Zero exponent is Exponent({}, 0); identity check is direct.

@dataclass(frozen=True)
class Exponent:
    terms: dict[str, Fraction]      # symbol_name → coefficient
    constant: Fraction

    def __add__(self, other): ...
    def __sub__(self, other): ...
    def __mul__(self, other): ...   # only by Fraction; symbol*symbol = NotImplemented
    def __neg__(self): ...
    def is_constant(self) -> bool:  # all terms zero
    def is_zero(self) -> bool:
    def is_one(self) -> bool:
```

Then:

```python
@dataclass(frozen=True)
class Unit:
    dimensions: dict[BaseSymbol, Exponent]
```

Backward compatibility: a literal-rational exponent is `Exponent({}, q)`.
All existing code paths that build / compare units with `Fraction`
exponents continue to work via implicit promotion at construction time.


## Algebraic rules

### combine(op, a, b)

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
