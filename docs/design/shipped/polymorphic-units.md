# Polymorphic units — design

**Status:** implementation complete on `main`; ships in **0.3.0** (not yet
released — the currently published version is 0.2.2.1). The implementation
matches the design below; some Phase 1 limitations are documented inline
(factor unification deferred, symbolic tyvar exponents not supported,
wrapper-typed polymorphic slots fall back to concrete check). For
user-facing how-to material see
[../../reference/polymorphism.md](../../reference/polymorphism.md).

> If you are reading this on a tagged release older than 0.3.0,
> H020 / H021 / H022 / H023 are not yet available in your installed
> version. Upgrade to 0.3.0 (or run from `main`) to use the feature.

Driven by concrete cases encountered during annotation of real-world Fortran
physics codebases.

## Motivation

When annotating a Fortran physics codebase, one recurring pattern can't be
expressed in DimFort's current single-typed annotation system: **generic
aggregation / reduction / interpolation helpers** that operate on whatever unit
the caller passes.

Canonical example (anonymised but representative):

```fortran
SUBROUTINE eff_param(klon, n, x, frac, hatype, agg, Zref)
  REAL, DIMENSION(klon, n), INTENT(IN)  :: x         ! variable to aggregate
  REAL, DIMENSION(klon, n), INTENT(IN)  :: frac      ! weighting fraction [-]
  CHARACTER(LEN=3),         INTENT(IN)  :: hatype    ! 'ARI' | 'INV' | 'CDN'
  REAL, DIMENSION(klon),    INTENT(OUT) :: agg       ! aggregated result
  REAL, OPTIONAL, DIMENSION(klon), INTENT(IN) :: Zref  ! reference height [m]
END SUBROUTINE
```

Different call sites pass `x = z0 {m}`, `x = albedo {1}`, `x = cdrag {1}`,
`x = roughness {m}`, etc. The relationship `x` and `agg` share the same unit
holds universally; `frac` is always `{1}`. Currently DimFort has no way to
express this — the whole interface gets left untyped, and we lose the ability
to propagate constraints through it.

This is a Hindley-Milner-style polymorphism use case. The same shape arises in
weighted-averaging helpers, conservative regridding utilities, and any other
generic numerical glue.

## Proposed feature: parametric polymorphism — `'a` over function signatures

### Syntax

Adopt OCaml-style type variables (`'a`, `'b`, ...) in the `@unit{}` directive:

```fortran
SUBROUTINE eff_param(klon, n, x, frac, hatype, agg, Zref)
  INTEGER, INTENT(IN) :: klon, n
  REAL, DIMENSION(klon, n), INTENT(IN)  :: x      !< @unit{'a}
  REAL, DIMENSION(klon, n), INTENT(IN)  :: frac   !< @unit{1}
  CHARACTER(LEN=3),         INTENT(IN)  :: hatype
  REAL, DIMENSION(klon),    INTENT(OUT) :: agg    !< @unit{'a}
  REAL, OPTIONAL, DIMENSION(klon), INTENT(IN) :: Zref  !< @unit{m}
END SUBROUTINE
```

Multiple variables sharing `'a` are constrained to share their inferred unit at
each call site. Distinct type variables (`'a`, `'b`) within the same signature
are independent.

### Semantics — Hindley-Milner over the unit-algebra

The well-trodden HM rules transfer cleanly to the unit-algebra:

- **Generalization**: at function-definition boundaries, free type variables are
  universally quantified. The function's checked type is
  `∀ 'a. eff_param('a, 1, 'a) : -`.
- **Instantiation**: at each call site, type variables are bound to fresh
  instances. Unify against the actual annotation of each arg.
- **Body checking**: the unit-algebra runs on the type variables exactly as it
  does on concrete units. Operations valid for any unit succeed; operations that
  require specific units (`LOG`, `EXP`, addition with a typed term) add
  constraints that may bind or conflict with `'a`.

### Type variables live *inside* the unit algebra

A type variable `'a` is not an opaque atom that either unifies or doesn't. It is
a **symbolic element of the unit algebra** — the same free abelian group that
holds concrete units. Multiplication, division, and rational exponentiation
compose `'a` and `'b` freely, without forcing them to be equal:

