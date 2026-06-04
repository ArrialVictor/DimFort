# Unit annotations

DimFort reads unit information from a custom Doxygen command,
**`@unit{…}`**, placed inside standard Doxygen comments on Fortran
variable declarations. Annotations are recognised by both DimFort (for
homogeneity checking) and Doxygen (for documentation rendering), so
you maintain a single source of truth.

## Syntax

```fortran
real :: velocity  !< @unit{m/s}
```

The unit expression follows a small grammar:

| Element        | Examples                                | Notes |
|----------------|-----------------------------------------|-------|
| Base unit      | `m`, `kg`, `s`, `K`, `A`, `mol`, `cd`   | The seven SI base units. |
| Derived unit   | `N`, `J`, `W`, `Pa`, `Hz`, `C`, `V`, `Ohm`, `T`, `rad`, `sr` | Shipped as defaults; extensible. |
| Prefix         | `km`, `ms`, `ns`, `MPa`, `kJ`, …        | Base units take all standard SI prefixes by default. Derived units must opt in. |
| Product        | `kg*m/s^2`                              | `*` and `/` are left-associative. |
| Power          | `m^2`, `m^-1`, `m^(1/2)`                | Integer or rational exponents. No decimals. |
| Grouping       | `kg/(m*s)`                              | Use parentheses to disambiguate. |
| Dimensionless  | `1`                                     | Also `rad`, `sr` by convention. |
| Log wrapper    | `LOG(Pa)`, `LOG(LOG(K))`                | Tags a value as residing in log space. `LOG ∘ EXP` and `EXP ∘ LOG` cancel; `LOG(1)` collapses to `1`. |
| Exp wrapper    | `EXP(K)`, `EXP(EXP(s))`                 | Symmetric to `LOG(...)`. Lower-case (`log(Pa)` / `exp(K)`) is accepted; the pretty-printer emits uppercase. |

Whitespace inside `{…}` is allowed and stripped: `@unit{  m / s  }`
is identical to `@unit{m/s}`. Same for the wrapper grammar:
`@unit{LOG( Pa )}` is identical to `@unit{LOG(Pa)}`. Inverse pairs
cancel on parse — `@unit{EXP(LOG(Pa))}` is the same annotation as
`@unit{Pa}`. The full rule set (cancellation, dim'less collapse,
homomorphisms) is in [docs/unit-algebra.md](unit-algebra.md).

Two slashes at the same paren depth (e.g. `kg/m*s`) produce a
**`UnitAmbiguityWarning`** — the expression has a defined meaning
(left-to-right) but the reader can't be sure which one you meant.
Parenthesise.

## Where to put the annotation

Annotations attach to **the declaration**, in one of two positions.

### Trailing (`!<`)

After the declaration, on the same statement:

```fortran
real :: mass            !< @unit{kg}
real :: pi = 3.14159    !< @unit{1}
```

### Preceding (`!>` or `!!`)

In a Doxygen block immediately above the declaration:

```fortran
!> @brief Gravitational acceleration at Earth's surface.
!> @unit{m/s^2}
real, parameter :: g = 9.81
```

`!>` starts a Doxygen block; `!!` continues one. Both work for the
preceding-block form, and either can carry the `@unit{…}`.

Both positions are first-class — use whichever reads better for a
given declaration.

## Declaration lists

A single annotation applies to **every variable in the list**:

```fortran
real :: x, y, z         !< @unit{m}
! → x, y, z all have unit m
```

If the variables in a list have *different* units, split the
declaration into separate statements. A future `--strict-declist` flag
will diagnose multi-name lists with a single annotation
(diagnostic **U011**); it is not yet implemented.

## Continuation lines (`&`)

For declarations broken across multiple physical lines, the
annotation may appear in any of three positions:

```fortran
! Form A: preceding Doxygen block
!> @unit{m/s}
real :: alpha, &
        beta,  &
        gamma

! Form B: trailing on the LAST line
real :: alpha, &
        beta,  &
        gamma   !< @unit{m/s}

! Form C: trailing on the FIRST line (ending the line with `&`)
real :: alpha, &   !< @unit{m/s}
        beta,  &
        gamma
```

All three apply the unit to *every* variable in the declaration
(`alpha`, `beta`, `gamma` above).

### Forbidden: `!<` on an intermediate continuation line

