# `dimfort.toml` reference

A `dimfort.toml` file at any ancestor directory of the file being
checked configures DimFort for that project. DimFort walks upward
from the **first** path passed on the command line until it finds
one (multi-root invocations against scattered codebases share that
first-path-anchored config — write a wrapper or a shared parent
config for cross-tree setups). A missing file is treated as empty;
a malformed file now fails fast with **exit 2** so a typo or stray
bracket isn't silently ignored. The LSP keeps the soft-degrade path
for the same case so an editor session never dies on a bad config.

Every section is optional. The example file below shows every key
DimFort recognises:

```toml
[project]
src_paths = ["src", "modules"]

[workset]
max_size         = 80
external_modules = ["netcdf", "mpi"]

[parser]
# CPP support for .F90 files.
cpp_defines   = ["WITH_NETCDF=1", "_OPENMP"]
include_paths = ["include", "vendor/foo/include"]

# Comment-marker namespace (0.2.7). Six pattern lists — three
# positive directive families and three `nonunit`-prefixed filters.
# Each positive list REPLACES its default; list both to keep the
# canonical form alongside a custom one.
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume = [
  { open = "@unit_assume{", close = "}", sep = ":" },
]
unit_affine = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
# Filter lists (default: three shipped `nonunit` patterns; empty
# for the two others). Set to `[]` for an explicit opt-out.
# nonunit         = [{ open = "@nonunit{", close = "}" }]
# nonunit_assume  = []
# nonunit_affine  = []

# Permissive lexer flags (0.2.7). All eight default OFF; opt in
# per corpus convention. See §parser.unit_lexer below.
[parser.unit_lexer]
allow_unicode_superscripts = true   # m·s⁻¹
allow_middot_multiplication = true  # kg·m⁻³

# Pre-tokenization transforms (0.2.7). Currently houses the
# biogeochem-tag strip.
[parser.unit_preprocess]
strip_biogeochem_tags = false

[units]
file = "etc/project-units.toml"

[diagnostics]
U021 = "info"      # demote conflicting-pattern warnings
"D1.7" = "error"   # promote exponent-must-be-dimensionless to hard error
P001 = "off"       # silence parser-skipped regions

[scale]
enabled = true
```

## `[project]`

| Key | Type | Default | Effect |
|---|---|---|---|
| `src_paths` | `list[string]` | `[]` | Directories DimFort treats as the canonical source tree. Paths are resolved relative to the config file. Empty means "check whatever was passed on the command line." |

## `[workset]`

The workset is the set of files DimFort holds in memory for a
single check (cross-file `use` resolution + signature aggregation
happen across the workset).

| Key | Type | Default | Effect |
|---|---|---|---|
| `max_size` | `integer` | unbounded on CLI; `40` on LSP | Cap the number of files per check. On large workspaces this keeps the LSP responsive by pinning the active file's direct dependencies. The LSP-side default can be raised via `maxWorksetSize` in `initializationOptions`. |
| `external_modules` | `list[string]` | `[]` | Module names DimFort should treat as external — calls into them resolve to "unit unknown" without firing `H004`. Names are lowercased. Useful for `mpi`, `netcdf`, `hdf5`, and project-internal vendored libraries you do not want DimFort to walk. |

## `[parser]`

CPP preprocessing for `.F90` files: when either `cpp_defines` or
`include_paths` is set, DimFort shells out to the system `cpp`
before parsing. Identical semantics to the pre-tree-sitter
`[lfortran]` block — both are accepted; `[parser]` wins on
conflict.

| Key | Type | Default | Effect |
|---|---|---|---|
| `cpp_defines` | `list[string]` | `[]` | Each entry becomes a `cpp -D<entry>` flag. Use `"NAME"` or `"NAME=value"`. |
| `include_paths` | `list[string]` | `[]` | Each entry becomes a `cpp -I<path>`. Paths are resolved relative to the config file. |

Comment-marker patterns live under `[parser.unit_comments]`
(nested table, 0.2.7). Permissive lexer flags live under
`[parser.unit_lexer]`. Pre-tokenization transforms live under
`[parser.unit_preprocess]`. Each is documented separately below.

### `[parser.unit_comments]`

Six pattern lists — three positive directive families and three
`nonunit`-prefixed filters. Each positive list REPLACES its
default (setting an empty positive list is an error and falls
back to the default). Filter lists default to `[]` for
`nonunit_assume` / `nonunit_affine` and to three shipped patterns
for `nonunit`; an explicit `[]` opts out.

| Key | Type | Default | Effect |
|---|---|---|---|
| `unit` | `list[table]` | `[{open="@unit{", close="}"}]` | Positive patterns DimFort treats as `@unit{...}` unit-claim directives. Each entry is `{open, close}`. |
| `nonunit` | `list[table]` | three shipped patterns | Filter patterns that suppress a match from `unit`. Each entry is `{open, close, regex?}`. Handles per-site author markers (`@nonunit{...}`) and project-level citation-shaped noise. |
| `unit_assume` | `list[table]` | `[{open="@unit_assume{", close="}", sep=":"}]` | Positive patterns for the `@unit_assume{...}` escape hatch. Each entry is `{open, close, sep}` — `sep` separates the asserted unit from the mandatory reason. |
| `nonunit_assume` | `list[table]` | `[]` | Filter patterns for `unit_assume`. Same shape as `nonunit` plus optional `sep`. |
| `unit_affine` | `list[table]` | `[{open="@unit_affine_conversion{", close="}", sep="->"}]` | Positive patterns for verified affine conversions. Each entry is `{open, close, sep}` — `sep` separates source and target units. |
| `nonunit_affine` | `list[table]` | `[]` | Filter patterns for `unit_affine`. Same shape as `nonunit_assume`. |