```
'a * 'b        → 'a*'b           (compound, no constraint)
'a / 'b        → 'a*'b^(-1)      (no constraint)
'a^p * 'b^q    → 'a^p * 'b^q     (no constraint; p, q rational or symbolic)
'a * {m}       → 'a*m            (no constraint; 'a unbound)
```

Only operations that already required unit-*equality* on concrete units force
unification of type variables:

```
'a + 'b        → unifies: 'a = 'b
'a + 'a*'b     → unifies: 'a = 'a*'b  ⇒  'b = {1}
MAX('a, 'b)    → unifies: 'a = 'b
'a < 'b        → unifies: 'a = 'b     (returns LOGICAL, {1})
LOG('a)        → LogWrap('a)          (wraps; no constraint on 'a)
LOG('a*'b)     → LogWrap('a*'b)       (wraps; no constraint)
SIN('a)        → binds: 'a = {1}      (no wrap machinery for trig)
```

This is exactly Kennedy's 1996 framing ("Programming Languages and Dimensions",
and the later "Relational Parametricity and Units of Measure"). Unification is
over a **free abelian group**, not over free syntactic terms. It is decidable:
normalize each side to its exponent vector over the basis `(base units, free
type variables)` and solve the resulting linear system over ℚ — Gaussian
elimination, same machinery a units-checker already needs for symbolic
exponents.

What this enables, beyond the canonical `eff_param` example:

```fortran
SUBROUTINE momentum(m, v, p)
  REAL, INTENT(IN)  :: m       !< @unit{'a}
  REAL, INTENT(IN)  :: v       !< @unit{'b}
  REAL, INTENT(OUT) :: p       !< @unit{'a*'b}
END SUBROUTINE
```

Three free parameters, one inferred composite, no spurious cross-arg
constraints. A caller passing `m={kg}`, `v={m/s}` instantiates
`'a={kg}, 'b={m/s}`, yielding `p={kg*m/s}` mechanically.

### Algebra rules with type variables (complete table)

| Operation | Rule |
|---|---|
| `'a * 'b` | `'a*'b` (no constraint) |
| `'a / 'b` | `'a*'b^(-1)` (no constraint) |
| `'a * {1}` | `'a` |
| `'a / 'a` | `{1}` |
| `'a * 'a` | `'a^2` |
| `'a^p`, p literal rational | `'a^p` |
| `'a^p`, p symbolic exponent | `'a^p` (composes with symbolic-exponents) |
| `'a^x`, x non-literal | binds `'a = {1}` (D1.4 unchanged) |
| `SQRT('a)` | `'a^(1/2)` |
| `ABS('a)` | `'a` |
| `'a + 'b` | unifies `'a = 'b` |
| `'a + {U}` (U concrete) | binds `'a = {U}` |
| `MAX('a, 'b)`, `MIN`, `SUM` over `'a`-array | unifies `'a = 'b` / preserves `'a` |
| `'a < 'b`, `==`, comparisons | unifies `'a = 'b`; result `{1}` (LOGICAL) |
| `LOG('a)`, `EXP('a)` | passes through existing LogWrap / ExpWrap rules (R5/R6); type variable flows through unchanged |
| `SIN('a)`, `COS('a)`, `TAN('a)`, `ASIN`, `ATAN`, … (no wrap machinery) | binds `'a = {1}` |

### Body-check examples

For the `'ARI'` (arithmetic-mean) branch of the canonical example:
```
agg = Σ frac * x        ! 'a += {1} * 'a → 'a += 'a → 'a ✓
```
Body checks cleanly. Inferred: `∀ 'a. eff_param('a, 1, 'a) : -`.

For an `'INV'` (inverse-mean) branch:
```
agg = Σ frac * 1/x      ! 'a += {1} * 'a^(-1) → 'a += 'a^(-1)
                        ! requires 'a = 'a^(-1), satisfied only when 'a = {1}
```
This branch forces `'a = {1}` — the polymorphic signature is contradicted by
the body. **H023 fires** (see Diagnostic codes). Same for the `'CDN'` branch
(`log²(x/Zref)` forces `'a = {m}`).

The fix is to split into per-mode subroutines, each with an honest signature:
`eff_param_ari` keeps `∀ 'a. (…) : -`, while `eff_param_inv` and
`eff_param_cdn` carry concrete signatures `(1, …) : -` and `(m, …, m) : -`
respectively. Runtime dispatch on the mode discriminator moves to the caller,
which is also where it semantically belongs (the caller knows which mode it
wants; the dispatching `SELECT CASE` is local to the call site).

