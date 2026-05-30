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
empirical power-law behind an `@unit_assume` escape hatch, and a
deliberate homogeneity bug — plus a short detour into DimFort's
`LOG(…)` / `EXP(…)` wrapper algebra. The variables read as textbook
physics — `T`, `p`, `rho`, `v`, `R_d` — so you don't need to know any
particular codebase to follow along.

## Line-by-line tour

| Line(s) | What's happening | What DimFort emits |
|--------:|------------------|--------------------|
| 14–28   | Declarations carry `@unit{…}` annotations. One declaration (`r_drop`) is intentionally left unannotated. | Nothing yet — annotations are just metadata at this stage. |
| 32–34   | Pure-literal initialisation (`T = 273.15`, etc.). | **Silent** — rule **R4.4** autocasts the literal to the LHS unit. The whole point of R4.4 is that pure-literal initialisation needs no extra ceremony. |
| 37      | Ideal gas law: `rho = p / (R_d * T)`. Units balance to `kg/m^3`. | **Silent** — the homogeneity check passes. |
| 42      | `p_hpa = p` — same dimension, different magnitude. | **S001** (warning): same dimension `kg·m⁻¹·s⁻²` but the magnitudes differ by ×100. Only fires under `--scale`. |
| 45      | `v = p / rho` — speed assigned a `m²/s²` value. | **H001** (error): `m·s⁻¹ ≠ m²·s⁻²`. Classic homogeneity bug, caught at the assignment. |
| 23, 53  | `r_drop` is declared without `@unit{}` and read inside a unit-checked expression on line 53. | **U005** (warning) on the *declaration* (line 23) — DimFort points at where the annotation is missing, not where it would have been used. |
| 53      | Empirical power-law fit: `(...)**(-0.922)` — a dimensioned quantity raised to a non-rational exponent. DimFort cannot derive a unit here (**D1.4**), so the line carries an `@unit_assume{kg/m^3 : empirical-fit power-law}` to assert the result. | **U020** (info): `RHS unit assumed kg·m⁻³ (empirical-fit power-law)`. Audit-only — never affects the exit code. The D1.4 fire is suppressed because derivation is short-circuited. |
| 61      | `dlnp = lnp - lnp_ref` — subtraction of two `LOG(Pa)` values. | **Silent** — the log homomorphism rewrites `LOG(Pa) − LOG(Pa)` to `LOG(Pa/Pa) = LOG(1) = 1`, matching the dimensionless LHS. |
| 62      | `p_back = exp(lnp)` — `exp` applied to a `LOG(Pa)` value. | **Silent** — `EXP ∘ LOG` cancels, so the RHS types as `Pa` and matches the LHS. |

## Expected `dimfort check` output

Captured with `dimfort 0.2.0` on the current `main`:

```
$ dimfort check --scale --no-color demos/tour.f90
demos/tour.f90:23: warning: U005 'r_drop' is used in a unit-checked expression but has no @unit{} annotation (e.g. used at line 53)
demos/tour.f90:42: warning: S001 Scale mismatch: same dimension (kg·m⁻¹·s⁻²) but the magnitudes differ by ×100. If this is a unit conversion, carry the factor on a typed PARAMETER; otherwise the units disagree in scale.
demos/tour.f90:45: error: H001 Assignment unit mismatch: m·s⁻¹ ≠ m²·s⁻²
demos/tour.f90:53: info: U020 RHS unit assumed kg·m⁻³ (empirical-fit power-law)
$ echo $?
1
```

Four diagnostics, one error → exit code `1`. Drop `--scale` and S001
goes away (three diagnostics, still exit `1` because H001 stands).

The two log-pressure lines (61 and 62) deserve a callout: they
produce **no diagnostic** even though `exp` is normally a strictly
dimensionless intrinsic. That's the LOG/EXP wrapper algebra at work
— `LOG(Pa) − LOG(Pa)` collapses to dimensionless via the log
homomorphism, and `exp(LOG(Pa))` cancels back to `Pa`. The
[unit-algebra spec](../docs/unit-algebra.md) has the full rule set
(R5.x) if you want the details.

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

### Hover on the homogeneity error (line 45)

> _Placeholder — screenshot to be captured from `tour.f90` line 45._
> Expected: `Detailed` hover on the `=` of `v = p / rho`, showing the
> unit-algebra tree with `m·s⁻¹` on the LHS row, `m²·s⁻²` on the RHS
> row, and a 🔴 marker on the assignment.

### Hover on the `@unit_assume` row (line 53)

> _Placeholder — screenshot to be captured from `tour.f90` line 53._
> Expected: RHS row carries the `(assumed: empirical-fit power-law)`
> annotation and a 🔵 overlay; the assignment row itself stays 🟢
> because homogeneity passes against the declared LHS unit.

### Hover on the LOG-wrapper subtraction (line 61)

> _Placeholder — screenshot to be captured from `tour.f90` line 61._
> Expected: `Detailed` hover showing the rewrite `LOG(Pa) − LOG(Pa)`
> → `LOG(Pa/Pa)` → `LOG(1)` → `1`, with a 🟢 marker — the algebra
> step that lets the assignment type-check silently.

## Want a single-page error tour?

A companion `demos/broken.f90` — several failure modes side by side,
without the prose — is a likely follow-up. Not shipped in the first
cut; track it via the issue list.
