# Content-hash cache for workspace check

Status: **draft / not implemented**. Doc-first per the algebra-extension precedent.

## Motivation

Cold workspace check on LMDZ (2,435 files) is ~31 s. The annotation cycle workflow re-runs checks frequently while editing a handful of files at a time. Most files are unchanged between runs, but every phase currently re-does all work from scratch.

Phase breakdown today (post-2026-05-22 consolidation):

| Phase | Cold (s) | What it does |
|---|---|---|
| load | ~15 | read + cpp + tree-sitter parse + annotation scan |
| index | ~2 | aggregate per-file symbol tables into workspace index |
| check | ~14 | per-file checker walk; emits diagnostics |
| aggregate | ~0 | merge cross-file scopes |

For a warm run where N files changed out of 2,435:
- load: ~(2435 − N)/2435 of work is re-readable from cache
- index: same
- check: per-file work is identical; diagnostics for unchanged files cannot change unless a **cross-file dependency they consume** changed

Target: warm run on LMDZ in **~5 s** for a typical N=5 edit, dominated by re-checking the changed files and any files that consume their exported symbols.

## Approach

### Cache key (per file)

SHA-256 over the concatenation of these byte sequences, each length-prefixed:

1. raw source bytes
2. `b"CPP\0"` + the sorted list of `(include_path, sha256(include_contents))` tuples for every file in the cpp closure (see "cpp closure tracking" below)
3. `b"CONFIG\0"` + JSON of the per-file-affecting subset of config: `external_modules`, `strict_mode`, `units_aliases`, `extra_defines`, `extra_include_paths`
4. `b"DIMFORT\0"` + `__version__`
5. `b"OUTPUT\0"` + `CHECKER_OUTPUT_VERSION` (a hand-bumped integer in `ts_checker.py`; bump on any change to serialized diagnostic / signature / parameter-value shape)

Hash mismatch on any input → cache miss for that file.

The DimFort version and OUTPUT version are global. We could shard cache directories by version to make pruning trivial.

### Cached artefacts (per file)

Path layout:
```
{cache_dir}/v{CHECKER_OUTPUT_VERSION}/{first2}/{rest_of_hash}.json.gz
```

`{first2}` is the first 2 hex chars of the hash — limits per-dir file count to ~256 for LMDZ-scale workspaces.

Payload format: **JSON + gzip**. msgpack would be ~2× more compact and faster to load, but msgpack isn't a stdlib module — adding a binary dependency just for the cache isn't worth it for the MVP. Revisit if cache I/O becomes a measurable share of warm-run time.

Payload:
```
{
  "schema": 1,
  "file": "path/relative/to/workspace_root",
  "mtime_ns": int,              # advisory only — hash is authoritative
  "var_units":            {name_lc: serialized_unit},
  "var_units_by_scope":   {[scope, name_lc]: serialized_unit},
  "function_signatures":  {name_lc: serialized_sig},
  "module_exports":       {module_lc: serialized_exports},
  "parameter_values":     {name_lc: serialized_rational},
  "var_types":            {name_lc: type_name_lc},
  "type_field_types":     {[struct_lc, field_lc]: field_type_lc},
  "field_units":          {[struct_lc, field_lc]: serialized_unit},
  "diagnostics":          [serialized_diagnostic, ...],
  "deps_consumed":        [{module_lc, symbol_lc, kind} ...],
  "cpp_closure":          [{path, hash} ...],
}
```

Notes:
- Tree-sitter trees are **not** cached (fast to re-parse from text, large to serialize).
- Source text is also not cached (we already have it on disk).
- All units serialize via a stable JSON-able form already implemented for `--export-units`; reuse that.
- Diagnostic serialization needs a stable structured form (severity, code, file, span, message, machine-readable payload). Currently diagnostics are dataclasses with `.to_dict()` for LSP — likely reusable.

### cpp closure tracking

The existing cpp invocation in `ts_parser.py:_run_cpp` already parses `# <lineno> "file"` markers. We currently record `current_file` per line but discard the set of distinct files. Cheap change: capture every distinct `current_file` value (excluding `<built-in>`, `<command-line>`) into a set, and return it alongside the expanded source.

For `.F90` files where cpp is skipped (no `#` directives — already an optimisation), cpp_closure is empty.

For `.f90` files where cpp is not invoked at all, cpp_closure is empty.

For each captured path, we hash its contents once and memoize. A workspace-level `IncludeHasher` instance with `{abspath: (mtime_ns, hash)}` keeps this cheap — the mtime check lets us skip rehashing unchanged includes within a single run, and a persistent on-disk version of the same map could carry across runs.

### Invalidation

A file's cached entry is valid if **both**:

1. **Self-hash matches.** Its own key (source + cpp closure + config + versions) matches the stored hash.
2. **Consumed dependencies are stable.** For every `(module_lc, symbol_lc, kind)` in `deps_consumed`, the currently-resolved symbol's serialized form is byte-identical to what was stored when this entry was written.

