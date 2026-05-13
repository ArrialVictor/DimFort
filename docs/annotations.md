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

Whitespace inside `{…}` is allowed and stripped: `@unit{  m / s  }`
is identical to `@unit{m/s}`.

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

## Diagnostics produced at annotation time

| Code        | Severity | Meaning |
|-------------|----------|---------|
| (malformed) | error    | `@unit{` with no closing `}`, empty `@unit{}`, or more than one `@unit{…}` on one comment line. |
| (orphan)    | warning  | An annotation that doesn't sit on or before a known declaration. |
| (conflict)  | error    | The same variable received two different unit annotations (e.g. `!>` block disagrees with `!<` trailing). |
| **U010**    | error    | `!<` on an intermediate line of an `&`-continued declaration; annotation is rejected. |

The semantic checker layers add the **H-series** on top:

| Code  | Severity | Meaning |
|-------|----------|---------|
| H001  | error    | Assignment LHS unit doesn't match RHS unit. |
| H002  | error    | `+` / `-` operands, or same-unit intrinsic args (`min`, `max`, `mod`, …) have different dimensions. |
| H003  | error    | Intrinsic that requires a dimensionless argument (`exp`, `log`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `sinh`, `cosh`, `tanh`, `log10`) given something else. |
| H004  | error    | User-defined function or subroutine-call argument unit mismatch. |

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

Rational `Pow` exponents in source code (`area ** 0.5`) are still
resolved to "unknown unit"; checks on the surrounding expression are
silently skipped.
