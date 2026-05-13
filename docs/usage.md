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
| `--no-cache`        | Bypass the on-disk analysis cache for this run.   |
| `--cache-dir PATH`  | Override the cache directory (default: `./.dimfort/cache`). |

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

Pre-alpha. The annotation scanner and attachment pass are working;
the semantic checker that produces H001–H004 diagnostics is being
implemented. Treat anything not listed in this file as unimplemented.