See [bringing DimFort to an existing codebase](../quickstart/bringing-to-existing-codebase.md)
for the adoption recipe, and
[design/shipped/unit-comment-markers.md](../design/shipped/unit-comment-markers.md)
for the full spec.

### `[parser.unit_lexer]`

Eight independent boolean flags that toggle permissive lexer rules
on top of the strict default grammar. Every flag defaults to
`false` — strict, conservative, no out-of-box silent misparses.
Opt in per corpus convention. See
[design/shipped/permissive-unit-lexer.md](../design/shipped/permissive-unit-lexer.md)
§3.1-§3.8 for per-flag empirical case + false-positive
characterization, and §4.2 for the pairwise composition contract.

| Flag | Default | Effect |
|---|---|---|
| `allow_unicode_superscripts` | `false` | Accept `⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺` as exponent characters (`m·s⁻¹`, `kg m⁻³`). |
| `allow_middot_multiplication` | `false` | Accept `·` (U+00B7) as a multiplication operator alias (`m·s`, `kg·m⁻³`). |
| `allow_fortran_star_star` | `false` | Accept `**` as an alias for `^` (`m**2`, `m**-1`). |
| `allow_latex_braces` | `false` | Accept `^{...}` grouping (`m^{-1}`, `Pa^{kappa-1/3}`); rewritten to paren'd shape. |
| `allow_dot_multiplication` | `false` | Accept `.` between identifiers as multiplication (`J.kg^-1`, `kgC.m^-2.s^-1`). Decimal literals unaffected. |
| `allow_implicit_product` | `false` | Accept whitespace between identifiers as multiplication (`kg m`, `W m`). |
| `allow_integer_suffix_exp` | `false` | Accept trailing **signed** integers on identifiers as exponents (`m s-1`, `kg m-3`, `J mol-1`). |
| `allow_bare_digit_exp` | `false` | Accept bare **unsigned** digit suffixes on a guarded set of known-unit identifiers as exponents (`m2`, `m3`, `W/m2`). HIGH false-positive risk — see design §3.5. |

### `[parser.unit_preprocess]`

Pre-tokenization transforms applied before the lexer (and before
any `[parser.unit_lexer]` flag rewrites).

| Key | Type | Default | Effect |
|---|---|---|---|
| `strip_biogeochem_tags` | `bool` | `false` | Strip parenthesised species / tracer tags before the lexer runs (`(NO3)`, `(CO2)`, `(dust1)`). Target: biogeochem tracer tags in coupled-Earth-system codebases. |
| `biogeochem_tag_exceptions` | `list[string]` | `[]` | Inner-paren content that must NOT be stripped. Escape hatch for tags that would collide with genuine unit content (e.g. `(1)` when the corpus writes `mol(NO3)/m^3` alongside `Pa^(1/2)`). |

## `[units]`

| Key | Type | Default | Effect |
|---|---|---|---|
| `file` | `string` | unset | Path to an extension units file (`*.toml`) merged on top of the shipped SI catalog. Lets a project add domain-specific units (`hPa`, `bar`, `day`, `percent`) without forking DimFort. Path is resolved relative to the config file. |

Full schema, merge semantics, and per-section examples are in
[units-file.md](units-file.md). The schema is the same as the
shipped [`default_units.toml`](https://github.com/ArrialVictor/DimFort/blob/main/src/dimfort/core/default_units.toml);
copy any entry from there as a starting point.

## `[diagnostics]`

Per-code severity overrides. Keys are either user-facing diagnostic
codes (`H001`, `U021`, `P001`, …) or unit-algebra rule markers
(`"D1.4"`, `"D1.6"`, `"D1.7"` — quoted because of the dot). Values
are `"error"`, `"warning"`, `"info"`, or `"off"`. Rule-marker
overrides win over generic-code overrides when both are set for
the same firing.

```toml
[diagnostics]
P001   = "off"      # silence parser-skipped regions on a known-F77 corpus
U021   = "info"     # demote pattern-conflict warnings to non-blocking
"D1.6" = "error"    # promote implicit wrapper-untag warnings to hard errors
```

Codes set to `"info"` or `"off"` never affect the exit code. The
full diagnostic catalog is at
[diagnostic-codes.md](diagnostic-codes.md).

## `[scale]`

| Key | Type | Default | Effect |
|---|---|---|---|
| `enabled` | `boolean` | `false` | Turn on multiplicative-scale checking (`S001`, `S002`, `S003`). Equivalent to `dimfort check --scale` on the CLI or `scaleMode: true` in LSP `initializationOptions`. |

Full feature: [design/shipped/scale.md](../design/shipped/scale.md).

## Legacy `[lfortran]` (deprecated)

The pre-tree-sitter parser was named `lfortran` and accepted
`cpp_defines` and `include_paths` under `[lfortran]`. Those keys
are still read for back-compat; `[parser]` is the canonical home
and wins on conflict. New projects should use `[parser]`.
