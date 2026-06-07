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
deliberate homogeneity bug, one numerically-stable log-space
computation that exercises the `LOG(…)` / `EXP(…)` wrapper algebra
end to end, and a tiny internal function — `dyn_p` (dynamic pressure)
— that's called with a mismatched argument so DimFort can catch a
cross-procedure unit error. The variables read as textbook physics —
`T`, `p`, `rho`, `v`, `R_d` — so you don't need to know any
particular codebase to follow along.

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
| 75      | `e_sat = dyn_p(T, rho)` — first formal expects `m/s`, actual is `T` (`K`). | **H004** (error): `Call to 'dyn_p': argument 1 (spd) unit mismatch: expected m·s⁻¹, got K`. The kind of bug that can't be caught at the call statement by intra-statement reasoning — DimFort matches formal-to-actual unit by position. |
| 79–86   | The function body is dimensionally clean: `0.5 * dens * spd**2` types to `kg·m⁻¹·s⁻² = Pa`, matching the declared return unit. | **Silent** — the body is correct; the bug is at the *call site* on line 75, not in the function. |

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
[`docs/unit-algebra.md`](../docs/reference/unit-algebra.md) §R5.

## Expected `dimfort check` output

Captured with `dimfort 0.2.0` on the current `main`:

```
$ dimfort check --scale --no-color demos/tour.f90
demos/tour.f90:23: warning: U005 'r_drop' is used in a unit-checked expression but has no @unit{} annotation (e.g. used at line 50)
demos/tour.f90:39: warning: S001 Scale mismatch: same dimension (kg·m⁻¹·s⁻²) but the magnitudes differ by ×100. If this is a unit conversion, carry the factor on a typed PARAMETER; otherwise the units disagree in scale.
demos/tour.f90:42: error: H001 Assignment unit mismatch: m·s⁻¹ ≠ m²·s⁻²
demos/tour.f90:50: info: U020 RHS unit assumed kg·m⁻³ (empirical-fit power-law)
demos/tour.f90:75: error: H004 Call to 'dyn_p': argument 1 (spd) unit mismatch: expected m·s⁻¹, got K
$ echo $?
1
```

Five diagnostics, two errors → exit code `1`. Drop `--scale` and S001
goes away (four diagnostics, still exit `1` because H001 and H004
stand).

### Explain the algebra: `--trace`

Pass `--trace` to attach the firing rule chain to each diagnostic.
For the H001 line above:

```
$ dimfort check --trace --scale --no-color demos/tour.f90
...
demos/tour.f90:42: error: H001 Assignment unit mismatch: m·s⁻¹ ≠ m²·s⁻²
  trace:
    → kg·m⁻¹·s⁻², kg·m⁻³  ⇒  m²·s⁻²   [R4.2]
...
```

The trace says: the RHS divided a `kg·m⁻¹·s⁻²` operand by a `kg·m⁻³`
operand, producing `m²·s⁻²` via rule **R4.2** (the quotient rule of
unit algebra). That's enough to tell you the mismatch isn't a typo in
an annotation — it's a real physical-units error. The same rule IDs
surface in the editor companions on hover, with the depth selectable
per-surface (`Short` / `Detailed`) via the `DimFort: Hover` settings.
The full rule reference lives in
[`docs/unit-algebra.md`](../docs/reference/unit-algebra.md).

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

Detailed hover on the `=` of `v = p / rho`: the unit-algebra tree
carries `m·s⁻¹` on the LHS row, `m²·s⁻²` on the RHS row, and a 🔴
marker propagates up to the assignment.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-h001-line42_dark.png">
  <img width="640" src="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-h001-line42_light.png" alt="Detailed hover on tour.f90 line 42 — H001 assignment mismatch m·s⁻¹ ≠ m²·s⁻²">
</picture>

### Hover on the `@unit_assume` row (line 50)

The RHS row carries the `(assumed: empirical-fit power-law)`
annotation and a 🔵 overlay; the assignment row itself stays 🟢
because homogeneity passes against the declared LHS unit.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-assume-line50_dark.png">
  <img width="640" src="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-assume-line50_light.png" alt="Detailed hover on tour.f90 line 50 — U020 RHS unit assumed kg·m⁻³ with the assignment row still 🟢">
