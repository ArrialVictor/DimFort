# Unit algebra — specification

> **Status**: implemented as of 2026-05-20 (Phase B / C / D landed
> across commits `b4ae113..62993f8`). The runtime now applies every
> rule below; this document is the canonical reference for the rule
> IDs, the operation tables in §14, and the diagnostic codes.
>
> Captures the rules agreed in the 2026-05-20 design session.
> Rules are numbered (`R<section>.<rule>`) so they can be referenced
> from code comments, diagnostics, and future design discussions.
> Diagnostic classes use the prefix `D`. Trace primitives use `T`.
>
> Every rule has at least one worked example. When a diagnostic
> message references a rule (e.g. `[R5.3]`), the example here is the
> canonical reference.
>
> Each rule is labelled with its status:
> - **AXIOM** — fundamental; not derivable from other rules. Implementing
>   the axioms is sufficient to realise the whole system.
> - **DERIVED** — follows from one or more axioms. Listed for reference
>   so users know the rule holds, but it doesn't need to be coded
>   independently — it falls out of the axiom rules.
> - **META** — design choice, clarification, or absence-of-rule
>   statement (not a rule in the operational sense).
> - **DEF** — definitions (representation, diagnostic codes, etc.).
>
> **Axioms** (the minimal set, 22 rules):
> R2.1, R2.2, **R2.3**, R3.1, R3.2, R4.1, R4.2, R4.3, R5.1, R5.3, R5.4,
> R5.6, R5.7, R5.9, R5.10, R6.1, R6.3, R6.4, R6.5, R6.6, R6.7, R7.1.
>
> **Derived rules** (6 rules, follow from axioms):
> R3.3, R5.2, R5.5, R5.8, R6.2, R7.2.
>
> **Removed rules**: R7.3 (superseded by R2.3 eager collapse).
>
> **Diagnostic classes**: D1.1, D1.2, D1.3, D1.4, D1.5, D1.6, D1.7.
> §15 documents the per-rule severity-override mechanism for project
> policy.

---

## 1. Representation

### R1.1 — Base unit tuple [DEF]

A *Regular unit* is a 7-tuple of rational exponents over the SI base
units:

```
Regular(m, s, kg, K, mol, A, cd)
```

**Example:**
```
Pa         = Regular(-1, -2,  1,  0, 0, 0, 0)    # kg/(m·s²)
m/s        = Regular( 1, -1,  0,  0, 0, 0, 0)
m²/s²      = Regular( 2, -2,  0,  0, 0, 0, 0)    # geopotential, energy/mass
J/(kg·K)   = Regular( 2, -2,  0, -1, 0, 0, 0)    # specific gas constant
kg/s       = Regular( 0, -1,  1,  0, 0, 0, 0)    # mass flux
1          = Regular( 0,  0,  0,  0, 0, 0, 0)    # dim'less
```

### R1.2 — Wrapper types [DEF]

A *Unit* is one of:

```
Unit ::= Regular(tuple)
       | LogWrap(Unit)
       | ExpWrap(Unit)
```

Units form a tree: leaves are 7-tuples; internal nodes are
`LogWrap` or `ExpWrap` markers. Depth is unbounded.

**Example:**
```
Pa                   = Regular(...)                          # leaf
LOG(Pa)              = LogWrap(Regular(...))                 # depth-1
LOG(LOG(Pa))         = LogWrap(LogWrap(Regular(...)))        # depth-2
EXP(K)               = ExpWrap(Regular(K))
EXP(EXP(K))          = ExpWrap(ExpWrap(Regular(K)))
```

### R1.3 — Equality [DEF]

Two units are equal iff their tree structures are identical after
canonicalization (§2).

**Example:**
```
parse("EXP(LOG(Pa))")   ≡   parse("Pa")     # both canonicalize to Regular(Pa)
parse("LOG(EXP(K))")    ≡   parse("K")
LogWrap(Regular(Pa))    ≠   Regular(Pa)     # different tree structures
```

---

## 2. Canonicalization on construction

Reductions are applied **eagerly** when a unit is constructed, so
equal units always have identical representations.

**Order of evaluation**: types propagate bottom-up through the AST,
following the operator precedence of the source language. Each
sub-expression's type is constructed (with R2.x canonicalization
applied) before any outer operation rule sees it. Concretely, the
arithmetic rules in §4–§7 always operate on **canonical** operands:
no LogWrap-of-ExpWrap (or vice versa) ever reaches an `×`/`÷`/`+`/`-`
rule, because R2.1/R2.2/R2.3 would have already fired at construction.

### R2.1 — LOG ∘ EXP cancellation [AXIOM]

```
LogWrap(ExpWrap(U))  ⇒  U
```

**Example:**
```
LOG(EXP(K))
  → LogWrap(ExpWrap(Regular(K)))
  → Regular(K)
  = K
```

### R2.2 — EXP ∘ LOG cancellation [AXIOM]

```
ExpWrap(LogWrap(U))  ⇒  U
```

**Example:**
```
EXP(LOG(Pa))
  → ExpWrap(LogWrap(Regular(Pa)))
  → Regular(Pa)
  = Pa
```

This is the rule that makes `pref = EXP(LOG(psol) - dgeop/RT)` type
as `Pa` after the inner subtraction reduces to `LogWrap(Pa)` (§5).

### R2.3 — Dim'less collapse [AXIOM]

```
LogWrap(Regular(0,...,0))  ⇒  Regular(0,...,0)
ExpWrap(Regular(0,...,0))  ⇒  Regular(0,...,0)
```

Applied **eagerly** whenever a wrapper around dim'less would be
formed — at the LOG/EXP intrinsic site (per R3.1, R3.2) **and** as
the result of any arithmetic operation that would produce a
wrapper-of-dim'less (e.g., `Ln - Ln` reducing via R5.2).

**Example:**
```
p1, p2 :: Pa
LOG(p1/p2)
  → LOG(Regular(0,0,0,0,0,0,0))          # p1/p2 cancels to dim'less
  → Regular(0,0,0,0,0,0,0)                # collapsed; result is plain dim'less
```

```
LOG(p_top) - LOG(p_bot)                   # both :: Pa
  → LogWrap(Pa) - LogWrap(Pa)
  → LogWrap(Pa / Pa)                      (R5.2)
  → LogWrap(dim'less)
  → Regular(dim'less)                     (R2.3 collapse)
  = 1
```