Dependent / mode-refinement typing — letting `'a` depend on a runtime
discriminator value — is a strictly more powerful alternative that is parked
as a future direction. Not in scope here.

### Implementation cost

Substantial. The feature needs:
- Type-variable representation in the unit-AST (already partially symbolic via
  the existing Exponent machinery — generalize to symbolic units).
- A unification algorithm over the unit-algebra (multiplication / division /
  exponentiation lattice). Specifically AG-unification (Kennedy 1996):
  normalize each side to its exponent vector over the basis `(base units, free
  type variables)` and solve the resulting linear system over ℚ. Reuses
  the rational-exponent machinery already in place for symbolic exponents.
- Generalization at function-definition boundaries (scope handling), with the
  letrec-style fixpoint pass for recursive groups.
- Instantiation at call sites (substitution).
- Per-call-site binding-history tracking: every per-arg contribution to each
  type variable is recorded so that H020 can render symmetric "collides with"
  diagnostics.
- H023 check at function-definition: any reachable body path that forces a
  binding on a quantified `'a` fires the signature-contradiction error.
- Pretty-printing for polymorphic signatures in the project's hover convention
  (`∀ 'a. name(…) : ret`), including the inline `'a = m` row form in the call
  hover tree.
- Caching: no changes to the per-file cache *model*. Polymorphic signatures
  serialize into the cache as a richer term language (encoding quantifiers,
  `'a^p`, `'a*'b`, etc.) on top of today's concrete-signature serialization.
  Tyvar names are canonicalized to declaration order (`'a, 'b, …`) before
  hashing so a rename refactor doesn't trigger spurious cache misses. Per-call
  instantiations are not cached separately (cheap inline computation).

Estimated effort: a multi-week feature, on the order of the scale (Phase 2a
affine) implementation. Designed as a coherent unit; not piecemeal.

## Soundness & scope

The single most important property of this feature is that it stays **local and
auditable**. Specifically:

### Generalize / instantiate at function boundaries only

Type variables are introduced at — and quantified over — a single function's
signature. The body is checked once, in the polymorphic context. Each call site
then independently instantiates the quantified variables to fresh unknowns and
unifies them against the actual arguments' units. **A caller's instantiation
never leaks back to constrain the function itself, nor sideways to other call
sites.**

In particular, this rules out a whole-program constraint store of the kind
where every use of every variable contributes equations to one global system.
That approach is theoretically appealing — for a non-recursive call graph it is
even decidable and sound — but its solver size grows with program size and its
diagnostics lose locality (a single fire can be the consequence of a chain
spanning hundreds of unrelated files). On real-world Fortran physics codebases
this becomes unbearable, both for the checker and for the human reading its
output. DimFort instead stays modular: each function is a checked island, each
call site is a checked edge, no global solver.

### Type variables exist only in function signatures

`'a` is meaningful only where it is quantified. In practice that means:

- **Allowed**: dummy argument declarations on `SUBROUTINE` / `FUNCTION`
  interfaces; the function's result-variable declaration; local variables of
  the function body (treated as fresh constraint targets during that single
  body check, not generalized further); `@unit_assume` directives inside the
  function body (subject to the usual discipline — registry entry, mandatory
  reason, irreducible-only).
- **Not allowed**: module-level variable declarations; `COMMON` blocks;
  `SAVE`'d locals or named `PARAMETER`s; `TYPE` (derived-type) component
  declarations.

A `'a` appearing outside the allowed positions is a hard parse-level error —
the annotation is meaningless if there is no enclosing quantifier.

### Signatures are fixed by the function body, not by its callers

A polymorphic function's signature — the set of quantified type variables and
the relationships between them — is determined by checking its body once, in
isolation. Each call site then instantiates those quantified variables against
the actual argument units present at that site, exactly as standard HM does.

What this rules out is the *reverse* direction: collecting facts across many
call sites and feeding them back to tighten the function's signature
("every observed caller passes `{kg}`, therefore `'a` is really `{kg}`"). The
function's signature does not depend on its callers, and one caller's
instantiation does not propagate sideways to constrain another caller.
Anything that lets callers tighten the signature would collapse parametricity.

