# CLI reference

```bash
dimfort check <paths>...        # check Fortran sources for unit homogeneity
dimfort interactions <sym> ...  # cross-site unit report for one variable
dimfort lsp                     # start the language server (stdio)
dimfort --version
```

`<paths>` may be individual files or directories. Directories are
walked recursively for files whose suffix is in the accepted set
(see [Supported file extensions](#supported-file-extensions) below).

## Supported file extensions

DimFort accepts `.f90` / `.F90`, `.f95` / `.F95`, `.f03` / `.F03`,
and `.f08` / `.F08`. The casing follows convention: lower-case
suffixes are plain Fortran source; upper-case suffixes carry CPP
preprocessor directives and trigger the `[parser] cpp_defines` /
`include_paths` machinery.

Fortran 2018 (`.f18` / `.F18`) and Fortran 2023 (`.f23` / `.F23`)
suffixes are **not currently accepted**. The underlying
`tree-sitter-fortran` grammar covers F2008 reliably and parts of
F2018 best-effort; constructs the grammar does not recognize would
surface as `P001` unparsed regions rather than silent acceptance.
Most projects targeting F2018+ still use `.f90` as the file suffix
(the standard level is set on the compiler command line, not by
the filename), so the practical gap is narrower than it sounds.

## `dimfort check`

Run the full pipeline (parse → annotate → attach → check) over each
input and print diagnostics to stdout. The exit code is the
fail-the-build signal — see [Exit codes](#exit-codes).

| Flag | Effect |
|---|---|
| `-q`, `--quiet`  | Suppress diagnostic output; only return an exit code. |
| `--no-color`     | Disable ANSI colour. Auto-disabled outside a TTY, or when `NO_COLOR` is set. |
| `--summary`      | After the diagnostic stream, print a per-file H/U count breakdown and total. |
| `--timings`      | Print wall-clock seconds per pipeline phase. With a cache active (`--cache read-only` or `--cache read-write`), also prints hit / miss / dirty / write counts. `--cache` alone (without `--timings`) is silent. |
| `--trace`        | Attach a unit-algebra rule-chain trace to each diagnostic, rendered below the message. Useful for wrapper-arithmetic diagnostics (D1.2 / D1.3 / D1.6). |
| `--scale`        | Opt in to multiplicative-scale checking — flag operands of the same dimension but different magnitude (e.g. `hPa` vs `Pa`, `g/kg` vs `kg/kg`) as `S001`. Equivalent to `[scale] enabled = true` in `.dimfort.toml`. |
| `--cache MODE`   | Content-hash cache mode: `off` (default), `read-only`, or `read-write`. See [Content-hash cache](../usage.md#content-hash-cache). |
| `--cache-dir D`  | Override the cache directory (default: `.dimfort-cache/` under the first path argument). |
| `--clear-cache`  | Wipe the cache directory before running. Combine with `--cache read-write` to repopulate. |

## `dimfort interactions`

```bash
dimfort interactions <symbol> <paths>...
```

List every site that reads or writes `<symbol>` across the workset,
tagged with the unit each site requires or contributes, and flag
sites whose unit constraints conflict (`X001`). `<symbol>` is
matched case-insensitively.

| Flag | Effect |
|---|---|
| `--file <name>`  | Restrict to occurrences in a file whose name or path ends with `<name>`. |
| `--scope <name>` | Restrict to occurrences inside a routine of this name (case-insensitive). |
| `--no-color`     | Disable ANSI colour. |
| `--scale`        | Also treat magnitude (factor) disagreements between sites as conflicts. Mirrors `check --scale`. |

The same query is available over LSP as the `dimfort/interactions`
custom request — see [editor-integration/lsp-protocol.md](../editor-integration/lsp-protocol.md#cross-site-analysis-dimfortinteractions).

## `dimfort lsp`

Start the language server over stdio. Accepts no arguments beyond
the `--stdio` no-op some clients (vscode-languageclient with
`TransportKind.stdio`) tack on automatically.

The wire-protocol contract — `initializationOptions` keys, custom
requests, debouncing, workspace command — is documented in
[editor-integration/lsp-protocol.md](../editor-integration/lsp-protocol.md).

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | No errors. |
| `1`  | At least one error-severity diagnostic was emitted. |
| `2`  | Usage error, missing file, or invalid config. |

Warnings and `info`-severity diagnostics print but do not affect
the exit code. To make a warning fail the build, remap it to
`"error"` under `[diagnostics]` in `.dimfort.toml` — see
[`.dimfort.toml` reference](dimfort-toml.md#diagnostics).
