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
| `--timings`      | Print wall-clock seconds per pipeline phase. With a cache active, also prints hit/miss/dirty/write counts. |
| `--cache MODE`   | Content-hash cache mode: `off` (default), `read-only`, or `read-write`. See [Content-hash cache](#content-hash-cache). |
| `--cache-dir D`  | Override the cache directory (default: `.dimfort-cache/` under the first path argument). |
| `--clear-cache`  | Wipe the cache directory before running. Combine with `--cache read-write` to repopulate. |

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

## Bringing DimFort to an existing codebase

Real Fortran projects rarely greet DimFort with a clean `@unit{...}`
slate — most have years of author convention in inline comments
already (`! [m/s]`, `! [m^2: empirical]`, …). 0.2.2 lets the
project tell DimFort about its own convention in `.dimfort.toml`
so those comments become first-class annotations without rewriting
every declaration line.

There are three independent pattern lists, one per directive
family — each can be configured (or left at its default) on its
own:

| `[parser]` key | Directive | Default |
| --- | --- | --- |
| `unit_comment_delimiters` | `@unit{...}` (unit claim) | `[{open="@unit{", close="}"}]` |
| `unit_assume_comment_delimiters` | `@unit_assume{...:...}` (escape hatch) | `[{open="@unit_assume{", close="}", sep=":"}]` |
| `unit_affine_comment_delimiters` | `@unit_affine_conversion{...->...}` (verified frame change) | `[{open="@unit_affine_conversion{", close="}", sep="->"}]` |

The three lists are deliberately independent: `@unit_assume{}`
suppresses a fire (a wrong assume silently loses safety) and
`@unit_affine_conversion{}` adds a global conversion rule
(rippling through downstream math), so projects opt into loose
delimiters per directive, not all at once.

### Recipe

Most adopters only need to extend `unit_comment_delimiters`:

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
```

Each list **replaces** its default; to keep canonical syntax
alongside a custom form, list both (as above). Setting any list
to `[]` is treated as a configuration error and falls back to
the default — an empty list would silently disable that directive
family, almost certainly a typo.

A more aggressive adopter who also wants bracket-shaped assumes
and verified affine conversions:

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "[",            close = "]", sep = ":" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
  { open = "[",                       close = "]", sep = "->" },
]
```

With this config, all of the following are recognised:

```fortran
real :: ws                     ! [m/s]
real :: ws                     ! horizontal wind speed [m/s]
real :: tracer_eff             ! eff. surface ratio [m^2: Andreas 1989]
real :: sst_k = sst_c + 273.15 ! sea-surface T conversion [degC -> K]
```

### What to expect on the first run

A burst of new diagnostics — many of them real bugs that have
been hiding behind doc-only annotations. Two new codes in
particular surface configuration-time issues:

- **U021 — conflicting unit comment patterns.** Two configured
  patterns matched the same comment with disagreeing captures.
  The first-listed wins (deterministic from the config order);
  the message asks the user to clarify by removing one form.
- **U023 — directive on wrong statement kind.** `@unit_assume`
  on a declaration, `@unit{}` on an assignment, and similar
  mismatches. The directive is dropped (not silently applied);
  the message names the directive that would attach correctly.

If the volume is overwhelming on a first pass, the `[diagnostics]`
table accepts severity overrides for any code:

```toml
[diagnostics]
U021 = "info"     # demote to non-blocking until the team triages
U023 = "info"
```

The full spec — including the `@unit{...}` rewrite detector that
adds "did you mean …?" suggestions to U002 — lives in
[design/unit-comment-delimiters.md](design/unit-comment-delimiters.md).

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
- **P001** (info): a region the parser couldn't read. DimFort makes no
  unit guarantee on those lines, so it says so (a blue squiggle) rather
  than implying they're clean. On by default; silence it with
  `[diagnostics]` `P001 = "off"` in `.dimfort.toml` (e.g. on known F77
  files). See [docs/design/unparsed-regions.md](design/unparsed-regions.md).
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
  keystroke, hover / inlay hints / go-to-definition / code
  action for inserting `!< @unit{}` skeletons. Wire it up in your
  editor following [docs/lsp.md](lsp.md); a VSCode extension scaffold
  lives next to the repo at `Homogeneity/DimFort-VSCompanion/` (its
  own GitHub repo:
  https://github.com/ArrialVictor/DimFort-VSCompanion)
- end-to-end CLI: `dimfort check FILE [FILE …]` runs the full pipeline
  and reports diagnostics in `file:line: severity: code message` form

Treat anything not listed above as unimplemented.

## Content-hash cache

On large worksets DimFort can cache per-file check results so that
re-runs only re-check the files that actually changed (and their
consumers). The cache is **off by default**; enable it with
`--cache read-write`.

```bash
dimfort check src/ --cache read-write --timings
```

On the first run the cache directory is created and every file's
check output is stored. Subsequent runs replay cached diagnostics
for unchanged files. The check phase drops sharply on a warm cache
(a benchmark workspace measured ~15 s → ~3 s); the rest of the pipeline
(load / aggregate / index) runs as usual, so total wall time goes
from ~33 s cold to ~20 s warm.

### What triggers invalidation

A file's cache entry is invalidated when:

- its **source bytes** change;
- any header pulled in via `#include` changes (the cpp closure is
  hashed alongside the source);
- the relevant `.dimfort.toml` config keys change
  (`external_modules`, `extra_defines`, `extra_include_paths`);
- the **DimFort version** changes;
- any **module the file uses** has its exports change (per-module
  dependency tracking).

If a cache entry's deps have moved but the file itself hasn't, the
entry is flagged "dirty" and the file is re-checked. The
`--timings` output shows hit / miss / dirty / write counts so you
can sanity-check invalidation behaviour.

### Cache location

The cache lives at `.dimfort-cache/` under the first path argument
by default. Override with `--cache-dir DIR`. Add the cache
directory to your `.gitignore` (it's build output, not source).

To wipe and rebuild the cache:

```bash
dimfort check src/ --cache read-write --clear-cache
```

The cache automatically prunes entries older than 30 days and
trims to a 500 MB ceiling at the end of each run.

### When to leave the cache off

- One-off runs over a small workset — the cache write overhead
  (roughly a millisecond per file) is dead weight when the whole
  run is sub-second.
- CI runs against a clean checkout — the workspace has no prior
  cache to read from, so the cache directory just gets written
  and discarded.
- Debugging the checker itself — use `--cache off` (default) to
  guarantee every diagnostic comes from a fresh check.
