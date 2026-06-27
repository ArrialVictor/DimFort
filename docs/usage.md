# User guide

This page is the entry point for someone past install who wants to
understand what DimFort does, what's wired up today, and how to
run a workspace efficiently. Each topic links into the dedicated
reference or quickstart pages.

For a step-by-step first run, start with the [quickstart](quickstart/).

## What's wired up

DimFort is at **beta**. The list below is what works end-to-end
today; anything not listed should be treated as unimplemented.

- **Annotation scanner.** `@unit{…}` extraction across all
  placement forms — trailing `!<`, preceding `!>` / `!!`, with
  declaration-list and continuation-line handling. Configurable
  per-project delimiters as of 0.2.2; see
  [quickstart/bringing-to-existing-codebase.md](quickstart/bringing-to-existing-codebase.md).
- **Semantic checker** for `H001` (assignment mismatch), `H002`
  (additive / same-unit-intrinsic operand mismatch), `H003`
  (dimensionless-intrinsic violation), `H004` (user-defined
  function / subroutine argument mismatch), and `H010` (warnings:
  implicit literal cast `D1.5`, implicit wrapper untag `D1.6`).
  Full [diagnostic-code reference](reference/diagnostic-codes.md).
- **Unit algebra** for `LOG(...)` / `EXP(...)`-tagged quantities.
  Wrapper arithmetic raises `H001` / `H002` with `(D1.2)` /
  `(D1.3)` / `(D1.4)` markers; the full rule set is in
  [reference/unit-algebra.md](reference/unit-algebra.md).
- **Intrinsics.** A curated set of Fortran intrinsics with known
  unit semantics — see [reference/intrinsics.md](reference/intrinsics.md).
- **Per-rule provenance traces** with `dimfort check --trace` and
  in the editor hover at `hover: "detailed"`.
- **Derived-type field access.** `b%v` works as a read and as an
  assignment target, with field annotations declared inside the
  `type :: …` block.
- **Multi-file worksets.** `dimfort check a.f90 b.f90 c.f90`
  aggregates unit tables and function signatures across files
  before checking each one; cross-file `use` clauses splice
  imported symbols into the consumer's scope.
- **Workspace-aware LSP server** (`dimfort lsp`) — workspace
  index, debounced live editing, hover, inlay hints,
  go-to-definition, code actions, completion, side panel.
  Wire-protocol contract:
  [editor-integration/lsp-protocol.md](editor-integration/lsp-protocol.md).
- **Opt-in scale checking** for magnitude (`hPa` vs `Pa`) and
  zero-point (`degC` vs `K`) mismatches via `--scale` or
  `[scale] enabled = true`. Spec:
  [design/shipped/scale.md](design/shipped/scale.md).
- **`P001` parser-skipped regions.** DimFort makes no unit
  guarantee on lines it couldn't parse and says so. On by
  default; silence on a known-F77 corpus with
  `[diagnostics] P001 = "off"` in `dimfort.toml`. See
  [design/shipped/unparsed-regions.md](design/shipped/unparsed-regions.md).

## Generating a project config

`dimfort init` scaffolds a `dimfort.toml` from the shipped discipline
templates. The output opens with the SI core (inherited from the
shipped defaults) and includes all five discipline templates —
selected ones uncommented, the rest commented out for in-file
discovery of what's available.

```bash
dimfort init -t climate              # activate the climate template
dimfort init -t climate,astronomy    # activate multiple
dimfort init --bare                  # SI core only, no templates
dimfort init --dry-run               # print to stdout without writing
dimfort init -o configs/my.toml      # write somewhere other than ./dimfort.toml
```