Rationale: `log(c)` and `exp(c)` for dim'less `c` are just real
numbers — semantically indistinguishable from any other dim'less
value. Preserving a log-tag on dim'less would catch a marginal set
of confusions while imposing noise on common patterns (Magnus
formulas, EXP-of-dim'less scaling factors). The collapse keeps
arithmetic clean.

> Note (forward-compat for soft units): if DimFort later adds dB,
> pH, log-pressure-coordinate support, those will be **specific**
> soft-unit tags (`SoftUnit(dB)`, etc.), not generic
> `LogWrap(dim'less)`. The collapse rule does not block that path —
> soft units would be introduced as their own type alongside
> Regular/LogWrap/ExpWrap.

### R2.4 — Error propagation [META — type-system invariant]

`Error` is treated as a special bottom type that propagates through
every operation:

```
LOG(Error)              ⇒  Error
EXP(Error)              ⇒  Error
Error ± anything        ⇒  Error
Error × anything        ⇒  Error
Error ÷ anything        ⇒  Error
anything ÷ Error        ⇒  Error
Error ^ k               ⇒  Error
LogWrap(Error)          ⇒  Error    (cannot wrap an erroring inner)
ExpWrap(Error)          ⇒  Error
```

**Diagnostic emission policy**: the diagnostic is emitted **once**,
at the deepest erroring sub-expression. Subsequent operations on an
already-erroring value produce `Error` silently — no cascading
diagnostics. This avoids diagnostic spam (one mistyped variable
shouldn't produce 50 errors up the call chain).

**Example:**
```
LOG(p) * LOG(p)                  # p :: Pa
  → LogWrap(Pa) × LogWrap(Pa)
  → Error (R5.6 emits D1.2 diagnostic)

EXP(LOG(p) * LOG(p))             # outer EXP on the erroring inner
  → EXP(Error)
  → Error                         # silent; no second diagnostic
```

This rule is implicit in any sensible type-system implementation;
made explicit here so the contract is documented and so test cases
exercising error-cascade behaviour have a reference.

---

## 3. Type rules for `LOG` and `EXP` intrinsics

### R3.1 — `LOG` typing [AXIOM]

```
LOG(value :: U)  ⇒  LogWrap(U)
```
Followed by R2.1 if `U = ExpWrap(V)`.

**Example:**
```
psol :: Pa
LOG(psol) :: LogWrap(Pa)

t :: ExpWrap(K)
LOG(t) :: LogWrap(ExpWrap(K)) → K     (R2.1 cancels)
```

### R3.2 — `EXP` typing [AXIOM]

```
EXP(value :: U)  ⇒  ExpWrap(U)
```
Followed by R2.2 if `U = LogWrap(V)`.

**Example:**
```
x :: K
EXP(x) :: ExpWrap(K)

lp :: LogWrap(Pa)
EXP(lp) :: ExpWrap(LogWrap(Pa)) → Pa  (R2.2 cancels)
```

### R3.3 — `LOG10` / `LOG2` typing [DERIVED from R3.1]

Same as R3.1. The log base is a numerical constant only; unit
algebra is identical.

*Derivation*: `LOG10(x) = LOG(x) / LOG(10)`. The factor `1/LOG(10)`
is a dim'less numerical constant; per R5.4 (literal scalar on
LogWrap), it doesn't change the unit type. So `LOG10` and `LOG`
produce identical typed results.

**Example:**
```
LOG10(psol) :: LogWrap(Pa)            # same type as LOG(psol)
```

### R3.4 — No special case for argument [META — clarification]

R3.1 and R3.2 apply regardless of whether the argument is dim'less,
unitful, or already wrapped. Cancellation (R2.1/R2.2) and collapse
(R2.3) are applied after construction.

**Example:**
```
LOG(2.0) :: 1                         # literal 2.0 is dim'less; collapsed via R2.3
LOG(LOG(Pa)) :: LogWrap(LogWrap(Pa))  # no cancellation (R2.1 only fires for LOG of EXP)
```

---

## 4. Arithmetic on Regular units

These are the existing DimFort rules (pre-spec). Listed here for
cross-reference.

### R4.1 — Addition / subtraction of Regular [AXIOM]

```
Regular(t1) ± Regular(t2)  ⇒  Regular(t1)        if t1 = t2
                            ⇒  ERROR (D1.1)       otherwise (unless D1.5 applies)
```

**Example:**
```
Pa + Pa  → Pa                                    # ✓ matching
Pa + K   → ERROR D1.1                            # mismatch
Pa + 1.0 → D1.5 (auto-cast with H010 warning)    # numeric literal
```

### R4.2 — Multiplication / division of Regular [AXIOM]

```
Regular(t1) × Regular(t2)  ⇒  Regular(t1 + t2)   # tuple elementwise add
Regular(t1) ÷ Regular(t2)  ⇒  Regular(t1 - t2)   # tuple elementwise sub
```

**Example:**
```
Pa × m   → Regular(0, -2, 1, 0, 0, 0, 0) = kg/s²   # Pa = kg/(m·s²); Pa·m = kg/s²
m / s    → Regular(1, -1, 0, 0, 0, 0, 0) = m/s
1 × Pa   → Regular(-1, -2, 1, 0, 0, 0, 0) = Pa     # dim'less neutral element
```

### R4.3 — Power on Regular [AXIOM]

For literal rational `k`:

```
Regular(t) ^ k  ⇒  Regular(k · t)
```

For non-literal `k`:

- **Rd base** (`t = 0,...,0`): result is `Rd` — `0·k = 0` for every
  `k`, literal or not. (Refinement of 2026-05-21 closing the LMDZ
  noise from interpolation-weight and stride-doubling patterns like
  `2 ** (ig2 - 1)`.)
- **Rn base** (`t ≠ 0,...,0`): ERROR D1.4 (runtime-dependent unit;
  classic Exner `p^kappa` case).

The exponent's own unit is checked by Gate 1 of Table 14.4 (D1.7 —
"exponent must be dim'less"). R4.3 assumes the exponent is dim'less;
non-dim'less exponents are rejected before this rule is consulted.

**Example:**
```
m^2          → Regular(2, 0, 0, 0, 0, 0, 0)  = m²
m^0.5        → Regular(0.5, 0, ...)          = m^(1/2)  (allowed; from SQRT)
2 ** (ig2-1) → Regular(0,...,0)              = 1        (Rd base + non-literal k)
Pa^kappa     → ERROR D1.4 if kappa is non-literal
             → Regular(-kappa, -2·kappa, kappa, ...) if kappa is a literal PARAMETER
```

---

## 5. Arithmetic on `LogWrap` units

Apply the **log homomorphism**: `+`/`-` on log-units corresponds to
`·`/`÷` of the inner units.

### R5.1 — LogWrap + LogWrap [AXIOM — the log homomorphism]

```
LogWrap(U) + LogWrap(V)  ⇒  LogWrap(U · V)
```

The inner `·` is computed by R4.2 (or recursively; may error per R5.6).

**Example:**
```
LOG(p1) + LOG(p2)         # both p :: Pa
  → LogWrap(Pa) + LogWrap(Pa)
  → LogWrap(Pa · Pa)
  → LogWrap(Pa²)
  = LogWrap(Regular(-2, -4, 2, 0, 0, 0, 0))
```

Mathematical justification: `log(a) + log(b) = log(a · b)`.

### R5.2 — LogWrap − LogWrap [DERIVED from R5.1]

