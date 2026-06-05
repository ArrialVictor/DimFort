# Polymorphic units (`'a`, `'b`, …)

> **Availability:** shipped in **0.3.0** (2026-06-06). Upgrade via
> `pip install --upgrade dimfort` (PyPI), or install the matching
> VSCode / Open VSX / Neovim / Emacs / Helix companion.

A polymorphic function works for any unit. DimFort lets you declare that
with a single OCaml-style type variable in `@unit{...}`:

```fortran
subroutine avg(x, y, out)
  real, intent(in)  :: x    !< @unit{'a}
  real, intent(in)  :: y    !< @unit{'a}
  real, intent(out) :: out  !< @unit{'a}
  out = 0.5 * (x + y)
end subroutine
```

`'a` is a placeholder for "whatever unit the caller passes." The
function above accepts two `kg` and returns a `kg`; or two `m` and
returns a `m`; or two `Pa` and returns a `Pa` — DimFort checks each
call site against the signature and propagates the answer.

The design rationale, complete algebra rules, and compositional
semantics live in [../design/shipped/polymorphic-units.md](../design/shipped/polymorphic-units.md). This
page is the practical reference: what you can write, what DimFort
will tell you, and what to do when it complains.

## When to reach for `'a`

Use a type variable when **two or more declarations of one function
share the same unit no matter what the caller passes**. Typical
shapes:

| Pattern | Signature |
|---|---|
| Identity / pass-through | `'a → 'a` |
| Generic averaging / interpolation | `('a, 'a, …) → 'a` |
| Per-tracer transport | `(q: 'a, masse: kg, w: kg/s, …) → 'a` |
| Bilinear product | `(m: 'a, v: 'b) → 'a*'b` |
| Diagnostic min/max wrapper | `(qmin: 'a, qmax: 'a, field: 'a)` |

Do **not** use `'a` when the function is genuinely tied to one
specific unit. If the body computes `temperature * gas_constant`, the
result is always energy per mole — a concrete `J/mol`, not a tyvar.

## Syntax

A type variable is a name starting with `'` followed by a lower-case
identifier: `'a`, `'b`, `'tracer`. Names are local to one signature;
two functions can each use `'a` independently.

Tyvars compose with the existing unit algebra:

```fortran
real, intent(in)  :: q       !< @unit{'a}
real, intent(in)  :: masse   !< @unit{kg}
real, intent(out) :: u_mq    !< @unit{'a*kg}    ! product
real, intent(out) :: tend    !< @unit{'a/s}     ! tendency
real, intent(out) :: dq2     !< @unit{'a^2}     ! squared
real, intent(out) :: sqrt_q  !< @unit{'a^(1/2)} ! rational power
```

Multiple distinct tyvars in one signature work the same way — they're
independent:

```fortran
subroutine momentum(m, v, p)
  real, intent(in)  :: m  !< @unit{'a}      ! mass
  real, intent(in)  :: v  !< @unit{'b}      ! velocity
  real, intent(out) :: p  !< @unit{'a*'b}   ! product
end subroutine
```

## Where you can put a `'a`

Allowed positions:

- Dummy arguments on `subroutine` / `function` interfaces
- The result variable of a `function`
- Local variables inside a `subroutine` / `function` body

Forbidden positions — fire **H021** at parse time:

- Module-level / file-level variable declarations
- `PARAMETER` declarations
- Components of a derived-type definition
- `SAVE`'d local variables (both `real, save :: x` and standalone `save :: x`)
- Variables listed in a `COMMON` block

The reason: a tyvar is only meaningful when there is an enclosing
quantifier (a function signature). A module-level variable has no
caller to instantiate `'a`, so the annotation has no semantic content.

## How DimFort checks polymorphic code

Each function is checked **independently**, in two phases.

**Body check.** Inside the function the tyvars are opaque
generators — `'a` is whatever the caller will eventually pass. Body
operations that preserve `'a` succeed silently (`'a + 'a → 'a`,
`'a * concrete → 'a*concrete`, `sqrt('a) → 'a^(1/2)`). Operations
that would **force a binding** on a tyvar fire **H023**:

| Body pattern | Forced binding | Fix |
|---|---|---|
| `'a + concrete` / `'a-typed lhs = concrete-typed rhs` | `'a = concrete` | Make the operation honest: either type the operand polymorphically or remove `'a` from the signature |
| `sin('a)`, `log('a)` directly forced to dim'less | `'a = {1}` | Drop `'a` for this branch, or split into mode-specific subroutines |
| `'a ** non_literal_exponent` | `'a = {1}` | Pass the base as `{1}` explicitly, or type the exponent so the result stays polymorphic |
| `'a + 'a^(-1)` | `'a = {1}` | The two branches need different signatures; split the function |

