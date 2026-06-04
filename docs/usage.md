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
  `[diagnostics] P001 = "off"` in `.dimfort.toml`. See
  [design/shipped/unparsed-regions.md](design/shipped/unparsed-regions.md).

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
- the relevant `.dimfort.toml` keys change (`external_modules`,
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
