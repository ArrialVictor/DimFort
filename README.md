# DimFort

![preview](https://raw.githubusercontent.com/ArrialVictor/DimFort/main/social_preview.png)

[![release](https://github.com/ArrialVictor/DimFort/actions/workflows/release.yml/badge.svg)](https://github.com/ArrialVictor/DimFort/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/ArrialVictor/DimFort/blob/main/LICENSE)
[![Python: 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://github.com/ArrialVictor/DimFort/blob/main/pyproject.toml)

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

> Status: **pre-alpha**. End-to-end, these work today: the annotation
> scanner, attachment pass, the full H-series checker (H001–H004 +
> H010), the unit-algebra wrapper rules for `LOG` / `EXP`-tagged
> quantities (D1.2 – D1.6), per-rule provenance traces, intrinsics,
> user-defined function and subroutine calls, derived-type field
> access, rational `**` exponents, multi-file worksets, a workspace-
> aware LSP server with live-edit diagnostics, hover (compact or
> full-tree trace mode), inlay hints, go-to-definition, code lens,
> code actions, completion, and a CLI that accepts files or
> directories.

## Install

DimFort is published on PyPI as `dimfort`. It's a CLI tool with an
entry point — install it isolated with [`pipx`](https://pipx.pypa.io/):

```bash
pipx install 'dimfort[lsp]'      # includes the LSP server extra
dimfort --version
```

The `[lsp]` extra pulls in `pygls`; omit it if you only want the
CLI and never the language server.

> **On macOS with Homebrew Python**: PEP 668 makes plain
> `pip install` refuse to touch Homebrew's site-packages. Use
> `pipx` as shown above, or install into a virtual environment.
> If `pipx install` says "command not found", run
> `brew install pipx && pipx ensurepath` first.

### From source (contributors)

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,lsp]"
```

### Requirements

Python ≥ 3.11. The Fortran parser
([tree-sitter-fortran](https://pypi.org/project/tree-sitter-fortran/))
is a runtime dependency installed automatically — no external
compiler or subprocess needed for parsing. For `.F90` files using
CPP `#`-directives DimFort shells out to the system `cpp` if
`[parser] cpp_defines` or `[parser] include_paths` are set in
`.dimfort.toml`.

## Usage

```bash
dimfort check path/to/file.f90       # check a single file
dimfort check path/to/project/       # walk a directory recursively
dimfort check path/...  --summary    # also print a per-file H/U count table
dimfort lsp                          # start the language server (stdio)
```

Exit code is `0` if no errors, `1` if any error-severity diagnostic was
produced, `2` for usage / file / config errors. Warnings alone do not
fail the run.

Diagnostic codes split into two families:

- **H-series** (`H001`–`H004`, `H010`) — homogeneity violations: the
  math doesn't balance dimensionally. `H010` is a warning (the rest
  are errors) and covers the implicit-cast / wrapper-untag cases
  (D1.5, D1.6) where DimFort accepts the expression but flags a
  smell. Wrapper-arithmetic violations (D1.2 / D1.3 / D1.4) surface
  via the same `H001` / `H002` codes with a `(D1.x)` tag in the
  message.
- **U-series** (`U001`, `U002`, `U005`–`U007`, `U010`, `U-conflict`) —
  annotation / metadata problems: something's wrong with the
  annotations themselves, not the math.

Full reference: [docs/usage.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/usage.md).
The wrapper-rule specification — including the rule IDs surfaced by
`--trace` — lives at
[docs/unit-algebra.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/unit-algebra.md).

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

For quantities that live in log or exp space, wrap the inner unit
with `LOG(...)` or `EXP(...)`:

```fortran
real :: lp     !< @unit{LOG(Pa)}
real :: tau    !< @unit{EXP(K)}
```

DimFort tracks the wrapper through arithmetic: `LOG(psol) + LOG(pref)`
types as `LOG(Pa²)`, `LOG(p1) − LOG(p2)` collapses to dimensionless
via the pressure-ratio rule, and `EXP(LOG(psol) − ...)` cancels back
to `Pa`. The full rule set is in
[docs/unit-algebra.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/unit-algebra.md).

### Trace mode

Pass `--trace` to see the rule chain behind each diagnostic:

```bash
dimfort check --trace src/cdrag_mod.f90
```

Each error / warning prints the firing rule IDs (`R3.1`, `R5.6`, …)
under the message. The VSCode extension toggles the same view in
hover: run **DimFort: Toggle Full Unit Trace in Hover** from the
Command Palette and any hover inside an assignment shows an ASCII
tree of the RHS expression with the rule chain on each node.

See [docs/annotations.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/annotations.md)
for the full reference: unit-expression grammar, continuation-line
forms, declaration lists, and the diagnostic codes the scanner can
emit.

## Documentation

- [Annotations](https://github.com/ArrialVictor/DimFort/blob/main/docs/annotations.md)
- [Usage details](https://github.com/ArrialVictor/DimFort/blob/main/docs/usage.md)
- [Language server](https://github.com/ArrialVictor/DimFort/blob/main/docs/lsp.md)
- [Releases](https://github.com/ArrialVictor/DimFort/blob/main/docs/release.md)

## Editor integrations

Thin LSP clients that wire `dimfort lsp` into common editors. Each
lives in its own repository, releases on its own cadence, and shares
the same feature surface (diagnostics, hover, inlay hints,
go-to-definition, code actions, completion).

- **[VSCode](https://github.com/ArrialVictor/DimFort-VSCompanion)** —
  on the [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=arrialvictor.dimfort-vscode)
  (`ext install arrialvictor.dimfort-vscode`) and on
  [Open VSX](https://open-vsx.org/extension/dimfort/dimfort-vscode)
  for VSCodium / Cursor / Theia / code-server.
- **[Neovim](https://github.com/ArrialVictor/DimFort-NvimCompanion)**
  (≥ 0.11) — install via any plugin manager pointing at the repo.
- **[Emacs](https://github.com/ArrialVictor/DimFort-EmacsCompanion)** —
  install via straight.el / use-package or manual `require`. Works
  with both eglot (Emacs 29+) and lsp-mode.

## License

See [LICENSE](https://github.com/ArrialVictor/DimFort/blob/main/LICENSE).