Condition (2) is the subtle part. A naive "any file in the workspace changed → invalidate" cascades into a full recheck on every edit. Per-symbol granularity keeps the warm-run target reachable.

**`deps_consumed` capture**: during the cached run that *produces* the entry, the checker records every cross-file symbol lookup it performs:
- `USE module, only: foo` clauses → record `(module, foo, "use_only")`
- `USE module` without `only` → record `(module, "*", "use_all")` (forces invalidation if the module's export set changes at all)
- function call to externally-resolved name → record `(module, name, "call")`

The recorded form is **what was looked up**, not **what was returned** — that way, when a symbol disappears entirely, the consumer still notices.

Validation pass at workspace startup:
1. Hash every file (already needed for self-key).
2. For each file, attempt to load cached entry.
3. Compute the workspace-level index from cache-valid entries' exports.
4. Re-validate each entry's `deps_consumed` against the index. If any dep's serialized form changed, mark the entry dirty.
5. Re-check dirty + missing files. Their results join the index. Repeat (4) until fixed-point — bounded because each iteration only adds to the dirty set.

In practice the iteration converges in 1–2 passes: edits are rare, and most consumers re-validate fine after one pass.

### Storage

- Default `{workspace_root}/.dimfort-cache/`; configurable via `dimfort.cache.dir` or `--cache-dir`.
- msgpack + gzip. Estimated ~3–10 KB per file × 2,435 = 7–25 MB on LMDZ.
- Add `.dimfort-cache/` to `.gitignore` automatically on first write — the cache is build-output, not source.
- LRU pruning at 500 MB or 30 days, whichever first; pruning runs lazily at start of each workspace check.

### CLI surface

```
dimfort check --cache off          # bypass entirely (no read, no write)
dimfort check --cache read-only    # use cache for reads, never write
dimfort check --cache read-write   # default
dimfort check --clear-cache        # rm -rf the cache dir, then run
dimfort check --timings            # additionally shows hit / miss / dirty counts
```

LSP: `dimfort.cache.mode` config (same vocabulary); `dimfort.cache.dir` override.

`--timings` output gains:
```
Phase timings
  load          14.58 s    (cached: 2390/2435  fresh: 45)
  ...
Cache
  hits          2390
  misses         45         self-hash changed
  dirty           0         dependency changed
```

### Concurrent writers

CLI + LSP can run against the same cache. Strategy:
- Reads: no lock — entries are immutable once written; if an entry doesn't exist yet, treat as miss.
- Writes: temp-file + atomic rename (`os.replace`). A double-compute is harmless — the second writer overwrites with byte-identical content.
- Pruning: protected by `flock` on `{cache_dir}/.lock`. If acquire fails, skip this run's pruning.

No global lock, no race on the hot path.

## Phase wiring

### load phase
- For each input file, compute self-hash candidate.
- Attempt cache read. On hit and entry passes self-hash: skip read+cpp+parse+annotation-scan. Stage cached artefacts.
- On miss: full pipeline as today; produce artefacts; **defer write until check phase finishes** (we want diagnostics + deps_consumed in the same entry).
- Tree-sitter tree is re-parsed lazily *only* for files the check phase decides are dirty (i.e., need re-checking).

### aggregate phase
- Build workspace index from staged artefacts (cached + fresh alike).

### dependency validation pass (new, sub-second on 2,435 files)
- For each cache-hit file, verify its `deps_consumed` against the just-built index.
- Mark dirty entries; their files join the to-be-rechecked set.
- Iterate to fixed-point (typically 1 pass).

### check phase
- Re-parse + re-check files in the to-be-rechecked set.
- For cache-hit-and-clean files, replay cached diagnostics verbatim.
- For freshly-checked files, write a new cache entry.

### serialization
- Need a `units.to_jsonable()` / `units.from_jsonable()` pair (likely already exists for `--export-units`; verify).
- Need diagnostic `.to_dict()` / `Diagnostic.from_dict()` (likely exists for LSP; verify).
- A round-trip test (load → serialize → deserialize → byte-compare) per artefact type goes in the unit suite.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Stale cache after checker change | `CHECKER_OUTPUT_VERSION` bumped on every output-shape change; cache directory sharded by version; old shards pruned. CI test: load cached entry from a *bumped-version* fixture, confirm rejection. |
| Cross-file invalidation bug | Stress test: scripted edit loop. Run cold → cache populated. Edit a random file. Run cached. Diff diagnostics vs a fresh cold run. **Must be empty.** Loop over 100+ random edits before shipping. |
| cpp closure mis-capture | The marker-set approach catches `#include`, but cpp macro expansions from external `-D` flags are already in the config part of the key. Unit test: include file changes → entry invalidates. |
| Concurrent corruption | Atomic rename for writes; no in-place mutation. Already covered by Python `os.replace` semantics on all supported OS. |
| Cache too aggressive | `--cache off` and `--clear-cache` give the user escape hatches. LSP toast shows hit/miss counts when timings enabled, so divergence is observable. |
| `deps_consumed` is incomplete | Bigger risk. Every cross-file lookup the checker makes must funnel through an instrumented function — if a code path looks up an external symbol without recording it, that entry will be stale-cached. Mitigation: a small audit of `_resolve_call`, `apply_use_clauses`, and `_resolve` to identify every external-lookup site; instrument once at those points. CI: instrument-coverage check that fails if a new lookup site is added without registration. |

## Estimated payoff

| Scenario | Cold | Warm | Notes |
|---|---|---|---|
| N=0 (no edits) | 31 s | ~3 s | hashing + dep-validation only |
| N=5 typical edit | 31 s | ~5 s | check 5 files + their direct consumers (estimate: 0–30 consumers per master constants module change) |
| N=20 cross-module refactor | 31 s | ~10–15 s | wider consumer fanout |
| N=2,435 worst case | 31 s + ~1 s cache write overhead | 31 s | no benefit, slight overhead |

For the annotation cycle workflow specifically (N=1–3, the master constants modules), expected warm runtime is ~3–6 s.

## What this does **not** do

- Cold runs are unaffected. The floor stays at ~31 s.
- Cross-process parallelism is a separate axis. Cache and parallelism compose; they don't substitute.
- The Rust-extension path is also separate.
- Per-line caching within a file. The unit is the file. Sub-file caching would require parser-state caching which we explicitly decided not to do.

## Implementation plan

Branch: `content-hash-cache` (scoped, design-doc-first pattern).

Suggested commit sequence:

1. **`cache: capture cpp include closure during preprocess`** — modify `_run_cpp` to return `(expanded, line_map, included_files)`; thread through `_load_one`. No behavior change; sets up the dep-tracking primitive. Tests: existing cpp tests + new test asserting closure set membership.

2. **`cache: serialize/deserialize per-file artefacts`** — implement `to_jsonable` / `from_jsonable` for units, diagnostics, signatures, parameter values, type fields. Round-trip tests for each.

3. **`cache: introduce CHECKER_OUTPUT_VERSION + key derivation`** — pure pure-function module `dimfort.core.cache_key`. Tests: deterministic key for fixed inputs; key changes on each input axis.

4. **`cache: implement on-disk store with atomic writes`** — read/write/prune of msgpack.gz entries. Tests: round-trip, concurrent-write fuzz with multiprocess, prune at size limit.

5. **`cache: record cross-file symbol lookups (deps_consumed)`** — instrument `_resolve_call`, `apply_use_clauses`, `_resolve`. New `RecordingCtx` wrapper around `_Ctx` that captures lookups when active. Tests: known-input file with known USE clauses produces expected deps list.

6. **`cache: wire load phase to consult cache`** — read-only path first. CLI flag `--cache read-only`. Tests: cold run populates nothing; second run with stub cache produces identical output.

7. **`cache: wire dependency-validation + write-back`** — full read-write path. CLI flag `--cache read-write` becomes default. Tests: the stress test described above.

8. **`cache: --timings reports hit / miss / dirty counts`** — UX polish.

9. **`cache: LSP wiring + config keys`** — `dimfort.cache.mode`, `dimfort.cache.dir`. Settings reload on config change (precedent: 2026-05-21 hover settings reload).

10. **`docs: cache user guide`** — write a short user-facing page explaining when to clear, what triggers invalidation, where the cache lives. Cross-link from main docs.

Each commit is independently testable and bisectable. Each leaves the codebase in a runnable state. The default for the cache mode flips to read-write at step 7; before that, the cache is opt-in.

Stress-test gate: step 7 doesn't merge to main until 100 consecutive random-edit cycles show zero diagnostic divergence between cached and cold runs.

## Decision points still open

1. ~~Cache location default.~~ **Decided 2026-05-22: workspace-local `.dimfort-cache/`.** `--cache-dir` override available for users who want a different layout.

2. ~~Granularity of dep tracking.~~ **Decided 2026-05-22: start per-`module`.** Simpler step 5; ship the full pipeline first, measure invalidation rate on the real annotation cycle, refine to per-`(module, symbol)` only if warm runs are dominated by spurious invalidation.

3. **`USE module` without `only`.** Recorded as `(module, "*", "use_all")`. Any export-set change invalidates. Strict, but rare in modernized code. Acceptable.

4. **Should we cache the tree-sitter tree?** Re-parse cost is in the load phase (~7 s on LMDZ from the profile). Caching it would save warm-run reparse for dirty files. Probably **no** — dirty files are few in the warm case, and serializing TS trees is awkward. Revisit only if profile shows it dominates.

5. **First-run UX.** First run still pays cold cost + cache-write overhead. Should we surface a one-time toast / CLI line: "Building DimFort cache (~31 s) — subsequent runs will be faster"?