This is what keeps the feature local. Calls between polymorphic functions are
fine — `f`'s `'a` can be passed straight into a `'b`-slot of `g`, and the call
site unifies them — because that unification happens at one specific point in
one specific function's body, not in a global store.

### Recursion

Recursion is a separate concern from cross-function transitivity. A polymorphic
function that calls itself needs letrec-style treatment: the recursive call
instantiates to the *same* set of type variables currently being generalized,
not to fresh ones. Standard HM handles this; the implementation needs a
fixpoint pass over the recursive group. Mutually recursive polymorphic
functions are handled in the same group.

### What this buys us

Three concrete consequences:

1. **Order-independent checking.** Checking the body of `f` does not depend on
   knowing `f`'s callers. Checking a caller does not depend on knowing other
   callers of the same function. This composes cleanly with the per-file cache.
2. **Diagnostics stay readable.** A unification failure at a call site names
   the function, its (small) polymorphic signature, and the offending actual
   argument. No backtrace through a global constraint graph.
3. **Refactoring stays safe.** Adding or removing a call site cannot retro-fire
   diagnostics elsewhere, because no constraints flowed out of it.

## Composition with existing features

### Symbolic exponents

Composes cleanly — `'a` and a symbolic exponent `p` are both rational-coefficient
elements of the same algebra, so they combine without machinery. A polymorphic
signature can quantify over both simultaneously:

```fortran
FUNCTION pow_law(x, p) RESULT(y)
  REAL, INTENT(IN) :: x   !< @unit{'a}
  REAL, INTENT(IN) :: p   !< @unit{1}
  REAL             :: y   !< @unit{'a^p}
  y = x ** p
END
```

Signature: `∀ 'a ∀ p. pow_law('a, 1) : 'a^p`. Two distinct quantifier classes
(unit variable + symbolic exponent) at one boundary. The AG-unification
machinery and the existing symbolic-exponent representation speak the same
language (rational exponents over a basis), so this is genuinely free.

Pretty-printing needs care: `'a^p` must render readably in hovers and CLI
output (handled by the same `format_unit` path that already prints `m^p`).

### LogWrap / ExpWrap

No special case. The LogWrap rules (R5.1, R5.2, R5.4, R6) operate on whatever
unit sits inside the wrapper — concrete or symbolic doesn't matter. In
particular, the standard `exp(log(a) ± log(b))` trick works under polymorphism
without any new machinery:

```
log('a) + log('b)        → LogWrap('a) + LogWrap('b)   [R5.1]
                         → LogWrap('a * 'b)
exp(LogWrap('a * 'b))    → 'a * 'b                      [R6]
```

This is a small triumph for the AG-unification framing: every existing
algebraic rule that already operated on unit expressions automatically lifts to
polymorphic unit expressions, because both inhabit the same algebra.

### Scale Phase 2a (affine units)

Type variables range over the **multiplicative unit algebra only** — the free
abelian group of base-unit powers. Affine units (`degC`, `degF`) inhabit a
separate layer that does not participate in multiplication, exponentiation, or
self-addition, so they cannot be elements of the algebra `'a` ranges over.

Consequently, a call site that tries to bind a `'a` slot to an affine actual
argument fails to unify with a clear message:

```
cannot bind type variable 'a to affine unit {degC};
convert to {K} at the call site, or pass as a delta type.
```

This is not a special-case restriction — it falls straight out of where
type variables live. Polymorphism over affine units (a separate `'a` flavor
ranging over the affine layer) is imaginable but defers cleanly: the affine
operation set is so narrow (`affine + delta`, `affine - affine`) that the body
of a hypothetical affine-polymorphic function would have almost no useful work
to do, and a caller needing it can write specialised versions for `K` and
`degC` cheaply. Future extension if real demand surfaces; not in scope here.

### `@unit_assume`

Allowed inside a polymorphic function's body subject to the usual discipline
(registry entry, mandatory reason, irreducible-only) — see the Soundness &
scope section. A polymorphic `@unit_assume 'a` is a *weaker* assertion than a
concrete `@unit_assume {U}` (it claims the expression matches the function's
polymorphic type, which every instantiation already validates), and the same
auditability rules govern it. Concrete plausible use: a polymorphic empirical
fit whose RHS has a non-rational exponent on a `'a`-typed input.

### Unannotated variables (U005)