```
LogWrap(U) - LogWrap(V)  ⇒  LogWrap(U / V)
```

*Derivation*: subtraction is addition of the inverse. Under R5.1's
homomorphism, the multiplicative inverse of `LogWrap(V)` corresponds
to the inner `1/V`. So `LogWrap(U) - LogWrap(V) = LogWrap(U) +
LogWrap(1/V) = LogWrap(U · 1/V) = LogWrap(U/V)`.

**Example:**
```
LOG(p_top) - LOG(p_bot)   # both :: Pa
  → LogWrap(Pa) - LogWrap(Pa)
  → LogWrap(Pa / Pa)
  → LogWrap(Regular(0,...,0))                # wrapper-of-dim'less constructed
  → Regular(0,...,0)                          # R2.3 collapses to plain dim'less
  = 1
```

### R5.3 — LogWrap + dim'less [AXIOM]

```
LogWrap(U) + Regular(0,...,0)  ⇒  LogWrap(U)
LogWrap(U) - Regular(0,...,0)  ⇒  LogWrap(U)
```

Rationale: `log(u) + c = log(u · e^c)` — constant absorbed inside log.

**Example:**
```
LOG(psol) - dgeop/RT      # dgeop/RT types as dim'less
  → LogWrap(Pa) - Regular(dim'less)
  → LogWrap(Pa)            # tag preserved; constant absorbed
```

This is the rule that makes the hydrostatic idiom type-check.

### R5.4 — Literal scalar × LogWrap [AXIOM]

For literal rational `k`:

```
k · LogWrap(U)  ⇒  LogWrap(U ^ k)
```

**Example:**
```
2.0 * LOG(p)              # p :: Pa
  → 2.0 · LogWrap(Pa)
  → LogWrap(Pa^2.0)
  → LogWrap(Regular(-2, -4, 2, 0, 0, 0, 0))
  = LogWrap(Pa²)
```

Mathematical justification: `k · log(u) = log(u^k)`.

### R5.5 — Non-literal scalar × LogWrap [DERIVED from R5.4 + R4.3]

```
k · LogWrap(U)   where k is non-literal   ⇒  ERROR (D1.4)
```

*Derivation*: R5.4 would produce `LogWrap(U^k)`. The inner `U^k`
requires R4.3, which errors on non-literal `k`. The error propagates.

**Example:**
```
factor :: dim'less    (variable, not PARAMETER)
factor * LOG(p)
  → ERROR D1.4         # can't put runtime value into unit exponent
```

### R5.6 — LogWrap × LogWrap [AXIOM — undefined operation]

```
LogWrap(U) × LogWrap(V)  ⇒  ERROR (D1.2)
```

Rationale: `log(u) · log(v)` has no clean physical/mathematical
interpretation.

**Example:**
```
LOG(p1) * LOG(p2)
  → LogWrap(Pa) × LogWrap(Pa)
  → ERROR D1.2
```

### R5.7 — LogWrap × Regular (non-dim'less) [AXIOM — undefined operation]

```
LogWrap(U) × Regular(t)   where t ≠ (0,...,0)   ⇒   ERROR (D1.2)
```

Note: per R2.3, `LogWrap(U)` always has `U` non-dim'less (the
dim'less case would have collapsed at construction), so the `U ≠
Regular(0,...,0)` condition is implicit.

**Example:**
```
LOG(p) * mass             # p :: Pa, mass :: kg
  → LogWrap(Pa) × Regular(kg)
  → ERROR D1.2
```

### R5.8 — Literal `1.0` × LogWrap [DERIVED from R5.4 at k=1]

```
1.0 · LogWrap(U)  ⇒  LogWrap(U)
```

*Derivation*: special case of R5.4 with `k = 1.0`, giving
`LogWrap(U^1.0) = LogWrap(U)`. Listed here because it's the most
common identity case.

> Note: the previous formulation "LogWrap × any dim'less Regular →
> LogWrap(U)" was withdrawn — it conflicted with R5.4 for literal
> multipliers other than `1.0` (e.g. `2.0 * LOG(Pa)` correctly gives
> `LOG(Pa²)`, not `LOG(Pa)`). For non-literal dim'less Regular
> operands, R5.5 errors per D1.4.

**Example:**
```
1.0 * LOG(p)              # p :: Pa
  → LogWrap(Pa^1)
  = LogWrap(Pa)            # unchanged
```

### R5.9 — Power of LogWrap [AXIOM — undefined operation]

```
LogWrap(U) ^ k   for k ≠ 1   ⇒   ERROR (D1.2)
LogWrap(U) ^ 1   ⇒   LogWrap(U)            (trivial identity)
```

Rationale: `(log u)^k` for `k ≠ 1` is "log raised to a power", not
"log of something". It has no clean log-of-anything interpretation.

Distinct from R5.4 (`k · LogWrap(U) ⇒ LogWrap(U^k)`), which IS
defined: `k · log u = log(u^k)` — scalar-times-log = log-of-power.

**Example:**
```
LOG(p) ** 2                            # p :: Pa
  → LogWrap(Pa) ^ 2
  → ERROR D1.2 [R5.9]
```

```
1.0 / LOG(p)                           # equivalent to LogWrap^(-1)
  → ERROR D1.2 [R5.9 with k = -1]
```

This rule also closes the gap "division by LogWrap" — since division
is multiplication by inverse and the inverse errors here, any
expression of form `anything / LogWrap(_)` errors through this rule.

### R5.10 — LogWrap + Regular(non-dim'less) [AXIOM — undefined operation]

```
LogWrap(U) ± Regular(t)   where t ≠ (0,...,0)   ⇒   ERROR (D1.3)
```

Rationale: dual of R6.6 on the LogWrap side. `log(u) + Pa` has no
clean interpretation — log results are numerical, adding to a unitful
pressure is meaningless.

R5.3 covers the dim'less-Regular case; R5.10 handles the
non-dim'less-Regular case. Together with R5.1 (LogWrap + LogWrap)
and R6.6 (covers LogWrap + ExpWrap by symmetry), addition involving
LogWrap is fully specified.

**Example:**
```
LOG(p) + pressure                       # p :: Pa, pressure :: Pa
  → LogWrap(Pa) + Regular(Pa)
  → ERROR D1.3 [R5.10]
