# DimFort

Static unit-consistency checker for Fortran. You annotate declarations with the
dimension they should carry, and DimFort verifies that assignments, arithmetic,
intrinsics, and procedure calls all line up. Annotations are written as a
custom Doxygen command, so a single source of truth feeds both the checker and
your generated documentation.

```fortran
real :: velocity  !< @unit{m/s}
real :: mass      !< @unit{kg}
real :: force     !< @unit{kg*m/s^2}

force = mass * velocity            ! diagnosed: force unit is kg, expected kg*m/s^2
```

> Status: **pre-alpha**. The annotation scanner, attachment pass, the
> full H-series checker (H001–H004), intrinsics, user-defined function
> and subroutine calls, derived-type field access, and multi-file
> worksets all work end-to-end through the CLI. The LSP server, the
> on-disk cache, and rational `Pow` exponents in source code are still
> to come.

## Install

Two prerequisites.

### 1. LFortran (parser frontend)

DimFort consumes LFortran's AST/ASR. It is not on PyPI or Homebrew; install
from conda-forge:

```bash
conda create -n lfortran -c conda-forge lfortran -y
```

DimFort discovers `lfortran` from `$PATH`, from `$LFORTRAN_BIN`, or from
`~/miniconda3/envs/lfortran/bin/lfortran` by default. Override with
`--lfortran PATH` on the CLI.

### 2. DimFort itself

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
pip install -e ".[dev,lsp]"
```

Requires Python ≥ 3.11.

## Usage

```bash
dimfort check path/to/project        # check Fortran sources for unit homogeneity
dimfort lsp                          # start the language server (stdio)
dimfort cache info | clean           # manage the on-disk analysis cache
```

Exit code is `0` if no errors, `1` if any error-severity diagnostic was
produced, `2` for usage / file / config errors.

## Doxygen integration

Annotations are read from Doxygen comments (`!>` / `!!` preceding a
declaration, or `!<` trailing it) and apply to every variable in a
declaration list. To make Doxygen render them natively, add one line to
your `Doxyfile`:

```
ALIASES += "unit{1}=\par Unit:^^\1"
```

Module-level constants follow the same notation:

```fortran
!> @brief Gravitational acceleration.
!> @unit{m/s^2}
real, parameter :: g = 9.81
```

See [docs/annotations.md](docs/annotations.md) for the full reference:
unit-expression grammar, continuation-line forms, declaration lists,
and the diagnostic codes the scanner can emit.

## Documentation

- [Annotations](docs/annotations.md)
- [Usage details](docs/usage.md)
- [Language server](docs/lsp.md)
- [Cache format](docs/cache-format.md)
- [Releases](docs/release.md)

## License

See [LICENSE](LICENSE).
