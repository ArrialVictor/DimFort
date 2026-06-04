# `.dimfort.toml` reference

A `.dimfort.toml` file at any ancestor directory of the file being
checked configures DimFort for that project. DimFort walks upward
from each input file until it finds one. A missing or malformed
file is treated as empty — defaults apply and the CLI / LSP
never fail to start because of config.

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

# Configurable comment delimiters (0.2.2). Each list REPLACES its
# default; list both to keep the canonical form alongside a custom
# one.
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]

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
| `unit_comment_delimiters` | `list[table]` | `[{open="@unit{", close="}"}]` | Comment patterns DimFort treats as `@unit{...}` unit-claim directives. Each entry is `{open = "...", close = "..."}`. Setting to `[]` is an error and falls back to the default. |
| `unit_assume_comment_delimiters` | `list[table]` | `[{open="@unit_assume{", close="}", sep=":"}]` | Patterns for the `@unit_assume{...}` escape hatch (asserts an irreducible RHS unit). Each entry is `{open, close, sep}` — `sep` separates the asserted unit from the mandatory reason. |
| `unit_affine_comment_delimiters` | `list[table]` | `[{open="@unit_affine_conversion{", close="}", sep="->"}]` | Patterns for verified affine conversions. Each entry is `{open, close, sep}` — `sep` separates source and target units. |

See [bringing DimFort to an existing codebase](../quickstart/bringing-to-existing-codebase.md)
for the adoption recipe, and
[design/shipped/unit-comment-delimiters.md](../design/shipped/unit-comment-delimiters.md)
for the full spec.

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
