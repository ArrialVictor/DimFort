# Usage

## Install

LFortran (parser frontend) — install once from conda-forge:

```bash
conda create -n lfortran -c conda-forge lfortran -y
```

DimFort:

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
pip install -e ".[dev,lsp]"
```

Requires Python ≥ 3.11.

## CLI

```bash
dimfort check <paths>...        # check Fortran sources for unit homogeneity
dimfort lsp                     # start the language server (stdio)
dimfort cache info | clean      # inspect or clear the analysis cache
```

`check` flags:

| Flag                | Effect                                            |
|---------------------|---------------------------------------------------|
| `-q`, `--quiet`     | Suppress diagnostic output; only return an exit code. |
| `--no-color`        | Disable ANSI colour (also auto-disabled outside a TTY, or when `NO_COLOR` is set). |
| `--lfortran PATH`   | Path to the `lfortran` binary (overrides `$LFORTRAN_BIN` and the conda default). |
| `--no-cache`        | Bypass the on-disk analysis cache for this run. *(cache not yet implemented)* |
| `--cache-dir PATH`  | Override the cache directory (default: `./.dimfort/cache`). *(cache not yet implemented)* |

`cache` subcommands:

| Command             | Effect                                            |
|---------------------|---------------------------------------------------|
| `dimfort cache info`  | Show cache location, entry count, total size. |
| `dimfort cache clean` | Remove the entire cache directory.            |

Exit codes:

| Code | Meaning |
|------|---------|
| `0`  | No errors. |
| `1`  | At least one error-severity diagnostic emitted. |
| `2`  | Usage error, missing file, or invalid config. |

Warnings alone do not fail the run.

## Annotating your sources

DimFort recognises units written as `@unit{…}` inside Doxygen comments
attached to declarations. The full reference — including continuation
lines, declaration lists, and the diagnostic codes — lives in
[annotations.md](annotations.md). Minimal example:

```fortran
real :: velocity      !< @unit{m/s}
real :: mass          !< @unit{kg}

!> @brief Surface gravity.
!> @unit{m/s^2}
real, parameter :: g = 9.81
```

Make Doxygen render the annotations alongside the rest of your
documentation by adding one line to your `Doxyfile`:

```
ALIASES += "unit{1}=\par Unit:^^\1"
```

## Status

Pre-alpha. Working pipeline pieces:

- annotation scanner (`@unit{…}` extraction, all placement forms)
- attachment (annotations → variables, with U010 enforcement)
- semantic checker for **H001** (assignment mismatch), **H002**
  (additive / same-unit-intrinsic operand mismatch), **H003**
  (dimensionless-intrinsic violation), and **H004** (user-defined
  function/subroutine argument mismatch), plus a useful subset of
  Fortran intrinsics (`sqrt`, `abs`, `exp`, `log`, trig family,
  `min`/`max`/`mod`/`merge`, `dot_product`/`matmul`,
  `sum`/`minval`/`maxval`, the kind-conversion family)
- derived-type field access (`b%v`) both as a read and as an
  assignment target, with field annotations declared inside the
  `type :: …` block
- multi-file worksets: `dimfort check a.f90 b.f90 c.f90` compiles any
  module files first (in dependency order, via a retry-loop) so
  `use` statements resolve, then aggregates unit tables and function
  signatures across files before checking each one
- end-to-end CLI: `dimfort check FILE [FILE …]` runs the full pipeline
  and reports diagnostics in `file:line: severity: code message` form

Not yet implemented: rational `Pow` exponents (`m^(1/2)` in source),
the LSP server, and the on-disk cache's read/write paths (only
`cache info` / `cache clean` work).

Treat anything not listed above as unimplemented.