A U005 variable passed into a `'a` slot at a call site contributes no unit
information to the unification — same way it contributes none to a concrete
call today. The `'a` instantiation simply stays unbound at that site, and no
fire results. Symmetrically, the polymorphic function never "learns" anything
about its `'a` from a U005-passing caller (per the signatures-fixed-by-body
rule). U005 and polymorphism are orthogonal.

## What this feature does NOT solve — and why we shouldn't extend it to cover

There's a tempting adjacent feature: a "polymorphic constant" notation
(e.g. `@unit{any}`) that would match any type at its use sites — useful for
sentinel values (undefined-value markers) and zero-array placeholders passed to
differently-typed slots of dispatch routines.

**Reject this extension.** A polymorphic-constant marker that propagates no
constraints is functionally a silent bypass of the unit checker. A typo on a
regular variable would silently disable checking for that variable. Worse, it
flips the auditability of these legitimate-but-unusual values from "visible as
`! unit pending`" to "invisible as `{any}`" — strictly worse for review.

The existing discipline rule (annotate as `! unit pending` with rationale)
keeps these values visible as untyped and forces a comment explaining why.
That's the right semantic: "we deliberately leave this untyped because
the value is polymorphic-by-design."

## Diagnostic codes

The feature introduces four new hard-error codes. Existing codes (D1.4, U005,
U020, etc.) are reused unchanged where the fire shape pre-dates polymorphism.

### H020 — Polymorphic call-site unification failure

Fires when a call to a polymorphic function constrains the same type variable
to inconsistent units across multiple argument slots.

Hover (tree form) on a call where `'a` is forced to `m` by `roughness` and to
`kg` by `out_kg`:

```
🔴 DimFort

eff_param(roughness, frac, out_kg)  :  -  🔴
├── roughness  :  'a = m      🔴      (collides with arg 3: out_kg)
├── frac       :  1           🟢
└── out_kg     :  'a = kg     🔴      (collides with arg 1: roughness)
```

CLI form:

```
H020: type variable 'a bound to inconsistent units at this call site
  ∀ 'a. eff_param('a, 1, 'a) : -
  arg 1  roughness  : 'a = m       (collides with arg 3: out_kg)
  arg 2  f          : 1
  arg 3  out_kg     : 'a = kg      (collides with arg 1: roughness)
```

The diagnostic is **symmetric**: every row that contributes a conflicting
binding goes red and names its partner(s). No "first arg wins"
asymmetry — unification has no ordering, and the conflict belongs equally to
all contributing sites. For N-way collisions among 3+ rows, list all partners
(`(collides with args 1, 4: a, d)`).

Implementation requires the checker to collect every per-arg binding
contribution per type variable, detect conflicts after the collection, and
mark all contributing rows red. One extra pass; the data is already being
computed.

### H021 — Type variable used outside an allowed position

Fires at parse time when `'a` appears in a position where it cannot be
quantified.

```
H021: type variable 'a cannot appear in a module-level variable declaration
  module-level vars require concrete units; only function signatures
  may quantify over 'a.
```

Triggered for: module variables, `COMMON` blocks, `SAVE`'d locals, named
`PARAMETER`s, `TYPE` component declarations, and any `@unit{'a}` not inside a
`SUBROUTINE`/`FUNCTION` interface scope (the function body and dummy args are
fine).

### H022 — Cannot bind type variable to affine unit

Fires at a call site when the actual argument carries an affine unit (`degC`,
`degF`) and would need to bind a `'a` slot.

Hover:

```
🔴 DimFort

eff_param(T_celsius, frac, out)  :  -  🔴
├── T_celsius  :  'a = degC   🔴      (affine unit cannot bind 'a)
├── frac       :  1           🟢
└── out        :  'a = K      🟢
```

CLI form:

```
H022: cannot bind 'a to affine unit degC
  ∀ 'a. eff_param('a, 1, 'a) : -
  arg 1  T_celsius  : 'a = degC    (affine unit cannot bind 'a)
  fix: convert degC to K at the call site, or pass as a delta.
```

Separate from H020 because the cause and the fix are categorically different
(layer mismatch, not algebraic conflict). Type variables range over the
multiplicative unit algebra only; affine units inhabit a separate layer.

### H023 — Polymorphic signature contradicted by function body

Fires at function definition when any reachable body path forces a binding on
a quantified type variable. The signature is dishonest: it claims polymorphism
the body does not deliver.