The five templates are `climate`, `astronomy`, `geosciences`,
`biology-medicine`, and `legacy` (imperial / CGS — archaeological
code only). Templates live in-tree at
[`src/dimfort/templates/`](https://github.com/ArrialVictor/DimFort/tree/main/src/dimfort/templates),
each entry annotated with its source citation. Refuses to overwrite
an existing `dimfort.toml` unless you pass `--force`.

The override gate enforced at load time: the seven SI base units and
the SI prefix table cannot be redefined (hard error). New `[derived]`
entries are accepted, and redefinitions of shipped derived units emit
`UnitAmbiguityWarning` rather than silently shadowing. Per-entry
provenance for the shipped defaults lives at
[`docs/reference/units-source-citations.md`](reference/units-source-citations.md).

## Content-hash cache

On large worksets DimFort can cache per-file check results so
re-runs only re-check the files that actually changed (and their
consumers). The cache is **off by default**; enable with
`--cache read-write`:

```bash
dimfort check src/ --cache read-write --timings
```

Or via LSP `initializationOptions`:

```jsonc
{ "cacheMode": "read-write" }
```

On the first run the cache directory is created and every file's
check output is stored. Subsequent runs replay cached diagnostics
for unchanged files. The check phase drops sharply on a warm
cache; the rest of the pipeline (load / aggregate / index) runs
as usual. `--timings` shows hit / miss / dirty / write counts so
you can sanity-check invalidation.

### Invalidation

A file's cache entry is invalidated when:

- its source bytes change;
- any header pulled in via `#include` changes (the `cpp` closure
  is hashed alongside the source);
- the relevant `dimfort.toml` keys change (`external_modules`,
  `extra_defines`, `extra_include_paths`, the three comment-pattern
  lists);
- the DimFort version changes;
- any module the file `use`s has its exports change
  (per-module dependency tracking).

If a cache entry's dependencies have moved but the file itself
hasn't, the entry is flagged "dirty" and the file is re-checked.

### Location

The cache lives at `.dimfort-cache/` under the first path argument
by default. Override with `--cache-dir DIR`. Add the cache
directory to your `.gitignore` — it's build output, not source.

Wipe and rebuild:

```bash
dimfort check src/ --cache read-write --clear-cache
```

The cache automatically prunes entries older than 30 days and
trims to a 500 MB ceiling at the end of each run.

### When to leave the cache off

- One-off runs over a small workset — the cache write overhead
  (~1 ms per file) is dead weight when the run is sub-second.
- CI runs against a clean checkout — no prior cache to read from,
  so the cache directory just gets written and discarded.
- Debugging the checker itself — `--cache off` (the default)
  guarantees every diagnostic comes from a fresh check.

Full wire-format reference for the cache:
[design/shipped/content-hash-cache.md](design/shipped/content-hash-cache.md).

## LSP server tuning (env vars)

The language server (`dimfort lsp`) reads two environment
variables. Both are optional and used mainly for debugging.

- **`DIMFORT_LSP_LOG_LEVEL`** — override the default `INFO`
  log threshold. Accepted: `DEBUG`, `INFO`, `WARNING`, `ERROR`,
  `CRITICAL` (case-insensitive). Useful when filing a bug report
  or investigating why a workspace-scope feature isn't firing —
  set to `DEBUG` to surface cache hits/misses, derive-root
  decisions, and other audit-trail messages that don't show at
  `INFO`. Invalid values warn and fall back to `INFO`. When set
  validly, the server logs a one-line confirmation at server
  start (`LSP log level set to <LEVEL> via DIMFORT_LSP_LOG_LEVEL`)
  so you can verify the env var was read without relying on
  threshold-effect observation. The confirmation is silent at
  `WARNING` or higher thresholds — if you asked for a quiet
  server, you get a quiet server.

  Pass to the companion-spawned server via your shell's env
  mechanism — for VSCode, `DIMFORT_LSP_LOG_LEVEL=debug code .`
  works because VSCode inherits the launching shell's
  environment. For Nvim/Emacs, set it in your shell's rc file
  or wrap the launch.

- **`DIMFORT_CRASH_LOG`** — path for the crash-trace file the
  server writes when stdio goes silent. Defaults to
  `/tmp/dimfort-lsp.crash`. Set to an empty string to disable.
