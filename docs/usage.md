# Usage

## Install

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
pip install -e ".[dev,lsp]"
```

Requires Python ≥ 3.11. The Fortran parser
([tree-sitter-fortran](https://pypi.org/project/tree-sitter-fortran/))
is installed automatically as a runtime dependency — no external
compiler or subprocess.

## CLI

```bash
dimfort check <paths>...   # check Fortran sources for unit homogeneity
dimfort lsp                # start the language server (stdio)
```

`<paths>` may be individual files or directories. Directories are
walked recursively for `.f90` / `.F90` / `.f95` / `.F95` / `.f03` /
`.F03` / `.f08` / `.F08` sources.

`check` flags:

| Flag             | Effect                                            |
|------------------|---------------------------------------------------|
| `-q`, `--quiet`  | Suppress diagnostic output; only return an exit code. |
| `--no-color`     | Disable ANSI colour (also auto-disabled outside a TTY, or when `NO_COLOR` is set). |
| `--summary`      | After the diagnostic stream, print a per-file H-/U-count breakdown and total. |

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
  (dimensionless-intrinsic violation), **H004** (user-defined
  function/subroutine argument mismatch), and **H010** (warnings:
  implicit literal cast D1.5, implicit wrapper untag D1.6). Plus
  a useful subset of Fortran intrinsics (`sqrt`, `abs`, `exp`,
  `log`, trig family, `min`/`max`/`mod`/`merge`,
  `dot_product`/`matmul`, `sum`/`minval`/`maxval`, the
  kind-conversion family)
- unit-algebra rules for `LOG` / `EXP`-tagged quantities (Phase
  B): `@unit{LOG(Pa)}`, `@unit{EXP(K)}`, and nested forms.
  Wrapper arithmetic raises H001 / H002 with `(D1.2)` / `(D1.3)` /
  `(D1.4)` markers identifying the firing rule. See
  [docs/unit-algebra.md](unit-algebra.md) for the full rule set.
- per-rule provenance traces (`dimfort check --trace`, and in the
  VSCode hover when the trace toggle is on)
- derived-type field access (`b%v`) both as a read and as an
  assignment target, with field annotations declared inside the
  `type :: …` block
- multi-file worksets: `dimfort check a.f90 b.f90 c.f90` aggregates
  unit tables and function signatures across files before checking
  each one; cross-file `use` clauses splice imported symbols into the
  consumer's scope
- a working LSP server (`dimfort lsp`) — workspace-aware (cross-file
  diagnostics in the editor), debounced live editing on every
  keystroke, hover / inlay hints / go-to-definition / code lens / code
  action for inserting `!< @unit{}` skeletons. Wire it up in your
  editor following [docs/lsp.md](lsp.md); a VSCode extension scaffold
  lives next to the repo at `Homogeneity/DimFort-VSCompanion/` (its
  own GitHub repo:
  https://github.com/ArrialVictor/DimFort-VSCompanion)
- end-to-end CLI: `dimfort check FILE [FILE …]` runs the full pipeline
  and reports diagnostics in `file:line: severity: code message` form

Treat anything not listed above as unimplemented.
