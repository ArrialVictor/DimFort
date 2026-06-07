# Performance audit — 0.2.5 cycle

Profiling-driven follow-up to the multifile-cache work. Profiles the
cold + warm `check_files` paths on the reference real-world Fortran
workset (2435 files), identifies hot spots, and ranks the remaining
wins by effort × impact.

Source: `scripts/profile_multifile.py`.

## Headline numbers (post multifile-cache)

| Pass | Wall time | Source |
| --- | --- | --- |
| cold | 76.9 s | `scripts/bench_multifile_cache.py` |
| warm | 19.3 s | same |
| speedup | 4.0× | |

cProfile adds ~50 % overhead, so cumulative-time rankings below
**come from the profile run** (cold 106.4 s / warm 28.4 s under
profiler) and are useful for relative sizing, not absolute timing.

## Warm-pass hot spots (where the 19.3 s actually goes)

| Function | cumtime | calls | per-call | Notes |
| --- | --- | --- | --- | --- |
| `_do_load` | 18.9 s | 2435 | 7.8 ms | load-phase parallel slot total |
| `_try_replay_from_cache` | 4.1 s | 2435 | 1.7 ms | CacheStore key + dep check |
| `scan_text` | 3.3 s | 2435 | 1.4 ms | walks cached tree for decls |
| `_digest_module_exports` | 3.3 s | **11352** | 0.3 ms | ~5 calls/file × 2435 |
| `_scan_declarations` | 3.1 s | 2435 | 1.3 ms | inside `scan_text` |
| `units.parse` | 2.6 s | **121086** | 22 µs | merged_var_units rebuild |
| `dump_module_exports` | 2.3 s | 10990 | 0.2 ms | feeds `_digest_module_exports` |
| `dump_unit_expr` | 2.1 s | 230545 | 9 µs | feeds `dump_module_exports` |
| `extract_uses` | 2.1 s | 2435 | 0.9 ms | walks tree for use clauses |
| `content_hash` | 1.6 s | **4870** | 0.3 ms | **2× per file** (tree + exports) |

## Quick wins (cheap, land in 0.2.5)

Five hot spots fall to easy fixes; each is independent and small. The
combined warm-pass estimate is ~9 s saved (19.3 → ~10-11 s).

### Q1. Hash file source once per `_load_one` — ~1.6 s

`_load_one` calls `content_hash(source)` for the TreeKey; the index
loop calls it again for the ExportsKey. Compute once, share via the
`_Loaded` record (a new `content_hash: str` field) or as a local
variable threaded through.

### Q2. Memoize `_digest_module_exports` by `id(exports)` — ~3-6 s

`_digest_module_exports` serialises the entire `ModuleExports` to
JSON then SHA-256s it. With the new `ModuleExportsCache`, the same
`ModuleExports` object recurs across calls, so an `id`-keyed memo
dict makes each module a one-time cost per session instead of N×
per-consumer-file. Memo is invalidated by the cache: if exports
change, you get a fresh object with a fresh `id`.

Savings double-count `dump_module_exports` (2.3 s) and a slice of
`dump_unit_expr` / `dump_exponent`.

### Q3. Cache `_parse_var_units(merged_var_units_text)` — ~2.5 s

`merged_var_units` is rebuilt every call from the per-file scan
results (now ~free thanks to the ExportsCache landing) but the
string-to-`UnitExpr` parse runs ~120k times per call regardless.
Memoize the result keyed by a digest of the input dict (same
function as `digest_merged_var_units` already in
`multifile_cache.py`).

### Q4. Cache `extract_uses` per file content hash — ~2 s

`workspace_index.extract_uses` walks each file's tree to find `use`
clauses. Same invalidation model as the TreeCache; a small in-memory
dict in `WorkspaceIndex` keyed by content hash.

### Q5. Don't sort `_walk` results when iteration order doesn't matter

Profile shows ~30 % of warm time goes to `_ts.walk` and its cursor
operations. Some call sites use generators and don't need sorted
output; an in-tree pass to spot redundant `list(_walk(...))` →
`sorted(...)` chains may save 1-2 s. Needs micro-profiling per
caller before committing.

## Medium wins (probably defer to 0.2.6+)

### M1. Per-file projection cache (spec §7.3) — ~3 s

Cache the per-file `attachment` + `var_units_by_scope` + `signatures`
keyed by content hash. Eliminates the scan-walk on hit (`scan_text`
3.3 s + `_scan_declarations` 3.1 s). Bigger structural change; pairs
with §7.2 incremental aggregation.

### M2. Incremental WS aggregation (spec §7.2) — ~5-10 s

Spec §7.2 already captures this. When only one file changed, instead
of re-running `check_files` over the whole workspace, run it on the
changed file + its reverse-dep set and patch the aggregate. Requires
a reverse-dep index + per-file projection cache (M1) + an
incremental API. 3-5 days.

### M3. Diff-skip on the LSP layer (spec §7.1) — biggest LSP UX win

Spec §7.1. Tracks per-file "meaningful state" (AST hash with comments
stripped, or token-list digest); comment / whitespace edits skip the
check entirely. Bench can't measure this because it's a check-not-run
shortcut, not a check-faster shortcut. ~1 day implementation. Could
ride into 0.2.5 since it's independent.

## Cold-pass observations (not low-hanging)

The cold pass spends 67 s of 107 s inside `ts_checker.check`. The two
single-biggest cumulative-time consumers are correctness emitters:

| Function | cumtime | calls | per-call |
| --- | --- | --- | --- |
| `_emit_h021_tyvar_positions` | 18.9 s | 1923 | 9.8 ms |
| `_emit_u005_for_unannotated` | 11.2 s | 1923 | 5.8 ms |
| `_resolve` | 10.2 s | 1.25 M | 8 µs |
| `_walk_expressions` | 10.2 s | 4.04 M | 2.5 µs |

These walk the AST per file and are the cost the CacheStore is built
to eliminate on warm passes (it does — check phase drops 24 s → 3 s
warm). The cold path is essentially "do the work the first time" —
optimising the emitters themselves would shave cold time but is
correctness-sensitive and not in the multifile-cache scope.

**Tree-cursor leaves:** `goto_next_sibling` (12.4 s) +
`goto_first_child` (8.7 s) = 21 s of pure tree-walk overhead on
cold. At 94.8 M cursor moves over 1923 files ≈ 49k per file —
plausibly high. A targeted look at `_walk_expressions` (called 4 M
times) might find an early-termination opportunity, but again
correctness-sensitive.

## Suggested ordering

1. **In this PR (0.2.5)**: Q1 + Q2 + Q3 + Q4. Four small commits,
   total ~9 s warm saving. Each is independent and easy to validate.
2. **Same release window if scope permits**: Q5 (low-confidence sizing
   — measure first) and §7.1 diff-skip (independent of cache work,
   biggest LSP-UX payoff).
3. **0.2.6+**: §7.3 per-file projection cache, then §7.2 incremental
   WS aggregation built on top of it. These together unlock the
   "single-file edit → sub-second" target.
4. **Out of scope** for now: check-phase emitter optimisation. Worth a
   separate audit when the cache infra is stable.

## Reproduce

```sh
# headline numbers
python scripts/bench_multifile_cache.py <workset>

# profile (cumtime ranking)
python scripts/profile_multifile.py <workset> --top 30

# isolate cache effect (no CacheStore)
python scripts/bench_multifile_cache.py <workset> --no-cache-store
```
