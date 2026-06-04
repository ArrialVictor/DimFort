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

> Status: **beta**. Usable, tested, and proven against real-world
> Fortran — but the `@unit{}` format, diagnostic codes, and LSP
> protocol are not frozen yet and may still shift between `0.x`
> releases. End-to-end, these work today: the annotation
> scanner, attachment pass, the full H-series checker (H001–H004 +
> H010), the unit-algebra wrapper rules for `LOG` / `EXP`-tagged
> quantities (D1.2 – D1.6), per-rule provenance traces, intrinsics,
> user-defined function and subroutine calls, derived-type field
> access, rational `**` exponents, multi-file worksets, a workspace-
> aware LSP server with live-edit diagnostics, per-surface hover
> (call / subroutine / expression, each Short or Detailed with a
> formal-vs-actual pairing or full unit-algebra trace), inlay
> hints, go-to-definition, code actions, completion, and a CLI that
> accepts files or directories.

## Adopting on an existing codebase

Many real-world Fortran projects already document units in inline
comments — `! [m/s]`, `! horizontal wind speed [m/s]`,
`! tracer ratio [m^2: Andreas 1989]`. DimFort can read your
project's own convention, so you don't rewrite every declaration
just to opt in.

Add a few lines to `.dimfort.toml`:

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
```

Now `! [m/s]` is a first-class unit annotation, checked exactly
like `@unit{m/s}`. The same mechanism handles `@unit_assume` and
`@unit_affine_conversion`, each on its own list with its own
opt-in. Full recipe in
[Bringing DimFort to an existing codebase](docs/quickstart/bringing-to-existing-codebase.md).

## Quick tour

Want a hands-on look first? [`demos/tour.f90`](https://github.com/ArrialVictor/DimFort/blob/main/demos/tour.f90)
is a short, self-contained file that exercises the most common
DimFort diagnostics on a textbook moist-thermodynamics routine.

```bash
dimfort check --scale demos/tour.f90
```

[`demos/README.md`](https://github.com/ArrialVictor/DimFort/blob/main/demos/README.md)
walks through the output line by line.

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
dimfort interactions <var> path/...  # cross-site unit report for one variable
dimfort lsp                          # start the language server (stdio)
```

`interactions` is an on-demand query: for a single variable it lists every
site that reads or writes it across the workset — grouped into Declaration /
Write / Read / Undetermined, each with the unit that site implies — and
emits `X001` when two sites make conflicting unit *claims* (which the
per-statement `check` can't see, since it fires even on unannotated variables).

Exit code is `0` if no errors, `1` if any error-severity diagnostic was
produced, `2` for usage / file / config errors. Warnings alone do not
fail the run.

Diagnostics are grouped by code prefix: **H** for homogeneity,
**U** for annotation-pipeline problems, **S** for the opt-in
scale family, **X** for cross-site findings from `dimfort
interactions`, and **P** for parser-skipped regions. Full
reference (every code, severity, and trigger) lives at
[docs/reference/diagnostic-codes.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/diagnostic-codes.md).
The unit-algebra rule taxonomy (`D1.1`–`D1.7`) that classifies
*why* a homogeneity diagnostic fires is at
[docs/reference/unit-algebra.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/unit-algebra.md).

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
[docs/reference/unit-algebra.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/unit-algebra.md).

### Trace mode

Pass `--trace` to see the rule chain behind each diagnostic:

```bash
dimfort check --trace src/my_module.f90
```

Each error / warning prints the firing rule IDs (`R3.1`, `R5.6`, …)
under the message. The VSCode extension surfaces the same trace in
hover, and lets you pick the depth per surface — call, subroutine,
expression — via the `DimFort: Hover` settings (`Short` for a
one-line summary, `Detailed` for the full unit-algebra tree with
per-row 🟢/🟡/🔴 markers).

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/hover-expression-detailed-violation_dark.png">
  <img width="640" src="https://raw.githubusercontent.com/ArrialVictor/DimFort/main/docs/img/hover-expression-detailed-violation_light.png" alt="Detailed expression hover showing a homogeneity violation propagating up the unit-algebra tree">
</picture>

See [docs/editor-integration/hover-ui.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/editor-integration/hover-ui.md)
for the layout spec.

See [docs/reference/annotations.md](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/annotations.md)
for the full reference: unit-expression grammar, continuation-line
forms, declaration lists, and the diagnostic codes the scanner can
emit.

## Documentation

- [Annotations](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/annotations.md)
- [Usage details](https://github.com/ArrialVictor/DimFort/blob/main/docs/usage.md)
  — includes the [bringing DimFort to an existing codebase](https://github.com/ArrialVictor/DimFort/blob/main/docs/usage.md#bringing-dimfort-to-an-existing-codebase)
  guide (configurable comment delimiters, added 0.2.2).
- [Language server](https://github.com/ArrialVictor/DimFort/blob/main/docs/editor-integration/lsp-protocol.md)
- [Releases](https://github.com/ArrialVictor/DimFort/blob/main/docs/release-process.md)

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
