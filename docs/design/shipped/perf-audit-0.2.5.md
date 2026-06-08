# Performance audit — 0.2.5 cycle (SHIPPED)

**Status:** complete. This doc captures the full performance journey
of the 0.2.5 cycle: starting point, what shipped, what was
investigated and dropped, final numbers, and items deferred to
future releases.

Companion docs:

- [`multifile-cache.md`](multifile-cache.md) — the foundational TreeCache + ModuleExportsCache design.
- [`coverage-visualization.md`](../future/coverage-visualization.md) §13.2 — design history of the workspace coverage stats bar (auto → manual pivot).

## Headline result

| | Pre-0.2.5 | After 0.2.5 |
| --- | --- | --- |
| Warm workspace refresh (2435-file workset) | ~80 s | **~1.3 s in-editor** / 2.4 s bench |
| Cold workspace refresh (no caches) | ~80 s | ~65 s |
| Initial WorkspaceIndex scan | ~4 s | ~0.6 s (warm restart) |
| Per-edit typing freeze (auto-refresh era) | up to 80 s | **N/A — no auto-refresh** |

**Net warm refresh speedup: ~60× in-editor.** The "typing freezes for tens of seconds every 12 seconds" UX symptom that motivated the audit is gone entirely — auto-refresh removed, manual refresh completes faster than the editor renders a single frame.

## The starting point (pre-0.2.5)

Source: `scripts/bench_multifile_cache.py` against a real-world Fortran workset of 2435 files.

The 0.2.4 coverage stats bar shipped default-`disabled` because the underlying `check_files` was too slow to power an auto-refresh feature. The smoke walk surfaced a single root cause: **load + index phases ran without any cross-call caching**, so every refresh paid ~21 s of "redo work that didn't actually change."

| Phase | Pre-0.2.5 warm | Cause |
| --- | --- | --- |
| load (A) | 17.84 s | tree-sitter re-parses every file |
| aggregate (B) | 0.09 s | n/a |
| index (C) | 3.51 s | module exports re-walked every file |
| check (D) | 28.63 s | partial CacheStore coverage |
| total | ~50 s | |

Plus an additional ~30 s of overhead from the auto-refresh state machine (idle debounce, dirty tracking, lock contention) when triggered during typing.

## What shipped

The cycle landed across three server-side PRs plus the companion-side cleanup. Each is documented inline; the table here is the at-a-glance summary.

### Core caches (PR #64)

| Win | Impact |
| --- | --- |
| TreeCache + ModuleExportsCache | load 17.84 s → 6.49 s warm; index 3.51 s → 0.11 s warm |
| Scan-internal parse fix | doubled the load-phase win |
| Q1 — dedup file content hash | ~5 s cold saved (hashed twice for two cache keys) |
| Q2 — memoize `_digest_module_exports` by `id(exports)` | ~3 s warm saved on the cache-replay loop |
| Q3 — cache parsed `merged_var_units` | ~0.3 s warm saved (~120k `units.parse` calls collapse) |
| Q4 — cache `extract_uses` per file text | ~0.5 s warm saved |
| Q5 — unify coverage refresh cache with `state.cache` | **First refresh per LSP session: 50 s → 18 s** |
| W1 — parallel + dedup `scan_workspace` | initial scan 4.05 s → 2.78 s |
| M5 — yield `check_lock` during long refreshes | eliminates typing-freeze symptom |
| Auto-refresh removal + manual command | architectural simplification; ~150 lines deleted; freeze symptom unconditionally gone |

### WorkspaceIndex persistence (PR #65)

| Win | Impact |
| --- | --- |
| W3 — disk-persistent WorkspaceIndex | **Initial scan 3.2 s → 0.6 s on warm restart** (5.4×) |
| Scan-elapsed + cache-hit count in the index-ready toast | user-visible feedback on cache effectiveness |

### Per-file projection cache (PR #66)

| Win | Impact |
| --- | --- |
| M1 — `ProjectionCache` for scan + attach outputs | **Warm refresh 18 s → 2.4 s bench, 1.3 s in-editor (7.7×)** |

The M1 PR delivered the user's "one parse, one extract" architectural intuition: cache the *outputs* of the tree walks per file content hash, so unchanged files skip both `scan_text` and `attach` entirely on cache hit. This was the single biggest user-perceptible improvement of the cycle.

## Final hot-spot breakdown (warm refresh, 2.4 s on bench)

After all of the above, the warm pass distribution:

| Phase | Time | Share |
| --- | --- | --- |
| load | 177 ms | 7 % |
| aggregate | 8 ms | <1 % |
| index | 108 ms | 5 % |
| **check** | **896 ms** | **38 %** — CacheStore disk-validation dominates |
| file iteration + bookkeeping | ~1.2 s | ~50 % — Python overhead, dict ops, aggregation |

The "Python overhead" half is the floor any pure-Python implementation hits on 2000 files; it's not a single hot function, just the cumulative cost of iterating + bookkeeping. The CacheStore disk-validation half could be reduced with an in-memory mirror (see "dropped" below) but at 1.3 s in-editor it's not justified.

## Investigated and dropped

These items were on the audit's "could help further" list but were dropped after measurement / spike work.

### Tree-sitter Queries (cold-pass extraction)

The idea: replace the Python tree-walks in `_scan_declarations` and `collect_function_signatures_and_module_exports` with tree-sitter query patterns evaluated in C. Initial estimate said ~15 s saved on cold.