</picture>

### Hover on the log-space round-trip (line 67)

Detailed hover showing the full rewrite chain
`log(Pa) → LOG(Pa)`, `LOG(Pa) − LOG(Pa) → LOG(Pa/Pa) → LOG(1) → 1`,
`exp(1) → 1`, with a 🟢 marker on the assignment. The demo shot that
shows DimFort doing something other checkers can't.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-logexp-line67_dark.png">
  <img width="640" src="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-logexp-line67_light.png" alt="Detailed hover on tour.f90 line 67 — log/exp rewrite chain collapsing to dimensionless, every row 🟢">
</picture>

### Hover on the call-site mismatch (line 75)

Detailed hover on the call showing the formal/actual pairing —
`spd : m/s` (formal) vs `T : K` (actual) with a 🔴 marker on the
offending argument, plus the green row for `rho` whose unit matches.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-h004-line75_dark.png">
  <img width="640" src="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/tour-hover-h004-line75_light.png" alt="Detailed hover on tour.f90 line 75 — H004 call-site mismatch with T:K passed where spd:m/s was expected">
</picture>

## Other demo files

Two short companion files live next to this one for specific
scenarios that don't fit the prose tour:

### [`demos/affine.f90`](affine.f90) — the scale family

Focused on `--scale` mode and the affine-conversion story:

- **S001** — magnitude (factor) mismatch, no offset issue.
- **S002** — un-blessed offset mismatch (`degC + K`).
- **`@unit_affine_conversion`** — the **verified** counterpart to
  `@unit_assume`. A small `c_to_k` conversion function whose body
  carries the directive type-checks silently because DimFort verifies
  the arithmetic actually performs the stated `degC → K` conversion.
- **S003** — what happens when the directive is there but the
  arithmetic is wrong (subtraction instead of addition). The error
  message even shows the `a*s+b` form DimFort solved for.

```
$ dimfort check --scale --no-color demos/affine.f90
demos/affine.f90:33: warning: S001 Scale mismatch: same dimension (kg·m⁻¹·s⁻²) but the magnitudes differ by ×1/100. …
demos/affine.f90:38: warning: S002 Offset mismatch: same dimension and scale but a different zero-point (offsets differ by -273.15, e.g. °C vs K) — add the conversion or keep units consistent
demos/affine.f90:64: error: S003 Affine-conversion directive does not verify: the degC -> K arithmetic is wrong: the RHS computes a*s+b with a=1, b=-273.15, but the conversion requires a=1, b=273.15
```

### [`demos/broken.f90`](broken.f90) — the bug zoo

One-block-per-code lookup table. No prose, no narrative — each block
is a single statement that fires exactly one code, with the message
DimFort produces. Use it as a quick "what does H002 look like?"
reference:

```
$ dimfort check --no-color demos/broken.f90
demos/broken.f90:19: warning: U005 'r' is used in a unit-checked expression but has no @unit{} annotation (e.g. used at line 37)
demos/broken.f90:22: error: H001 Assignment unit mismatch: m·s⁻¹ ≠ m
demos/broken.f90:25: error: H002 Operand unit mismatch in '+'/'-': m ≠ s (D1.1)
demos/broken.f90:28: error: H003 Intrinsic 'sin' requires a dimensionless argument; got m
demos/broken.f90:31: error: H004 Call to 'require_seconds': argument 1 (s_arg) unit mismatch: expected s, got m
demos/broken.f90:34: warning: H010 Implicit cast: literal '2.0' to m (prefer a named PARAMETER, e.g. `REAL, PARAMETER :: <name> = 2.0   !< @unit{m}`)
```

### [`demos/multifile/`](multifile/) — cross-file `use` chain

A four-file program (shared constants module + clean and broken
pressure modules + a driver) demonstrating how DimFort behaves across
a `use` chain: workset discovery, cross-file diagnostics, and
coverage aggregation. Useful for exercising the side panel's
file-vs-workspace stats segment, which differs visibly as you switch
between tabs of different workset sizes. See
[`demos/multifile/README.md`](multifile/README.md) for the walk.