```

Note: per R2.3, a wrapper-of-dim'less can't reach this rule —
`LOG(p1/p2)` collapses to `Regular(dim'less)` at construction, and
then `Regular(dim'less) + Pa` is the standard R4.1 case (matching
required, or D1.5 cast if literal).

---

## 6. Arithmetic on `ExpWrap` units

Apply the **exp homomorphism** (dual of log): `×`/`÷` on exp-units
corresponds to `+`/`-` of the inner units.

### R6.1 — ExpWrap × ExpWrap [AXIOM — the exp homomorphism]

```
ExpWrap(U) × ExpWrap(V)  ⇒  ExpWrap(U + V)
```

The inner `+` is computed by R4.1 (may error if mismatched).

**Example:**
```
EXP(temp1) * EXP(temp2)   # both temp :: K
  → ExpWrap(K) × ExpWrap(K)
  → ExpWrap(K + K)
  → ExpWrap(K)
```

Mathematical justification: `exp(a) · exp(b) = exp(a + b)`.

### R6.2 — ExpWrap ÷ ExpWrap [DERIVED from R6.1]

```
ExpWrap(U) / ExpWrap(V)  ⇒  ExpWrap(U - V)
```

*Derivation*: division is multiplication by the inverse. Under R6.1's
homomorphism, the multiplicative inverse of `ExpWrap(V)` corresponds
to the inner additive inverse `-V`. So `ExpWrap(U) / ExpWrap(V) =
ExpWrap(U) × ExpWrap(-V) = ExpWrap(U + (-V)) = ExpWrap(U-V)`.

**Example:**
```
EXP(temp1) / EXP(temp2)   # both :: K
  → ExpWrap(K - K)
  → ExpWrap(K)
```

### R6.3 — ExpWrap × dim'less [AXIOM]

```
ExpWrap(U) × Regular(0,...,0)  ⇒  ExpWrap(U)
ExpWrap(U) ÷ Regular(0,...,0)  ⇒  ExpWrap(U)
```

Rationale: `exp(u) · c = exp(u + log(c))`; `log(c)` is dim'less, so
the inner sum reduces to `U` by R4.1.

**Example:**
```
EXP(temp) * 2.0           # temp :: K, 2.0 :: dim'less literal
  → ExpWrap(K) × Regular(dim'less)
  → ExpWrap(K)             # unchanged
```

### R6.4 — ExpWrap ^ literal scalar [AXIOM]

For literal rational `k`:

```
ExpWrap(U) ^ k  ⇒  ExpWrap(k · U)
```

(Inner scaling uses R4.3.)

**Example:**
```
EXP(temp) ^ 2             # temp :: K
  → ExpWrap(K)^2
  → ExpWrap(2 · K)
  → ExpWrap(Regular(0, 0, 0, 2, 0, 0, 0))
  = ExpWrap(K²)
```

Mathematical justification: `(exp(u))^k = exp(k · u)`.

### R6.5 — ExpWrap + ExpWrap [AXIOM — undefined operation]

```
ExpWrap(U) + ExpWrap(V)  ⇒  ERROR (D1.3)
```

Rationale: `exp(u) + exp(v)` has no clean closed form (unlike
`log(u) + log(v)` which reduces). The exp side lacks the additive
homomorphism that log has.

**Example:**
```
EXP(x) + EXP(y)
  → ERROR D1.3
```

### R6.6 — ExpWrap + non-ExpWrap [AXIOM]

```
ExpWrap(U) + (non-ExpWrap value)  ⇒  ERROR (D1.3 or D1.5)
```

Where D1.5 fires if the non-ExpWrap operand is a numeric literal
(soft-cast with warning).

**Example:**
```
EXP(x) + 1.0                            # x :: K
  → ExpWrap(K) + Regular(dim'less literal)
  → D1.5 H010 warning; cast literal to En; result En

EXP(x) + pressure                        # pressure :: Pa (variable)
  → ERROR D1.3
```

### R6.7 — ExpWrap × Regular (non-dim'less) [AXIOM — undefined operation]

```
ExpWrap(U) × Regular(t)   where t ≠ (0,...,0)   ⇒   ERROR (D1.2)
ExpWrap(U) ÷ Regular(t)   where t ≠ (0,...,0)   ⇒   ERROR (D1.2)
(symmetric for Regular × ExpWrap, Regular ÷ ExpWrap)
```

Rationale: dual of R5.7. `exp(unitful) · pressure` has no physical
interpretation. Per R2.3, `ExpWrap(U)` always has `U` non-dim'less.

**Example:**
```
EXP(temp) * pressure                   # temp :: K, pressure :: Pa
  → ExpWrap(K) × Regular(Pa)
  → ERROR D1.2 [R6.7]
```

When `t` is dim'less, R6.3 handles it instead (ExpWrap × dim'less =
ExpWrap, unchanged).

---

## 7. Cross-cases and mixed-wrapper arithmetic

### R7.1 — LogWrap × ExpWrap [AXIOM — undefined operation]

```
LogWrap(U) × ExpWrap(V)  ⇒  ERROR (D1.2)
LogWrap(U) ÷ ExpWrap(V)  ⇒  ERROR (D1.2)
(symmetric: ExpWrap × LogWrap, ExpWrap ÷ LogWrap)
```

(Except via cancellation R2.1 / R2.2 if one wraps the other at the
type-construction level — though those reductions happen before this
rule would fire.)

Rationale: `log(u) · exp(v)` mixes two algebraic worlds with no
shared homomorphism. The product `log(u) · exp(v)` doesn't reduce to
log or exp of anything natural.

**Note on `+`/`-`**: the `LogWrap + ExpWrap` case is covered by
R6.6 (`ExpWrap + non-ExpWrap → ERROR`), not by R7.1. R7.1 covers
only `×` and `÷`.

**Example:**
```
LOG(p) * EXP(temp)
  → LogWrap(Pa) × ExpWrap(K)
  → ERROR D1.2 [R7.1]

LOG(p) + EXP(temp)
  → ERROR D1.3 [R6.6, NOT R7.1]    # R6.6 dominates for +

EXP(LOG(p))
  → ExpWrap(LogWrap(Pa))
  → Pa                              # R2.2 cancellation; never reaches R7.1
```

### R7.2 — Deeply-nested wrapper arithmetic [DERIVED from R5.1/R6.1 + R5.6/R6.5/R7.1]

When R5.1 or R6.1 produces an inner expression that is itself between
two wrapped units, the inner operation may error per R5.6 / R6.5.

*Derivation*: this isn't an independent rule — it's a consequence of
how the homomorphism rules (R5.1, R6.1) cascade. When the inner
operation hits an undefined case (R5.6, R6.5, R7.1), the error
propagates outward. R7.2 just documents the cascade behaviour
explicitly so users understand why deep nesting can error.

**Example:**
```
LOG(LOG(p1)) + LOG(LOG(p2))            # both p :: Pa
  → LogWrap(LogWrap(Pa)) + LogWrap(LogWrap(Pa))
  → LogWrap(LogWrap(Pa) · LogWrap(Pa))      (R5.1)
  → ERROR D1.2 on inner ·                    (R5.6)
```

The type representation is closed under construction (`LOG(LOG(p))`
is a valid type), but the operation is undefined.

### R7.3 — Wrapper-of-dim'less transparency [REMOVED — superseded by R2.3]