**Spike result (1-hour probe on a throwaway branch):**

- Tree-sitter Queries do work cleanly on tree-sitter-fortran's grammar — the API is solid and the queries we'd write for the relevant node types are short and obvious.
- **Actual cold-pass saving is ~2.5 s**, not 15 s. The initial estimate was wishful. M1 had already absorbed most of the "tree walk" cost via caching, leaving only the cold-path walks (which are smaller than estimated) for Queries to attack.
- Effort to ship cleanly: 3-4 days (refactor `_scan_declarations` + `collect_function_signatures_and_module_exports` + parity tests).
- Risk: correctness regression. The scanned outputs feed the annotation system; any divergence between the Query-based extraction and the Python-walk extraction would corrupt annotations workspace-wide.

**Verdict: dropped.** ~4 % cold-pass improvement is not worth a multi-day refactor with correctness risk. If a future profile shows tree-walk cost is the dominant remaining bottleneck, revisit.

### In-memory CacheStore mirror

The idea: the per-file diagnostic CacheStore reads each entry from disk to validate dep digests. A dict-keyed in-memory mirror would skip the disk read on hit. Initial estimate said ~5-6 s warm saved.

**Why dropped:** M1 made everything except the check phase essentially free, so the actual remaining warm cost from disk validation collapsed to ~0.6 s. Saving 0.6 s on a 1.3 s in-editor warm refresh would be unperceptible. Not worth even the half-day to implement.

### Auto-refresh state machine (originally shipped in 0.2.4)

The idle-debounce + dirty-tracking + daemon-worker design that drove the coverage stats bar. In-editor smoke testing during 0.2.5 confirmed it created more UX problems than it solved — the cost of `check_files` was fundamental (correctness requires re-checking every consumer of changed constants modules) and no caching could eliminate it; the user wasn't getting "fresh stats" but "freezing typing for 20 s every 12 s."

**Replaced with:** explicit `dimfort.refreshWorkspaceCoverage` command (registered via `workspace/executeCommand`). User clicks → progress indicator → fresh stats. Same data, no UX pathology.

### B (compact / shaved tree representation)

Initially considered as an alternative to M1: store a stripped AST with only the node types we care about. Dropped before any code was written: M1's "cache the outputs" achieves the same effect without redesigning the tree representation that all the LSP feature handlers depend on. See PR #64 commit history for the design discussion.

### Cython / mypyc / native compilation

Discussed but never started. After M1, no remaining hot Python loop justifies the build + packaging complexity. If a future pure-Python bottleneck emerges, revisit.

## Deferred to future releases

### M2 — Incremental workspace aggregation

The idea: when only one file changed, run `check_files` on that file + its reverse-dep set, reuse cached projections for everyone else, patch the workspace aggregate.

**Why not 0.2.5:** the auto-refresh removal made M2 less urgent — the user explicitly triggers refreshes now, so saving 1 s on each one barely registers. M2 also doesn't help the constants-module-edit workflow (where reverse-dep set = entire workspace).

**When it might be worth doing:** if/when a future release re-adds an auto-refresh-flavoured feature (e.g. background incremental indexing for the bar), M2 becomes the right shape.

### Per-file projection-cache disk persistence (W4)

Same shape as W3 but for `ProjectionCache`. Would make the FIRST refresh after a clean LSP start hit the cache instead of cold-extracting. Saves ~5-6 s on the first-refresh-of-session.

**Why not 0.2.5:** first refreshes are rare; cold-start UX is already tolerable after W1 + W3.

### Checker emitter algorithmic optimization

`_emit_h021_tyvar_positions` (19 s cold) and `_emit_u005_for_unannotated` (11 s cold) dominate the check phase on cold pass. M1 caches outputs so they're irrelevant on warm pass. On cold pass, an algorithmic refactor sharing a single AST traversal could halve cold time.

**Why not 0.2.5:** correctness-sensitive (these emit user-facing diagnostics); needs its own design pass; cold pass isn't a UX problem post-0.2.5.

## Reproduce

```sh
# Bench (cold/warm timing)
python scripts/bench_multifile_cache.py <workset>

# Profile (cumtime ranking)
python scripts/profile_multifile.py <workset> --top 30

# Bench scan_workspace in isolation (W1)
python scripts/bench_scan_workspace.py <workset>
```

Real-world workset for these benches: 2435 Fortran files, 1923 parseable, ~50k diagnostics across 5 severities, ~700 unique modules. Numbers in this doc are from a 2026-06-08 run on macOS 14 / M-series CPU.

## Lessons learned

- **Measure before estimating.** The Queries spike showed the original estimate was off by 6×. Spending an hour on a real measurement saved ~3 days of building the wrong thing.
- **M1's "one walk → bundle → cache" architecture won.** Once we stopped re-doing work, the system stopped feeling slow. The user's "one parse, one extract" instinct was the right architectural call.
- **Architectural simplifications are often the biggest UX wins.** Removing the auto-refresh state machine eliminated more user pain than any caching optimization. Less code, less complexity, better UX.
- **In-editor smoke walks catch what benches miss.** The check_lock contention symptom didn't show up in the bench (which doesn't have a typing simulator). The walk revealed it; M5 + the auto-refresh removal addressed it.
- **Don't ship "to round out the story."** Queries and the in-memory mirror were both candidates for "complete the performance update narrative." The data said the user wouldn't notice. Shipping them would have added complexity for no felt improvement.
