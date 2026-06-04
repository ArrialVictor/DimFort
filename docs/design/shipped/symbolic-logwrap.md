# Symbolic LogWrap multipliers — design notes

Status: **shipped** (merged to `main` 2026-05-22, alongside the
`symbolic-exponents` work). The R5.4 path now accepts symbolic
linear `Exponent` multipliers — see `_combine` in
`src/dimfort/core/units.py` (the docstring around the rule explicitly
references "γ · LOG(u) = LOG(u^γ)"). Closed three Tetens-family D1.4s
in a real-world Fortran codebase; the irreducible empirical-fit cases
remain pending and use the `@unit_assume` escape hatch.

This branch extended the symbolic-exponent machinery to one more
algebra path: the multiplier in `γ · LOG(p)` patterns. The design
below (problem statement + rules + tests) remains accurate as a
spec; the "step-by-step plan" near the end is historical (work
completed).


## Problem statement

DimFort's LogWrap arithmetic includes R5.4 — the log-power identity:

> `γ · LOG(p) = LOG(p^γ)` when `γ` is a literal rational.

Today, `γ` must be a literal rational at the call site. When `γ` is
a `REAL, SAVE` variable annotated dim'less but set at runtime (e.g.
`XGAMW = (XCL - XCPV) / XRV` in `modd_csts`'s Tetens curve), R5.4
fires the runtime-fallback rule **R5.5** which emits D1.4 ("scalar
multiplier of LogWrap is not a literal rational").

The remaining 4 D1.4s in the validation workspace after the
`symbolic-exponents` merge are all this shape:

```
modd_csts.f90:263       XALPW = LOG(XESTT) + (XBETAW/XTT) + (XGAMW * LOG(XTT))
modd_csts.f90:266       XALPI = LOG(XESTT) + (XBETAI/XTT) + (XGAMI * LOG(XTT))
qsat_seawater_mod.f90:102   ZFOES = 0.98*EXP(XALPW - XBETAW/PT - XGAMW * LOG(PT))
qsat_seawater2_mod.f90:85   ZFOES = EXP(24.4543 - 67.4509*(100/PT) - 4.8489*LOG(PT/100) - ...)
```

All four fire on the `γ · LOG(T)` or `γ · LOG(p)` shape where `γ` is
dim'less but not literal.


## Worked example

```fortran
REAL :: kappa                       !< @unit{1}
REAL :: p                           !< @unit{Pa}
REAL :: r                           !< @unit{LOG(Pa^kappa)}
r = kappa * LOG(p)
```

Under the `symbolic-exponents` merge:
- `LOG(p)` types as `LogWrap(Pa)`.
- `kappa * LogWrap(Pa)` — multiplier isn't a literal → R5.5 fires D1.4.

Under this branch:
- `kappa * LogWrap(Pa)` recognises `kappa` as a dim'less identifier
  resolvable to an Exponent (`Exponent.from_symbol("kappa")`).
- Applies R5.4-symbolic: `kappa · LogWrap(Pa) = LogWrap(Pa^kappa)`.
- `Pa^kappa` is a symbolic Unit, already supported (Step 2 of the
  previous branch).
- Result unit: `LogWrap(Pa^kappa)`, matches the annotation.

The Tetens cancellation (substituting `α = log(es(Tt)) + β/Tt + γ·log(Tt)`):

```
EXP(α - β/T - γ·LOG(T))
  = EXP(log(es(Tt)) + β·(1/Tt - 1/T) + γ·log(Tt) - γ·log(T))
  = es(Tt) · EXP(β·(1/Tt - 1/T)) · (Tt/T)^γ
```

The `(Tt/T)^γ` is symbolic, but `Tt/T` is dim'less (both K), so
`(Tt/T)^γ = dim'less` regardless of γ. The whole EXP types as Pa
(matching `es(Tt)`), which is exactly what `ZFOES` is annotated.


## What gets reused (no change)

- `Exponent` data type — already on main.
- `Unit.dimension` carries `Exponent` per slot — already on main.
- `_resolve_symbolic_exponent` — already on main, used by the `**`
  path. Will be reused here for the multiplier path.
- `_logwrap_inner_pow(inner, k)` — internally calls `inner.pow(k)`,
  which (since Step 2 of `symbolic-exponents`) accepts `Exponent`.
  No code change required.

The whole point of this branch is the *wiring*: pass symbolic
Exponents through `combine`'s `*_literal` parameters to the existing
R5.4 path, and let the Unit-level `Unit.pow(Exponent)` machinery do
its job.


## Algebra rules (formal)

### R5.4 — generalised

```
multiplier ∈ Number      :  γ · LogWrap(u) = LogWrap(u^γ)   (existing)
multiplier ∈ Exponent    :  γ · LogWrap(u) = LogWrap(u^γ)   (NEW)
```

The result's inner unit, `u^γ`, uses `Unit.pow(Exponent)` from the
previous branch. If `γ` is symbolic AND `u` already has symbolic
dimensions in some slot, `Unit.pow` raises `UnitError` (non-linear);
caller falls back to D1.4.

### R5.4 — division branch

```
LogWrap(u) / multiplier  with multiplier constant  :  LogWrap(u^(1/multiplier))
LogWrap(u) / multiplier  with multiplier symbolic  :  REFUSE → D1.4
```

A symbolic divisor `κ` would mean `u^(1/κ)`. The exponent `1/κ` is
NOT a linear form over rationals (it's a rational *function*), so
it doesn't fit our Exponent algebra. Explicit refusal at the
boundary.

### R6.4 — generalisation note

The ExpWrap branch (`EXP(k·u) = ExpWrap(k·u)`) also has a
`_logwrap_inner_pow` call site (line 440 in `units.py`). Same
extension applies — accept symbolic multipliers via `Unit.pow`.

### What does NOT change

- R5.1, R5.2 (LogWrap homomorphism, addition under LOG): no
  multipliers involved. Unchanged.
- R5.3, R5.6, R5.7, R5.9, R5.10, R6.1, R6.2, R6.5, etc.: no
  multipliers. Unchanged.


## Resolver wiring

In `ts_checker._resolve` at the math-expression dispatch (around
line 420), `combine` is called with `a_literal=` and `b_literal=`
derived from `_resolve_constant_value`. Today those return
`Number | None`. Change:

```python
# Before:
left_lit = _resolve_constant_value(left, ctx, source) if left is not None else None
right_lit = _resolve_constant_value(right, ctx, source) if right is not None else None

# After:
left_lit = _resolve_constant_value(left, ctx, source) if left is not None else None
if left_lit is None and left is not None:
    left_lit = _resolve_symbolic_exponent(left, ctx, source)
right_lit = _resolve_constant_value(right, ctx, source) if right is not None else None
if right_lit is None and right is not None:
    right_lit = _resolve_symbolic_exponent(right, ctx, source)
```

`combine`'s signature widens: `Number | Exponent | None`.


## Diagnostic interactions

| Today | After this branch |
|---|---|
| R5.5: `γ · LogWrap(u)` with non-literal γ → D1.4 | R5.4-symbolic: same expression with γ resolvable as a dim'less linear Exponent → LogWrap(u^γ), no D1.4 |
| D1.4 fires when γ is genuinely unknown (not annotated dim'less) | Unchanged — still D1.4 |
| D1.2 (undefined wrapper op) — unrelated | Unchanged |

LogWrap divided by a symbolic γ → D1.4 (explicit refusal, see
algebra section).


## Step-by-step implementation plan

### Step 1 — widen `combine`'s literal parameter type (~10 lines)

- Change signature: `a_literal: Number | Exponent | None`,
  `b_literal: Number | Exponent | None`.
- Update any internal logic that does `isinstance(x_literal, int)`
  or arithmetic on `x_literal` to also handle Exponent.
- No behavior change yet; this just makes the types correct.

### Step 2 — wire symbolic fallback at the resolver (~30 lines + tests)

- In `ts_checker._resolve` and `_walk_expressions`, after
  `_resolve_constant_value` returns None for an operand of `*` or
  `/`, fall through to `_resolve_symbolic_exponent`.
- Tests: `γ * LOG(p)` with `γ : 1` annotated dim'less → no D1.4,
  result `LOG(Pa^γ)`.

### Step 3 — refuse symbolic divisor on LogWrap (~5 lines + test)

- In `combine`'s LogWrap / Unit branch, when `b_literal` is a
  symbolic Exponent, return D1.4 (don't try `1/symbolic`).
- Test: `LOG(p) / γ` with symbolic γ → D1.4.

### Step 4 — Real-world verification

- Re-run on the validation workspace.
- Expected: all 4 Tetens-family D1.4s closed.
- Expected: no new false positives.

### Step 5 — extend R6.4 (ExpWrap) similarly if needed

- Check whether any real-world ExpWrap case fires D1.4 today and would
  benefit. If yes, apply the same widening to the ExpWrap path.
  If no, defer.

### Step 6 — update the internal findings log entry `#009`

- Mark the Tetens family as closed.
- Note that the fix shipped via this branch.


## Test plan

### Unit tests (`tests/unit/test_ts_checker.py`)

- `γ * LOG(p)` with `γ : 1` annotated → result LogWrap(Pa^γ), no diag.
- `2 * LOG(p)` (literal) — must still work via the existing R5.4
  path (regression guard).
- `LOG(p) / γ` with symbolic γ → D1.4.
- `LOG(p) / 2` (literal divisor) — still works via R5.4.

### Real-world regression

Each of the 4 Tetens sites in the validation workspace should produce
no diagnostic after this branch. No new findings anywhere.


## Failure modes

1. **Backward compat.** Changing `combine`'s signature might break
   callers outside `_resolve`. Search for all `combine(...)` calls;
   each must work with the wider type. Default `None` keeps the
   no-literal path intact.

2. **Recursive symbol blowup.** If `γ` is annotated `@unit{1}` and
   `δ` is also annotated `@unit{1}`, then `γ * δ * LOG(p)` would
   try to resolve `γ * δ` as Exponent — symbol×symbol is non-linear
   → returns None → falls back to D1.4. Honest refusal, no crash.

3. **R5.4 result type mismatch.** A LHS annotated `LOG(Pa^2)`
   compared against a result of `LOG(Pa^γ)` would correctly fire
   H001 because the inner Units have different exponent forms.
   This is honest behavior — we *should* flag it.


## Open decisions

- **Sign edge cases.** `−γ · LOG(p)` should produce `LogWrap(p^(−γ))`.
  The unary-minus handling already exists for literals; verify it
  applies to symbolic Exponents too. (Easy test.)
- **R6.4 ExpWrap extension.** Investigate whether the same pattern
  shows up in EXP-side multipliers. If so, generalise both
  branches symmetrically.
- **Symbolic-multiplier hover trace rendering.** `LOG(Pa^kappa)`
  rendered in a hover should be readable. `format_unit` on a
  LogWrap-inner symbolic Unit should already work via the
  symbolic-exponents Step 5 changes. Verify in VSCode after Step 4.


## Branch hygiene

- Each step its own commit. Push after Step 4 lands so it's visible.
- Don't merge until the validation-workspace verification (Step 4) is clean.
- If the algebra hits a corner case we didn't anticipate, kill the
  branch — main is unchanged.