A trailing annotation on a *middle* line of a continued declaration is
**rejected** with diagnostic **U010** and the unit is *not* applied:

```fortran
real :: alpha, &
        beta,  &  !< @unit{m/s}   ← U010 — neither first nor last
        gamma
```

The position suggests per-variable scope, which DimFort doesn't
support. Move the annotation to the first or last line, or split the
declaration into separate statements.

## Module constants

Use the same notation as for local variables. Either form is fine:

```fortran
module physical_constants
  implicit none

  !> @brief Gravitational acceleration at Earth's surface.
  !> @unit{m/s^2}
  real, parameter :: g = 9.81

  real, parameter :: pi = 3.14159265   !< @unit{1}
end module
```

## Doxygen rendering

To make Doxygen recognise `@unit{…}` as a documented field rather than
unknown text, register the alias in your `Doxyfile`:

```
ALIASES += "unit{1}=\par Unit:^^\1"
```

After this, Doxygen renders `@unit{m/s}` as a "Unit:" line in the
variable's generated docs. DimFort and Doxygen now share the exact
same source — no duplication.

## Escape hatch: `@unit_assume`

Some expressions can't be analysed dimensionally at all — most commonly
**empirical power-law fits** that raise a dimensioned quantity to a
non-rational exponent. The Brandes-2007 snow-density law is the canonical
case:

```fortran
real :: r_snow    !< @unit{m}
real :: rho_snow  !< @unit{kg/m^3}
! r_snow^(-0.922) has no representable dimension → D1.4
rho_snow = 1.e3*0.178*(r_snow*2.*1000.)**(-0.922)   !< @unit_assume{kg/m^3 : empirical-fit Brandes 2007}
```

`@unit_assume{ <unit> : <reason> }` is a **statement-level** directive
(write it as a trailing `!<` on the assignment). It tells the checker to
**stop deriving** that assignment's RHS — suppressing the D1.4 and any
interior fire — and instead treat the result as the asserted `<unit>`.

- **It suppresses derivation, not consistency.** The asserted unit is
  still checked against a *declared* LHS unit, so an assume that
  contradicts the variable's `@unit{}` still fires **H001** — it can
  never mask a real conflict. To propagate the unit downstream, annotate
  the variable's declaration as usual; the assume only governs *this*
  statement.
- **`reason` is mandatory** — a short category (`empirical-fit`,
  `scale-pun`, `legacy-const`, …) plus free text. Every assumption is
  therefore both greppable (`grep -rn @unit_assume`) and visible in the
  check output as a **`U020`** INFO note. INFO never affects the exit code.
- This is *not* a way to silence genuine mismatches — reach for it only
  when DimFort fundamentally cannot represent the unit (non-rational
  exponents, empirical fits). Prefer a typed PARAMETER or a real fix
  everywhere else.

> v1 keys assumes by source line, which is exact for raw-parsed files.
> A `.F90` file whose lines shift under `cpp` preprocessing is a known
> limitation (the assume may not align with the expanded statement).

## Verified conversion: `@unit_affine_conversion`

A multiplicative conversion can ride on a typed PARAMETER
(`play[Pa] / PA_PER_HPA[Pa/hPa]` resolves to `hPa`). An **affine** (offset)
conversion — `°C ↔ K` — cannot: addition preserves the frame, and there is
no unit you can add that turns a `degC` into a `K`. So a correct
`t_k = t_c + 273.15` would fire `S002` (offset mismatch) with no way to
bless it. `@unit_affine_conversion` is that blessing — and, because DimFort
*knows* both offsets, it is **verified**, not trusted:

```fortran
real :: t_c   !< @unit{degC}
real :: t_k   !< @unit{K}
real, parameter :: RTT = 273.15  !< @unit{K}
t_k = t_c + RTT   !< @unit_affine_conversion{degC -> K}
```

`@unit_affine_conversion{ <src> -> <tgt> }` is a **statement-level**
directive (trailing `!<` on the assignment; `{src, tgt}` with a comma is an
accepted synonym). DimFort checks the assignment actually performs the
`src → tgt` conversion (target frame on the LHS, RHS affine-linear in one
`src` operand with the *exact* offset/factor arithmetic):

- **Valid ⇒ silent.** The `S002` the statement would raise is suppressed,
  and the result is cleanly the target frame.