Previously: wrapper-of-dim'less acted as transparent dim'less in mixed
arithmetic with Regular(non-dim'less).

Removed 2026-05-20 in favour of R2.3 (eager collapse). With R2.3,
no wrapper-of-dim'less type ever exists at operation time — it's
already collapsed to plain `Regular(dim'less)` at construction. So
R7.3 had nothing to apply to.

The cases R7.3 used to handle now resolve as follows:

```
e_0 * EXP(arg)           # arg :: dim'less, e_0 :: mbar
  → mbar × EXP(dim'less)
  → mbar × Regular(dim'less)      (R2.3 collapse on construction of EXP)
  → mbar                            (R4.2 dim'less × non-dim'less)
```

```
LOG(p1/p2) * mass        # p1, p2 :: Pa, mass :: kg
  → LOG(Regular(dim'less)) × Regular(kg)
  → Regular(dim'less) × Regular(kg)    (R2.3 collapse)
  → Regular(kg)                         (R4.2)
```

Counter-example still works correctly:
```
LOG(p) * mass            # p :: Pa, mass :: kg
  → LogWrap(Pa) × Regular(kg)
  → ERROR D1.2 per R5.7              # LogWrap's inner is non-dim'less
```

---

## 8. Diagnostic classes

### D1.1 — Unit mismatch in addition/subtraction (`H002`)

Two operands of `+` or `−` have non-matching Regular units.
Triggered by R4.1.

**Example:**
```
pressure + temperature        # Pa + K
  → H002 D1.1
```

### D1.2 — Undefined operation (`H001` or `H002` per context)

Operations like `LogWrap × LogWrap` (R5.6), `LogWrap × non-dim'less
Regular` (R5.7), `LogWrap × ExpWrap` (R7.1).

**Example:**
```
LOG(p1) * LOG(p2)
  → H002 D1.2 [R5.6]
```

### D1.3 — Undefined sum involving ExpWrap

Specifically `ExpWrap + ExpWrap` (R6.5), `ExpWrap + non-ExpWrap`
non-literal (R6.6), `LogWrap + ExpWrap` (R7.1).

**Example:**
```
EXP(x) + EXP(y)
  → H002 D1.3 [R6.5]
```

### D1.4 — Runtime-dependent unit (`H001`)

Power with non-literal exponent (R4.3), scalar-times-wrapper with
non-literal scalar (R5.5).

**Example:**
```
p ** kappa                   # kappa not declared PARAMETER
  → H001 D1.4 [R4.3]
```

### D1.5 — Implicit literal cast (`H010`, warning)

`+` or `−` between a numeric literal and a unitful operand. Auto-cast
the literal to the unitful's unit; emit warning.

**UX rendering** (LSP / editor companions):
- **LSP severity**: `DiagnosticSeverity.Warning` (2). Yellow squiggle,
  appears in Problems panel alongside H001/H002 errors but visually
  distinct. Not `Hint` (too easy to overlook) and not `Error` (would
  block CI runs configured to fail on errors).
- **Diagnostic message** (one-line): `H010 implicit cast: literal '<N>'
  to <unit>`. Example: `H010 implicit cast: literal '1.' to m/s`.
- **Extended hover text**: short paragraph explaining the smell, plus
  the suggested rewrite with a named PARAMETER.
- **Code-action**: ✅ shipped. The LSP exposes an `Extract literal
  to a named PARAMETER` quick-fix. In the VSCode companion the
  refactor prompts via `showInputBox` for the parameter name, then
  applies two edits: a typed `REAL, PARAMETER :: <name> = <literal>
  !< @unit{<unit>}` declaration at the end of the enclosing routine's
  declaration block, plus the use-site replacement.

**Example:**
```
ycdragm = ust*ust / (1. + speed) / speed     # speed :: m/s
                                              # literal 1. + m/s
  → H010 D1.5
  hint: prefer a named constant
        REAL, PARAMETER :: one_ms = 1.0   !< @unit{m/s}
```

### D1.6 — Implicit wrapper untag at assignment (`H010`, warning)

Assignment where LHS is `Regular(t)` and RHS is a wrapper-of-dim'less
or wrapper-of-Regular(t). Allow with warning.

**Example:**
```
REAL :: scaling     !< @unit{1}
scaling = EXP(arg)                            # arg :: dim'less
  → ExpWrap(dim'less) assigned to dim'less
  → H010 D1.6 (allow, warn implicit untag)
```

### D1.7 — Exponent must be dimensionless (`H010`, warning)

The exponent of a `**` operator must resolve to a dim'less unit. An
exponent whose unit is non-dim'less would derive (via `a^b = exp(b ·
log(a))`) to an `ExpWrap`-typed result, but in practice this almost
always indicates a typo rather than an intentional entry into
exp-tagged space.

Default severity: **warning**. Promote to error or silence entirely
via `.dimfort.toml`'s `[diagnostics]` section (see §15).

**Example:**
```
real :: speed                                 # speed :: m/s
real :: r
r = 2.0 ** speed
  → 2.0 :: Rd (dim'less literal)
  → speed :: m/s (Rn, non-dim'less)
  → power(Rd, Rn) fires D1.7 (exponent not dim'less)
  → H010 D1.7 warning
```

Rewrite to enter exp-tagged space explicitly when intentional:
```
r = EXP(speed * LOG(2.0))     ! same type, explicit intent
```

The derivation route through this rewrite is allowed because `EXP(...)`
is an explicit user signal of intent, whereas `**` is the conventional
power operator and carries the conventional "exponent is a pure number"
expectation.

---

## 9. Pretty-printing

### P1 — Regular units

Use the existing pretty-printer.

**Example:**
```
Regular(0, 0, 0, 0, 0, 0, 0)      → "1"
Regular(-1, -2, 1, 0, 0, 0, 0)    → "Pa"
Regular(2, -2, 0, -1, 0, 0, 0)    → "J/(kg*K)"
```

### P2 — Wrapper units

```
prettyprint(LogWrap(u))  =  "LOG(" + prettyprint(u) + ")"
prettyprint(ExpWrap(u))  =  "EXP(" + prettyprint(u) + ")"
```

**Example:**
```
LogWrap(Regular(Pa))                  → "LOG(Pa)"
LogWrap(LogWrap(Regular(Pa)))         → "LOG(LOG(Pa))"
ExpWrap(Regular(K))                   → "EXP(K)"
ExpWrap(LogWrap(Regular(Pa)))         (canonicalized to "Pa" via R2.2 — never printed in wrapped form)
```

### P3 — Round-trip property

```
parse(prettyprint(u)) ≡ u
```

**Example:**
```
u = LogWrap(LogWrap(Regular(Pa)))
prettyprint(u) = "LOG(LOG(Pa))"
parse("LOG(LOG(Pa))") = u           ✓
```

### P4 — No shorthand

`LOG^3(U)` and similar are not used. Always fully nested:
`LOG(LOG(LOG(U)))`.

### P5 — No bare-word LOG

Parentheses are always required.

**Example (rejected):**
```
@unit{LOG Pa}     → parse error
@unit{LOG(Pa)}    → OK
```

---

## 10. Annotation syntax

### A1 — Wrapper annotations

```fortran
REAL :: lpref   !< @unit{LOG(Pa)}
REAL :: foo     !< @unit{EXP(EXP(K))}
```

### A2 — Parser resilience

The parser accepts:
- Lowercase: `@unit{log(Pa)}` parses identically to `@unit{LOG(Pa)}`.
- Whitespace: `@unit{LOG( Pa )}`, `@unit{LOG (Pa)}`.

The pretty-printer always produces uppercase, no extra whitespace.

**Example:**
```
@unit{log( Pa )}     parses to  LogWrap(Regular(Pa))
                     pretty-prints as  "LOG(Pa)"
```

### A3 — Canonicalization on parse

Inverse pairs canonicalize on read.

**Example:**
```
@unit{EXP(LOG(Pa))}  parses to  Regular(Pa)
                     pretty-prints as  "Pa"
```

---

## 11. Open questions (not yet decided)

### OQ1 — RESOLVED (see R2.3)

The previous open question — `unitful × ExpWrap(dim'less)` behaviour —
is closed by R2.3 (eager dim'less collapse). Since `EXP(dim'less)`
collapses to `Regular(dim'less)` at construction, the operation
reduces to `unitful × Regular(dim'less)` and is handled by R4.2.

Resolution date: 2026-05-20.

### OQ2 — Soft "quantity kinds" (dB, Np, pH, log-pressure coords)

**Status**: deferred — out of scope for "DimFort as a dimensional-
homogeneity tool", in scope for a hypothetical future "DimFort as a
quantity-kind tracker" extension.

#### Why it isn't homogeneity

Dimensional homogeneity is a structural check on SI-base-unit exponents
— the 7-tuple `(M, L, T, Θ, I, N, J)`. Under that check, dB / pH /
log-pressure are all `Regular(0,...,0)` — indistinguishable from any
other dim'less quantity. Catching `loudness = 2.0 * loudness` (which
is wrong in dB-space — dB levels add, not multiply by scalars)
requires tracking *which* dim'less kind the value is, not just *that*
it's dim'less. That's a richer type system — a *soft tag* on top of
the dim/unit system.

#### Landscape of existing approaches

Worth surveying before any implementation. Tools that handle, or
attempt to handle, soft quantity kinds:

| Tool / language | Approach |
|---|---|
| **Pint** (Python) | "Offset units" for things like Celsius / Fahrenheit (related to soft units via affine transforms); custom plumbing for true soft tags. |
| **TypeScript brand types** | `type DB = number & { __dB: never }` — nominal soft tags via structural-typing escape hatches. Manual per kind. |
| **F# units of measure** | First-class unit-of-measure types; doesn't formally handle dB but the type system is the closest mainstream precedent. |
| **Fortress** (Sun Labs research) | Had unit-of-measure types with named dim'less kinds. Discontinued. |
| **ATS** | Dependent types subsume soft tags. Academic. |
| **CamFort, F18 units proposal** | Homogeneity-only. Same scope as DimFort. |

None of the *Fortran-targeted* tools handle this today. Doing it
in DimFort would be a deliberate scope expansion that positions it
against type-system research languages rather than against other
Fortran-units tools.

#### Triggers to revisit

Reopen this question when at least one of:

1. A DimFort user has a real Fortran codebase with a dB / pH / log-
   coord quantity they want to annotate, and prose-and-convention
   has demonstrably been insufficient.
2. The "quantity-kind tracker" positioning is consciously adopted —
   product-direction decision, not opportunistic.
3. A clean design lands for soft-kind arithmetic that doesn't
   require touching the frozen Regular/LogWrap/ExpWrap algebra
   (e.g., a fully orthogonal `SoftKind` layer that interacts with
   `Regular` only at marked boundary points).

Until one of those, the sketch from earlier discussions stands as
a placeholder: a `SoftKind(name, base_unit)` type alongside
Regular / LogWrap / ExpWrap, with its own arithmetic rules and a
new diagnostic class for soft-kind violations. None of it has been
specified.

#### Forward-compatibility hooks already in place

R2.3 (dim'less collapse) doesn't prevent a future `SoftKind` type
from existing alongside `Regular`. `SoftKind` would NOT be a special
case of `LogWrap(Regular(0,...,0))`; it would be its own type
kind, so the collapse rule applies to genuine wrapper-of-dim'less
and leaves soft tags alone.

### OQ3 — Derived-type unit annotations

Per-field unit annotations on Fortran derived types are out of scope
for this spec; tracked separately in the DimFort backlog.

### OQ4 — Variable-exponent powers (`x**y` with non-literal `y`)

Currently errors via D1.4 (R4.3, R5.5). Forward-compatible with
future precise typing (e.g. when `y` is a `PARAMETER` literal).

### OQ5 — RESOLVED (missing-annotation propagation)

The previous open question — what `x + y` should produce when `x`
has no `@unit{}` annotation and `y :: m/s` — is resolved as
**unknown**. DimFort returns `None` for the expression as a whole;
it never infers a missing annotation from a sibling operand,
because silent inference would mask real bugs. The actionable
diagnostic is `U005` on the unannotated declaration; the assignment
itself emits no `H`-series error because consistency against an
unknown operand can't be verified.

Same applies to wrapper cases: `LOG(x)` with `x` unannotated → the
LOG call resolves to `None`, propagating outward; U005 still fires
on `x`'s declaration.

Resolution date: 2026-05-20.

---

## 12. Trace mechanism

Enable per-expression unit-computation traces to help debug
diagnostics and explain typing decisions to users.

### T1 — Trace data structure

Each `Unit` value carries an optional `provenance` chain:

```
Provenance = list of (rule_id, before_unit(s), after_unit, location)
```

Built up as rules fire during expression typing.

### T2 — Trace activation

Off by default (memory overhead). Enabled via:
- CLI flag: `dimfort check --trace`
- Per-diagnostic: any diagnostic at H010 or above automatically
  includes its RHS trace in verbose mode.
- LSP hover on an annotation shows the trace if available.

### T3 — Trace pretty-printing

Rule numbers in trace output link back to this spec.

**Example output:**
```
H001 at cdrag_mod.f90:300: Assignment unit mismatch: Pa ≠ 1
trace for RHS:
  EXP(LOG(psol) - zgeop1/(RD*t1*(1.+RETV*max(q1,0.))))
  → LOG(psol)                                  ⇒ LogWrap(Pa)        [R3.1]
  → zgeop1 / (RD * t1 * (1. + ...))            ⇒ Regular(dim'less)  [R4.2]
  → LogWrap(Pa) - Regular(dim'less)            ⇒ LogWrap(Pa)        [R5.3]
  → EXP(LogWrap(Pa))                           ⇒ Regular(Pa)        [R2.2]
```

---

## 13. Implementation phasing

All four phases have shipped. The list is preserved for historical
reference and to anchor cross-links from code comments.

1. **Phase A** — ✅ shipped. `H010` severity tier (D1.5 only).
2. **Phase B** — ✅ shipped (5 sub-step commits). Log/Exp wrapper
   representation, rules R1–R7, diagnostics D1.2 / D1.3 / D1.4,
   R5/R6 reductions.
3. **Phase C** — ✅ shipped. `H010` assignment soft-cast (D1.6) for
   the rare case of explicit wrapper-typed assignments to Regular
   targets. With R2.3 collapse most wrapper-of-dim'less assignments
   resolve naturally, so D1.6 fires only when the inner unit is
   non-dim'less and matches the LHS — i.e. when the assignment
   "drops" a log/exp tag whose carrier is unitful.
4. **Phase D** — ✅ shipped. Trace mechanism: `Provenance` records
   in `dimfort.core.trace`, hooks at every rule fire in
   `combine` / `power` / `wrap_log` / `wrap_exp`, opt-in via
   `dimfort check --trace` and the LSP `traceHoverEnabled` flag
   (toggled from VSCode via `DimFort: Toggle Full Unit Trace in
   Hover`). LSP hover renders the trace as an ASCII tree of the
   enclosing assignment.

A and B are independent. C depends on B. D can ship any time after B.

---

## 14. Operation tables (rule lookup)

For each binary operation between operand-type pairs, the cell shows
the rule that applies and the resulting type. Tables are an
exhaustive enumeration of the axioms and derived rules in §4–§7;
they exist to make rule lookup fast and to expose any gaps.

### Operand legend

With R2.3 in effect, wrapper-of-dim'less types never exist at
operation time (they're collapsed at construction). The four
operand categories are:

```
Rd = Regular(0,...,0)         dim'less Regular  (e.g.,  1, kg/kg, count)
Rn = Regular(t≠0)             non-dim'less Regular  (e.g.,  Pa, m/s, K)
Ln = LogWrap(Rn)              log of non-dim'less  (e.g.,  LOG(p))
En = ExpWrap(Rn)              exp of non-dim'less  (e.g.,  EXP(temp))
```

Cell format: `rule → result`. `sym` means "mirror cell (operation
commutes)". Deeper nestings (e.g., `LogWrap(Ln)`) follow the same
rules recursively.

### Table 14.1 — Addition / Subtraction (`+` / `−`)

> **Status**: frozen 2026-05-20. Any rule change that would alter a
> cell in this table must be flagged explicitly.

Commutative; upper triangle shown.

| `+/-` | Rd | Rn | Ln | En |
|---|---|---|---|---|
| **Rd** | R4.1 → Rd | R4.1 → ERR D1.1 *(D1.5 cast if literal → Rn)* | R5.3 → Ln | R6.6 → ERR D1.3 *(D1.5 cast if literal → En)* |
| **Rn** | sym | R4.1 → Rn *(if tuples match; else ERR D1.1)* | R5.10 → ERR D1.3 | R6.6 → ERR D1.3 |
| **Ln** | sym | sym | R5.1 → Ln (+) *[richer inner]* / R5.2 → Ln or Rd (−) *[Rd if inner tuples match, via R2.3]* | R6.6 → ERR D1.3 |
| **En** | sym | sym | sym | R6.5 → ERR D1.3 |

### Table 14.2 — Multiplication (`×`)

> **Status**: frozen 2026-05-20. Any rule change that would alter a
> cell in this table must be flagged explicitly.

Commutative; upper triangle shown.

| `×` | Rd | Rn | Ln | En |
|---|---|---|---|---|
| **Rd** | R4.2 → Rd | R4.2 → Rn | R5.4 → Ln *(literal k)* / R5.5 → ERR D1.4 *(non-literal k)* | R6.3 → En |
| **Rn** | sym | R4.2 → Rn *(exponents add)* | R5.7 → ERR D1.2 | R6.7 → ERR D1.2 |
| **Ln** | sym | sym | R5.6 → ERR D1.2 | R7.1 → ERR D1.2 |
| **En** | sym | sym | sym | R6.1 → En *(if inner tuples match; else ERR D1.1; may collapse to Rd via R2.3)* |

### Table 14.3 — Division (`÷`)

> **Status**: frozen 2026-05-20. Any rule change that would alter a
> cell in this table must be flagged explicitly.

**Not commutative.** Row = denominator (`B`), column = numerator (`A`).
Read as: `A ÷ B`.

Convention: `A ÷ B = A × B^(−1)`. Inverses: `Rd^(−1) = Rd`,
`Rn^(−1) = Rn'` (negated tuple, still Rn category), `En^(−1) = En'`
(via R6.4 with k=−1). `Ln^(−1) → ERR` via R5.9.

| `A ÷ B` ↓ \\ `A` → | Rd | Rn | Ln | En |
|---|---|---|---|---|
| **÷ Rd** | R4.2 → Rd | R4.2 → Rn | R5.4 → Ln *(literal k)* / R5.5 → ERR D1.4 *(non-literal k)* | R6.3 → En |
| **÷ Rn** | R4.2 → Rn *(inverted)* | R4.2 → Rn | R5.7 → ERR D1.2 | R6.7 → ERR D1.2 |
| **÷ Ln** | R5.9 → ERR D1.2 | R5.9 → ERR D1.2 | R5.9 → ERR D1.2 | R7.1 → ERR D1.2 |
| **÷ En** | R6.3 → En *(inverted)* | R6.7 → ERR D1.2 | R7.1 → ERR D1.2 | R6.2 → En *(may collapse if inner-diff is dim'less)* |

### Table 14.4 — Power (`^`)

> **Status**: revised 2026-05-21. Expanded from 4×3 (k-literalness)
> to 4×4 (exponent unit category) with a value-level sub-table for
> the dim'less-exponent column. The previous frozen cells are
> preserved except for the (Rd, k non-literal) cell, which flipped
> from ERR D1.4 to Rd — the spec note that justified the literal
> case (`0·k = 0`) applies for any k, literal or not. Adds D1.7
> ("exponent must be dim'less") covering the 12 non-Rd-exponent
> cells.

Binary on a unit pair (base, exponent). The exponent's *unit
category* selects the column; only the `Rd` column produces a
well-typed result. The other three columns fire D1.7 to surface
the typo before the formal derivation through `a^b = exp(b·log(a))`
would let it propagate as an ExpWrap further downstream.

| Base \ Exponent | **Rd** | **Rn** | **Ln** | **En** |
|---|---|---|---|---|
| **Rd** | Rd | ERR D1.7 | ERR D1.7 | ERR D1.7 |
| **Rn** | *see sub-table* | ERR D1.7 | ERR D1.7 | ERR D1.7 |
| **Ln** | *see sub-table* | ERR D1.7 | ERR D1.7 | ERR D1.7 |
| **En** | *see sub-table* | ERR D1.7 | ERR D1.7 | ERR D1.7 |

**Sub-table — Rd-exponent column** (value-level dispatch):

| Base | k = 1 | k literal, ≠ 1 | k non-literal | k's unit unknown |
|---|---|---|---|---|
| **Rd** | Rd (identity) | Rd *(0·k = 0)* | **Rd** *(0·k = 0 for any k)* | Rd |
| **Rn** | Rn (identity) | R4.3 → Rn *(scaled tuple)* | ERR D1.4 | ERR D1.4 |
| **Ln** | Ln (identity) | R5.9 → ERR D1.2 | R5.9 → ERR D1.2 | R5.9 → ERR D1.2 |
| **En** | En (identity) | R6.4 → En *(scaled inner)* | ERR D1.4 | ERR D1.4 |

The "k's unit unknown" column ("exponent variable has no `@unit{}`
annotation") still resolves the *value* gate; the underlying issue
("annotation missing") is the proper signal of `U005`, which fires
at the declaration. D1.7 is NOT raised for unknown-unit exponents
— that would double-flag the same code, and the unknown case is
much more likely benign (an unannotated integer index) than the
known-but-unitful case (`2.0 ** speed`).

#### Why D1.7 is a *warning* by default

The wrapper algebra would formally type `Rd ^ Rn` as
`exp(Rn · log(Rd)) = exp(Rn) = ExpWrap(Rn)` via R3.2 — i.e., it
has a coherent type. The strict reading would let this pass and
fire errors only when the resulting ExpWrap collides with downstream
operations. The pragmatic reading is that `**` is virtually always
intended to operate on numbers, not unitful quantities; an unitful
exponent reads as a typo more often than as an intentional entry
into exp-tagged space.

D1.7 captures this with a warning at the power site. Projects with
heavier log-coordinate usage can demote it (`"D1.7" = "off"` in
`.dimfort.toml`), and projects that want hard errors can promote
it (`"D1.7" = "error"`). See §15 for the override mechanism.

#### Derivation commentary

The cells in Table 14.4 are consistent with the operational
interpretation `a^b = exp(b · log(a))` under the spec's wrapper
homomorphisms, with one explicit override:

- **Rd ^ Rd** (any value of k): `log(Rd) → Rd` via R2.3; `b · Rd →
  Rd` via R4.2; `exp(Rd) → Rd` via R2.3. Result: Rd. ✓ The
  derivation transparently recovers the `0·k = 0` rule across the
  whole Rd row.
- **Rn ^ Rd-literal**: `log(Rn) → LogWrap(Rn)`; `k · LogWrap(Rn) →
  LogWrap(Rn^k)` via R5.4 (literal-scalar lifts to inner power);
  `exp(LogWrap(Rn^k)) → Rn^k` via R2.2. Matches R4.3. ✓
- **Ln ^ Rd-k≠1**: `log(Ln) → LogWrap(LogWrap(Rn))`; `k ·
  LogWrap(LogWrap(Rn)) → LogWrap(LogWrap(Rn)^k)`; the inner
  `LogWrap(Rn)^k` for k≠1 fires R5.9 D1.2. ✓
- **Rd ^ Rn** (the override): the derivation gives `log(Rd) ·
  Rn · exp(...) → ExpWrap(Rn)`. We override this to D1.7 because
  the syntactic form `a ** b` carries a stronger physical-correctness
  expectation than the explicit `EXP(b * LOG(a))` rewrite that
  would produce the same type.

The scalar-to-power lift in R5.4 / R6.4 is the load-bearing axiom
that the derivation route depends on. Without it, `Rn ^ k` would
lose the scalar `k` to dimensional collapse (R4.2 makes
`Regular(0,...,0) × Regular(t) = Regular(t)`, dropping the value
of the scalar). With R5.4 / R6.4 in place, the derivation
recovers Table 14.4 faithfully — except for the four D1.7 cells
which we surface earlier than the derivation would.

### Table 14.5 — `LOG` (unary)

> **Status**: frozen 2026-05-20. Any rule change that would alter a
> cell in this table must be flagged explicitly.

| Input | Rule | Result |
|---|---|---|
| Rd | R3.1 + R2.3 | Rd *(collapse — log of dim'less is dim'less)* |
| Rn | R3.1 | Ln |
| Ln | R3.1 | LogWrap(Ln) — depth-2 LogWrap |
| En | R3.1 + R2.1 | Inner of En *(R2.1 peels one ExpWrap layer; canonically either Rn or En)* |

### Table 14.6 — `EXP` (unary)

> **Status**: frozen 2026-05-20. Any rule change that would alter a
> cell in this table must be flagged explicitly.

| Input | Rule | Result |
|---|---|---|
| Rd | R3.2 + R2.3 | Rd *(collapse)* |
| Rn | R3.2 | En |
| Ln | R3.2 + R2.2 | Inner of Ln *(R2.2 peels one LogWrap layer; canonically either Rn or Ln)* |
| En | R3.2 | ExpWrap(En) — depth-2 ExpWrap |

### Cell counts (coverage sanity check)

| Op | Valid result | ERROR | Total unique cells |
|---|---|---|---|
| + / − | 4 | 6 | 10 (commutative; 10 unique of 16) |
| × | 8 | 2 | 10 |
| ÷ | 8 | 8 | 16 (not commutative) |
| ^ | 8 | 4 | 12 |
| LOG | 4 | 0 | 4 |
| EXP | 4 | 0 | 4 |

Every cell maps to at least one rule. The collapse rule (R2.3)
significantly reduces table size from 6×6 to 4×4 by eliminating
wrapper-of-dim'less as a distinct type.

---

## 15. Per-rule severity overrides (project policy)

The diagnostic severities listed in §8 are spec **defaults**. Each
project can override them per-rule via the `[diagnostics]` section
of its `.dimfort.toml`:

```toml
[diagnostics]
"D1.7" = "error"      # promote D1.7 from warning to hard error
"D1.6" = "off"        # silence D1.6 (implicit wrapper untag) entirely
"H010" = "error"      # treat ALL H010 warnings as errors
```

Keys may be:

- **Rule markers** (`D1.4`, `D1.5`, `D1.6`, `D1.7`, …) — most
  specific; affects only diagnostics carrying that marker.
- **Diagnostic codes** (`H001`, `H002`, `H010`) — broader; affects
  every diagnostic with that code unless a more specific rule
  marker is also configured.

Values are `"error"`, `"warning"`, or `"off"`. Rule markers take
precedence over codes; both override the spec default.

This decouples *spec opinion* (the rules and their default
severities) from *project policy* (CI strictness, intentional
exp-tagged-space usage, etc.). The same source file may produce
different diagnostic severities under different projects — that's
by design.

---

## References

- `Homogeneity/notes/dimfort-fp-and-limitations.md` — original
  motivating FP classes (EXP(LOG) hydrostatic idiom, `1.+speed`
  regularization).
- `Homogeneity/LMDZ_FINDINGS.md` — findings on LMDZ source that
  surfaced these patterns (especially `cdrag_mod.f90:300` and
  `screenc_mod.f90:115`).
- Design conversation log: 2026-05-20 session.
