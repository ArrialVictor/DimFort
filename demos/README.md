# DimFort tour

A five-minute, hands-on look at what DimFort sees in a small Fortran
file. The single source file in this directory — [`tour.f90`](tour.f90)
— is the canonical demo: README screenshots, talks, and editor-companion
walkthroughs are all taken from it, so the output below stays
reproducible.

If you just want to see DimFort work, run:

```bash
dimfort check --scale demos/tour.f90
```

Then read on for a line-by-line walkthrough of what each diagnostic
(and each *silence*) is telling you.

## What `tour.f90` contains

A short moist-thermodynamics routine: a handful of state variables
(temperature, pressure, density, gas constant), the ideal-gas law, an
empirical power-law behind an `@unit_assume` escape hatch, a
deliberate homogeneity bug, and one numerically-stable log-space
computation that exercises the `LOG(…)` / `EXP(…)` wrapper algebra
end to end. The variables read as textbook physics — `T`, `p`, `rho`,
`v`, `R_d` — so you don't need to know any particular codebase to
follow along.

## Line-by-line tour

| Line(s) | What's happening | What DimFort emits |
|--------:|------------------|--------------------|
| 14–25   | Declarations carry `@unit{…}` annotations. One declaration (`r_drop`) is intentionally left unannotated. | Nothing yet — annotations are just metadata at this stage. |
| 29–31   | Pure-literal initialisation (`T = 273.15`, etc.). | **Silent** — rule **R4.4** autocasts the literal to the LHS unit. The whole point of R4.4 is that pure-literal initialisation needs no extra ceremony. |
| 34      | Ideal gas law: `rho = p / (R_d * T)`. Units balance to `kg/m^3`. | **Silent** — the homogeneity check passes. |
| 39      | `p_hpa = p` — same dimension, different magnitude. | **S001** (warning): same dimension `kg·m⁻¹·s⁻²` but the magnitudes differ by ×100. Only fires under `--scale`. |
| 42      | `v = p / rho` — speed assigned a `m²/s²` value. | **H001** (error): `m·s⁻¹ ≠ m²·s⁻²`. Classic homogeneity bug, caught at the assignment. |
| 23, 50  | `r_drop` is declared without `@unit{}` and read inside a unit-checked expression on line 50. | **U005** (warning) on the *declaration* (line 23) — DimFort points at where the annotation is missing, not where it would have been used. |
| 50      | Empirical power-law fit: `(...)**(-0.922)` — a dimensioned quantity raised to a non-rational exponent. DimFort cannot derive a unit here (**D1.4**), so the line carries an `@unit_assume{kg/m^3 : empirical-fit power-law}` to assert the result. | **U020** (info): `RHS unit assumed kg·m⁻³ (empirical-fit power-law)`. Audit-only — never affects the exit code. The D1.4 fire is suppressed because derivation is short-circuited. |
| 67      | `p_ratio = exp(log(p) - log(p_ref))` — the numerically-stable form of `p / p_ref`, written entirely in log space. | **Silent** — and this is the most interesting silence in the file. See below. |

## The log-space round-trip (line 67)

Most static checkers will refuse `log(p)` when `p` has a unit, because
`log` is normally a strictly-dimensionless intrinsic (it'd fire
**H003**). DimFort treats `log` and `exp` as **homomorphisms** on the
unit lattice instead — `log` promotes a unit into the `LOG(…)` wrapper,
`exp` strips it — so the round-trip below type-checks cleanly:

```
log(p)               : Pa            →  LOG(Pa)        (R3.x — log homomorphism)
LOG(p)  −  LOG(p_ref)               →  LOG(Pa / Pa)   (R5.2 — subtraction in log space)
LOG(Pa / Pa)                         →  LOG(1)  →  1   (dimensionless collapse)
exp(1)                               →  1              (EXP ∘ identity)
```

End to end: `exp(log(p) - log(p_ref))` types as dimensionless, matching
the LHS, with no annotations needed beyond the LHS unit and no escape
hatch. The same algebra handles the product form
`exp(log(a) + log(b))` (which types as `a · b`) and the cancellation
`exp(LOG(Pa))` (which types as `Pa`). Full rule set in
[`docs/unit-algebra.md`](../docs/unit-algebra.md) §R5.

## Expected `dimfort check` output

Captured with `dimfort 0.2.0` on the current `main`:

```
$ dimfort check --scale --no-color demos/tour.f90
demos/tour.f90:23: warning: U005 'r_drop' is used in a unit-checked expression but has no @unit{} annotation (e.g. used at line 50)
demos/tour.f90:39: warning: S001 Scale mismatch: same dimension (kg·m⁻¹·s⁻²) but the magnitudes differ by ×100. If this is a unit conversion, carry the factor on a typed PARAMETER; otherwise the units disagree in scale.
demos/tour.f90:42: error: H001 Assignment unit mismatch: m·s⁻¹ ≠ m²·s⁻²
demos/tour.f90:50: info: U020 RHS unit assumed kg·m⁻³ (empirical-fit power-law)
$ echo $?
1
```

Four diagnostics, one error → exit code `1`. Drop `--scale` and S001
goes away (three diagnostics, still exit `1` because H001 stands).

## In an editor

The same file, opened in an editor that runs `dimfort lsp` through one
of the companions ([VSCode](https://github.com/ArrialVictor/DimFort-VSCompanion),
[Neovim](https://github.com/ArrialVictor/DimFort-NvimCompanion),
[Emacs](https://github.com/ArrialVictor/DimFort-EmacsCompanion)), shows
the same diagnostics inline plus hovers, inlay hints, and the side
panel. The shots below are taken from `tour.f90` directly — when they
are refreshed, they come from this file so the README stays in sync
with what a reader sees if they open it themselves.

<!--
  Screenshot slots. Capture from `demos/tour.f90` so the shots stay
  traceable. Filenames follow `docs/img/tour-*` and use the existing
  light/dark `<picture>` convention.
-->

### Hover on the homogeneity error (line 42)

> _Placeholder — screenshot to be captured from `tour.f90` line 42._
> Expected: `Detailed` hover on the `=` of `v = p / rho`, showing the
> unit-algebra tree with `m·s⁻¹` on the LHS row, `m²·s⁻²` on the RHS
> row, and a 🔴 marker on the assignment.

### Hover on the `@unit_assume` row (line 50)

> _Placeholder — screenshot to be captured from `tour.f90` line 50._
> Expected: RHS row carries the `(assumed: empirical-fit power-law)`
> annotation and a 🔵 overlay; the assignment row itself stays 🟢
> because homogeneity passes against the declared LHS unit.

### Hover on the log-space round-trip (line 67)

> _Placeholder — screenshot to be captured from `tour.f90` line 67._
> Expected: `Detailed` hover showing the full rewrite chain
> `log(Pa) → LOG(Pa)`, `LOG(Pa) − LOG(Pa) → LOG(Pa/Pa) → LOG(1) → 1`,
> `exp(1) → 1`, with a 🟢 marker on the assignment. This is the demo
> shot that shows DimFort doing something other checkers can't.

## Want a single-page error tour?

A companion `demos/broken.f90` — several failure modes side by side,
without the prose — is a likely follow-up. Not shipped in the first
cut; track it via the issue list.
