# Your first check

This page walks through running DimFort end-to-end on a small
sample file so you can see what an annotated source looks like,
what a diagnostic reads like, and how to act on one.

The sample is `demos/tour.f90` in the DimFort repository — a short
self-contained Fortran routine drawn from textbook moist
thermodynamics. It exercises the most common DimFort diagnostics
in one place.

## Get the sample

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
```

`demos/tour.f90` is the file we will check.

## Run the check

```bash
dimfort check --scale demos/tour.f90
```

`--scale` enables the opt-in [S-series](../reference/diagnostic-codes.md#s-series--scale-opt-in)
checks (same dimension, different magnitude or zero-point) — useful
on a first read because they catch unit-system mismatches that
dimension-only checks miss.

You'll see output like:

```
demos/tour.f90:42:7: error H001 — assignment LHS unit kg, RHS unit kg·m
demos/tour.f90:55:12: warning H010 (D1.5) — implicit cast of literal 0.5 to kg/m
demos/tour.f90:81:3: warning S001 — operands disagree on magnitude: hPa vs Pa
…
```

[`demos/README.md`](https://github.com/ArrialVictor/DimFort/blob/main/demos/README.md)
walks through the file line by line so you can match each diagnostic
back to the source.

## Reading a diagnostic

Every line has the same shape:

```
<file>:<line>:<col>:  <severity>  <code>  —  <message>
```

- **Code prefix** identifies the family: `H` for homogeneity, `U`
  for annotation problems, `S` for scale, `X` for cross-site, `P`
  for parser-skipped regions. The full catalog is at
  [reference/diagnostic-codes.md](../reference/diagnostic-codes.md).
- **Severity** is `error`, `warning`, or `info`. Only errors fail
  the run (exit code 1). Warnings and info print but do not block.
- **D-class tags** like `(D1.5)` on H-series messages identify the
  unit-algebra rule that fired. The rule reference is
  [reference/unit-algebra.md](../reference/unit-algebra.md).

## Inspect the rule chain

For wrapper-arithmetic diagnostics (`LOG(Pa)`, `EXP(K)` and so on)
the message alone may not say enough. Re-run with `--trace`:

```bash
dimfort check --scale --trace demos/tour.f90
```

Every diagnostic now carries the rule chain that led to it (`R3.1`,
`R5.6`, …) printed under the message. The same trace is available
in the editor hover at `hover: "detailed"` — see
[editor-integration/hover-ui.md](../editor-integration/hover-ui.md).

## Annotate your own file

The annotation grammar is just `@unit{…}` inside a Doxygen comment:

```fortran
real :: velocity      !< @unit{m/s}
real :: mass          !< @unit{kg}
real :: kinetic_e     !< @unit{kg*m^2/s^2}

kinetic_e = 0.5 * mass * velocity ** 2   ! checks
```

Full grammar (continuation lines, derived types, intrinsics):
[reference/annotations.md](../reference/annotations.md).

If your project already documents units in inline prose (`! [m/s]`
style), don't rewrite every declaration — DimFort can be configured
to read your existing convention. See
[bringing DimFort to an existing codebase](bringing-to-existing-codebase.md).

## Next steps

- [Bringing DimFort to an existing codebase](bringing-to-existing-codebase.md)
- [`dimfort.toml` reference](../reference/dimfort-toml.md)
- [Editor integration](../editor-integration/)
