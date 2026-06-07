# Multi-file demo

A small four-file program that demonstrates DimFort across a `use`
chain. The single-file [`tour.f90`](../tour.f90) covers the per-line
diagnostic surface; this directory exercises **cross-file** behaviour
— shared modules, coverage aggregation, and the side-panel's
file-vs-workspace stats segment.

## Layout

| File | Role |
| --- | --- |
| [`constants_mod.f90`](constants_mod.f90) | Shared physical constants (g, R_dry, c_p, …). Fully annotated. |
| [`pressure_clean.f90`](pressure_clean.f90) | Pressure routines, fully annotated and dimensionally consistent. `use`s `constants_mod`. |
| [`pressure_broken.f90`](pressure_broken.f90) | Mirror of the clean module with three deliberate problems (U005, H002, and a silent physical bug). `use`s `constants_mod`. |
| [`driver.f90`](driver.f90) | Top-level program that `use`s all three modules. Largest workset of the four when opened. |

## What DimFort sees

The "workset" for a given active file is its transitive `use`-closure
plus the active file itself. Opening different files therefore
exercises worksets of different sizes:

| Active file | Workset | Workspace size |
| --- | --- | --- |
| `constants_mod.f90` | `{constants_mod}` | 1 file |
| `pressure_clean.f90` | `{pressure_clean, constants_mod}` | 2 files |
| `pressure_broken.f90` | `{pressure_broken, constants_mod}` | 2 files |
| `driver.f90` | `{driver, pressure_clean, pressure_broken, constants_mod}` | 4 files |

In the editor side panel's coverage stats bar, that translates to a
visibly different `WS:` segment as you switch between tabs — most
sharply when moving between `constants_mod` (1-file workset, so
`File == WS`) and `driver` (4-file workset, so `WS` aggregates over
all the cross-file errors `pressure_broken` introduces).

## Walk it on the CLI

A workset-wide check (pass the directory so DimFort discovers every
file in it; pointing at a single file gives U007 because the
`use`d modules wouldn't be in the workset):

```bash
dimfort check demos/multifile/
```

Per-file coverage breakdown across the whole directory:

```bash
dimfort coverage demos/multifile/
```

The clean and constants modules report 100% coverage; the broken
module reports considerably less (~70% on the latest build); the
driver picks up a propagated U005 from the broken module and reports
just under 100%. The workset total sums everything line-weighted.