H023 is **strict** — there is no warning form. A polymorphic
signature is a promise to callers; a body that breaks the promise on
some path is a footgun the checker refuses to silently approve. The
recommended fix is also better code: explicit per-mode dispatch in
the caller beats one over-broad function whose body branches on a
runtime mode discriminator.

**Call-site check.** At each call to a polymorphic function, DimFort
instantiates the tyvars to fresh unknowns and unifies them against
the actual arguments' units. If every constrained tyvar gets one
consistent value, the call succeeds. If two argument slots imply
different values for the same tyvar, **H020** fires:

```
H020: Call to 'avg': type variable 'a bound to inconsistent units at
this call site
  arg 1 (x):   'a = m   (collides with arg 2 (y))
  arg 2 (y):   'a = kg  (collides with arg 1 (x), arg 3 (out))
  arg 3 (out): 'a = m   (collides with arg 2 (y))
```

The trailer is **symmetric**: every contributing row names every other
row that disagreed. Unification has no ordering and the conflict
belongs equally to all sites.

Affine units (e.g. `degC`, with a non-zero zero-point offset) cannot
bind a tyvar — type variables range over the *multiplicative* unit
algebra only. Trying to pass a `degC` value into an `'a` slot fires
**H022** with a fix hint to convert to the base unit (`K`) or pass as a
delta.

## Recursion

Self-recursive polymorphic functions work without any extra ceremony.
A recursive call that passes the function's own tyvar-typed args to
itself is the identity instantiation — DimFort's per-slot net
coefficient (`formal_tyvar − actual_tyvar`) sees them cancel out, the
tyvar stays unbound, and the equation `σ(formal) = formal = actual`
is trivially satisfied.

A recursive call that passes a **concrete** value into a tyvar slot
binds the tyvar to that concrete unit at that site only — independent
of the slot's instantiation in the calling instance.

## `@unit_assume` with `'a`

A polymorphic `@unit_assume` is permitted inside a polymorphic body
(same registry discipline as the concrete form: every site gets an
entry in the project's `@unit_assume` registry with a reason). It
emits **U020** INFO at the site and suppresses any D1.4 the RHS
would otherwise raise:

```fortran
real, intent(in)  :: x  !< @unit{'a}
real, intent(out) :: y  !< @unit{'a}
y = 2.0 * x  !< @unit_assume{'a : polymorphic empirical fit, scale-invariant}
```

Use this for irreducible polymorphic operations (non-rational power
on a `'a`-typed input, for instance). For expressible operations
that just happen to escape the existing algebra rules, fix the
underlying issue instead.

## Phase 1 limitations

These are real but documented; some surface as
`UnsupportedPolymorphism` falling back to the concrete-check path:

- **Symbolic tyvar exponents** (e.g. `'a^kappa`). Currently rejected
  in unification; falls back to per-slot dim check.
- **`Unit.factor` unification**. A `g/kg` actual into an `'a` slot
  also bound to `kg/kg` elsewhere is silently accepted; only the SI
  dim is checked. A future Phase 2 will add a multiplicative
  sub-system.
- **Wrapper-typed polymorphic slots** (`LOG('a)`, `EXP('a)` in
  signatures). Currently fall back to concrete equality check;
  inference under the wrapper would require unification under
  `LogWrap` / `ExpWrap`.
- **Polymorphism over affine units** (a hypothetical `'a` ranging
  over `degC`-style offset units). Out of scope by design — see the
  design doc for the rationale.

## Hover surfacing

LSP signature hovers for polymorphic functions prefix the rendering
with `∀` for each declared tyvar, in sorted order:

```
∀ 'a. avg(? : 'a, ? : 'a) : 'a
∀ 'a. ∀ 'b. momentum(? : 'a, ? : 'b, ? : 'a*'b) : -
```

Concrete signatures are unaffected — no `∀` prefix when no tyvar
appears.

## See also

- [Diagnostic codes](diagnostic-codes.md) — H020, H021, H022, H023 reference
- [Unit algebra](unit-algebra.md) — D-class taxonomy + LogWrap/ExpWrap
- [../design/shipped/polymorphic-units.md](../design/shipped/polymorphic-units.md) — design rationale + complete algebra rules table