- **Invalid ⇒ `S003` (error).** Wrong direction, wrong constant, wrong
  target, a non-affine (multiplicative) pair like `{Pa -> hPa}`, or a
  non-affine-linear RHS — each reports *how* the arithmetic is off.
- **Not an `@unit_assume`.** That directive is *trusted* and for the
  irreducible (and lives in `UNIT_ASSUME_REGISTRY.md`); this one is
  *verified* and needs **no registry entry** — the check is its
  justification. Use `@unit_assume` only when DimFort fundamentally can't
  represent the unit; use `@unit_affine_conversion` for °C↔K conversions.
- **Opt-in.** Like the rest of the scale family it only fires under
  `scale_mode` (`.dimfort.toml [scale] enabled = true` or `--scale`).

The cleanest idiom is a small conversion **function** whose one body line
carries the directive — callers then get a clean typed `degC → K` signature.

## Diagnostics

Annotation-time problems surface as **U-series** codes (`U001`
malformed, `U006` orphan, `U-conflict` two annotations disagreeing,
`U010` `!<` on an intermediate continuation line, `U020`
`@unit_assume` audit note, …). The semantic checker adds the
**H-series** (`H001`–`H004`, `H010`), and `scale_mode` adds the
**S-series** (`S001`–`S003`).

The full table — every code, severity, and trigger — lives at
[reference/diagnostic-codes.md](diagnostic-codes.md). Per-code
severity can be remapped per project under `[diagnostics]` in
`.dimfort.toml`.

Intrinsics handled:

| Category          | Intrinsics                                              | Unit semantics |
|-------------------|---------------------------------------------------------|----------------|
| Dimensionless     | `exp`, `log`, `log10`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `sinh`, `cosh`, `tanh` | arg must be `1`; result is `1`. H003 on violation. |
| Transforming      | `sqrt`, `abs`                                           | result is `arg^(1/2)` for sqrt, `arg^1` for abs. |
| Transparent       | `floor`, `ceiling`, `nint`, `int`, `real`, `dble`, `sign`, `aimag`, `anint` | result = first arg's unit. |
| Same-unit args    | `min`, `max`, `mod`, `modulo`, `merge`                  | every arg shares one unit (merge: only first two); result is that unit. H002 on mismatch. |
| Product           | `dot_product`, `matmul`                                 | result = `arg[0] * arg[1]`. |
| Reduction         | `sum`, `minval`, `maxval`                               | result = element unit. |

User-defined functions and subroutines are now checked, including
across files. Their unit interface is inferred from the annotations on
their declared formal arguments and the result variable:

```fortran
function box_area(side) result(out)
  real, intent(in) :: side    !< @unit{m}
  real :: out                 !< @unit{m^2}
  out = side * side
end function
```

A call site is checked against this signature: each actual argument
must have the same unit as the corresponding formal (or be unknown),
and the call's resolved unit becomes the formal return unit (used by
the surrounding H001 check). When the called routine lives in a
different file, pass both files to `dimfort check` on the same command
line — the orchestrator compiles modules first (in dependency order)
and aggregates signatures across the whole workset. v1 keys signatures
by the bare function name — two functions with the same name in
different scopes are not disambiguated; last definition wins.

### Derived-type fields

Annotate fields exactly like local variables, inside the type block:

```fortran
type :: particle
  real :: m       !< @unit{kg}
  real :: q       !< @unit{C}
  real :: v(3)    !< @unit{m/s}
end type
```

Both `%`-access reads (`tot = b%m`) and writes (`b%m = mass`) are
checked. Field annotations live in their own scope-aware table, so a
local variable named `m` and a field named `m` don't collide — they
can carry independent units.

v1 limitation: field lookup is keyed by `(bare_type_name, field_name)`.
Two derived types in different modules that share a name are not
disambiguated — last definition wins.

Rational `**` exponents in source code are now handled: literal
real-valued exponents close to a "nice" rational with denominator ≤
100 (e.g. `0.5` → `1/2`, `0.3333…` → `1/3`) are decoded and used as a
fractional exponent. Exponents that don't match a nice rational (e.g.
`0.314`) still resolve to "unknown unit" and the surrounding check is
silently skipped — this is intentional, since irrational exponents on
non-dimensionless units have no physical meaning.