```
H023: function body forces 'a = 1 — signature is not actually polymorphic
  ∀ 'a. eff_param('a, 1, 'a) : -
  branch 'INV':  agg = SUM(frac / x)
                 requires 'a = 'a^(-1), satisfied only when 'a = 1

  fix: either rewrite the signature with a concrete unit in place of 'a,
       or split this branch into a separate non-polymorphic subroutine.
```

The rule is strict: *any* body path that constrains a quantified `'a` to a
concrete unit fires H023. There is no warning-level form. The strictness is
deliberate — a polymorphic signature is a promise to callers ("works for any
unit"), and a body that breaks the promise on some path is a footgun the
checker should refuse to silently approve.

The recommended fix (split per-mode subroutines) is also better code: explicit
dispatch in the caller beats runtime-mode-as-CHARACTER inside one over-broad
function. Dependent / mode-refinement typing — letting `'a` depend on a
runtime discriminator value — is the strictly more expressive future
alternative; parked as future work, not in scope here.

### Reused codes

| Code | Pre-polymorphism behavior | Under polymorphism |
|---|---|---|
| `D1.4` (internal) | Non-rational exponent on dimensioned base | Unchanged — also fires for `'a^x`, x non-literal |
| `U005` | Unannotated variable used | Unchanged — orthogonal to polymorphism (see Composition section) |
| `U020` | `@unit_assume` INFO | Unchanged — also covers `@unit_assume 'a` |
| `H001`/`H002` | Unit mismatch on assignment / arithmetic | Unchanged — fires inside the polymorphic body during local checking |

### Why no whole-program "constraint conflict" code

There is no code for "constraint chain across multiple call sites produces an
inconsistency" because the design rules out such chains (Soundness & scope §
Signatures-fixed-by-body). Every polymorphism fire is a local fire at one call
site or in one function body.

## Decisions

Recorded here so the rationale survives the design-discussion thread:

- **Syntax: `'a`.** OCaml/F#/Coq convention. Short, visually distinct from
  concrete units (which never start with `'`), and physicists comfortable with
  `α, β` mathematical notation read `'a, 'b` as the same indexing convention.
  Greek-letter aliases (`@unit{α}` ↔ `@unit{'a}`) considered and rejected as
  parser surface area for marginal aesthetic gain.
- **Surfacing: standard hover convention, extended.** Definition hovers use
  `∀ 'a. name(slot1, slot2, …) : ret` — the `∀` prefix marks polymorphism
  without disrupting the existing format. Call hovers use the existing tree
  form with inline `'a = m` on every `'a`-slot row (free info on success,
  essential on failure). Side panel and CLI follow the same conventions.
  Multiple type variables render as `∀ 'a ∀ 'b` (one quantifier per variable).
- **Caching: naive is sufficient.** No changes to the per-file cache model.
  Polymorphic signatures serialize into the cache with tyvar names
  canonicalized to declaration order to avoid spurious misses on rename
  refactors. Per-call-site instantiations are not cached.
- **Error UX: symmetric `(collides with …)` trailer.** When a type variable is
  bound to inconsistent units across argument slots, every contributing row
  goes red and names its partner(s). No "first arg wins" asymmetry —
  unification has no ordering and the conflict belongs equally to all sites.
  The trailer wording (`collides with`, not `expected`) deliberately avoids
  collision with the existing concrete-signature mismatch convention.
- **Strict signatures.** A polymorphic signature whose body forces a binding
  on any reachable path fires H023; no warning-level form exists. The
  recommended fix (split into per-mode subroutines) is also better code.
- **Parametric modules: out of scope.** Type variables live only in function
  signatures. Module-level polymorphism is not pursued (compilation-model
  alignment, audit locality, low practical demand — see Soundness & scope §
  "Type variables exist only in function signatures").
- **Mode-refinement / dependent typing: parked as future work.** The
  strictly-more-expressive alternative to H023's split-the-function fix,
  letting `'a` depend on a runtime discriminator value. Not in scope here.

## Provenance

Idea crystallised during a multi-cycle annotation campaign in early 2026,
where the same shape recurred across a half-dozen generic surface-aggregation
helpers. The OCaml-style framing was settled as the natural fit during design
review. Specific candidate instances are tracked in the project's annotation
working notes (not in this repo).
