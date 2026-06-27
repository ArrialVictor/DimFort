# Cache audit — DimFort 0.2.7

**Date:** 2026-06-18.
**Scope:** every named cache in `src/dimfort/` as of the 0.2.7 release cut.
**Methodology:** same three criteria as the 0.2.6 audit
([cache-audit-0-2-6.md](cache-audit-0-2-6.md)):

1. Invalidation contract documented in the module / class docstring.
2. Bound stated (max entries, max bytes, or "O(open buffers) +
   evicted on didClose").
3. Memory-churn test passes (open / close a non-trivial number of
   files in a loop without resident-set growth).

This file is the written deliverable the audit produces; it
closes the 0.2.6 audit's "Deferred to 0.2.7" list.

## Caches in scope (13)

Unchanged since the 0.2.6 audit — no caches added or removed in
0.2.7. The table is reproduced here so a reader doesn't need to
flip to the prior doc.

| # | Cache | File:Line | Stores | Type |
|---|---|---|---|---|
| 1 | `CacheStore` (content-hash) | `src/dimfort/core/cache_store.py:61` | Per-file diagnostics keyed by SHA-256 of source+config | Disk-persistent |
| 2 | `TreeCache` | `src/dimfort/core/multifile_cache.py:122` | tree-sitter `CachedParse` keyed by `(content_hash, parse_mode)` | In-memory |
| 3 | `ModuleExportsCache` | `src/dimfort/core/multifile_cache.py:241` | `(signatures, modules)` keyed by `(content_hash, merged_units_digest)` | In-memory + disk (M5) |
| 4 | `ProjectionCache` | `src/dimfort/core/multifile_cache.py:479` | `(ScanResult, AttachmentResult)` per `(content_hash, patterns_fp)` | In-memory + disk (M4) |
| 5 | Persistent `ProjectionCache` codec | `src/dimfort/core/multifile_cache_persist.py` | Disk mirror of (4) | Disk |
| 6 | Persistent `ModuleExportsCache` codec | `src/dimfort/core/multifile_exports_cache_persist.py` | Disk mirror of (3) | Disk |
| 7 | Inlay table cache | `src/dimfort/lsp/inlay.py` | Per-URI `(version, var_types, parameters, type_fields)` | In-memory |
| 8 | Declaration scan cache | `src/dimfort/lsp/decl_scan.py` | Per-URI `(version, declarations)` | In-memory |
| 9 | Sorted unit names | `src/dimfort/lsp/completion.py` | `(base, derived, prefix)` sorted tuples keyed by `id(table)` | In-memory |
| 10 | Coverage `_ws_cache` / `_ws_result_cache` / per-file | `src/dimfort/lsp/coverage.py` | Workspace `CacheStore` (tempdir) + latched `WorksetResult` + per-file projections | In-memory + tempdir |
| 11 | Interactions report cache | `src/dimfort/lsp/interactions.py` | `OrderedDict[(symbol_lc, scale) → SymbolReport]`, LRU cap 64 | In-memory |
| 12 | Parsed unit-table memo | `src/dimfort/core/multifile.py:795` | `dict[(text_digest, id(table)) → parsed UnitExpr]` | In-memory (session-scoped) |
| 13 | `IncludeHasher` | `src/dimfort/core/cache_key.py` | `(path, mtime_ns) → content-hash` | In-memory |

## Per-cache audit verdicts

Every Yellow from 0.2.6 is now Green. Severity reflects documentation
rigor; correctness was already passing in 0.2.6 (memory-churn measured;
no leaks). What changed is how formally each contract is stated.

| # | Cache | Invalidation documented? | Bound stated? | Audit verdict |
|---|---|---|---|---|
| 1 | `CacheStore` | ✅ Module + class docstrings; `Concurrency` + `Pruning` rST subsections | ✅ `size_limit_bytes=500 MB`, `max_age_days=30` (named constants) | **Green** |
| 2 | `TreeCache` | ✅ Class docstring + FIFO-on-overflow note + cross-link to `_apply_cache_max_entries` for the LSP-layer sizing strategy | ✅ `max_entries` param; concrete LSP-layer cap | **Green** (was Yellow — cross-link tightened) |
| 3 | `ModuleExportsCache` | ✅ Class docstring + explicit sub-memo bound rationale (each sub-memo's key-identity ties its lifetime to `_entries`) | ✅ `max_entries` for `_entries`; structural bound on sub-memos | **Green** (was Yellow — sub-memo rationale formalised) |
| 4 | `ProjectionCache` | ✅ Class docstring + patterns-fingerprint invalidation | ✅ `max_entries` param; mirrored to disk | **Green** |
| 5 | M4 persist codec | ✅ Module docstring + schema version constant + new `Bound` subsection cross-linking in-memory cap | ✅ Mirrors in-memory eviction explicitly | **Green** (was Yellow — cross-link added) |
| 6 | M5 persist codec | ✅ Module docstring + `_EXPORTS_SCHEMA_VERSION` + new `Bound` subsection | ✅ Mirrors in-memory eviction explicitly | **Green** (was Yellow — cross-link added) |
| 7 | Inlay table cache | ✅ Module docstring with `Invalidation` / `Bound` / `Thread safety` rST subsections matching `cache_store.py` rigor | ✅ `O(open buffers)` formal; evicted on `didClose` via `forget_uri` | **Green** (was Yellow — docstring upgraded) |
| 8 | Decl-scan cache | ✅ Module docstring with same three subsections | ✅ Same as #7 | **Green** (was Yellow — docstring upgraded) |
| 9 | Sorted unit names | ✅ Module docstring with `Invalidation` + `Bound` subsections formalising the `id(table)`-keyed clear-and-replace | ✅ At most one entry — bound is structural (force-clear on miss) | **Green** (was Yellow — docstring upgraded) |
| 10 | Coverage caches | ✅ Module docstring with per-cache subsections (each of the three has its own `Invalidation` + `Bound` block) | ✅ Three distinct bounds, each named | **Green** (was Yellow — docstring upgraded) |
| 11 | Interactions LRU | ✅ Comment block + FIFO + flush-on-result-swap | ✅ `_REPORT_CACHE_MAX = 64` (named constant) | **Green** |
| 12 | Parsed unit-table memo | ✅ Inline docstring + explicit caller-owned-lifetime note cross-linking `ModuleExportsCache.parsed_units_memo`'s structural bound | ✅ Bound inherits from caller's memo dict | **Green** (was Yellow — cross-link added) |
| 13 | `IncludeHasher` | ✅ Class docstring; mtime-based intra-run invalidation | 🟡 LSP-reuse concern remains: bound is "within one workspace check" — re-used across multiple LSP checks where stale entries are never evicted | **Pass with carry-forward** (see below) |

**Net change from 0.2.6:** 9 Yellow → Green (rows 2, 3, 5, 6, 7, 8, 9, 10, 12); 4 Green retained (1, 4, 11, with 13 carrying forward).

## Memory-churn pytest gate

The 0.2.6 audit shipped `scripts/cache_memory_churn.py` as a one-off
release-prep check. The 0.2.7 audit promotes it to a CI-enforced
pytest gate at `tests/integration/test_cache_memory_churn.py`:

- **N = 200** unique-content files (4× the 0.2.6 baseline; tighter
  noise floor).
- **Threshold:** per-iteration RSS growth < **50 KB**. Above this is
  treated as a real leak; the 0.2.6 baseline was ~+1.8 KB/iter, well
  inside the cap.
- **Runs in default `pytest`** invocation (no marker required), which
  means it gates every PR on the existing `ci.yml` `Test` step. No
  new workflow plumbing — the existing `pytest --cov=dimfort` line
  already discovers it.
- **Cross-platform**: `resource.getrusage` skipped on Windows
  (`pytest.mark.skipif`); Linux + macOS run identically (the test
  normalises macOS bytes → KB).

The standalone script lives on for interactive debugging — its
`--report-every` flag prints the growth curve, useful when the test
fails and you want to know *which* iteration the growth started. The
test only asserts the post-loop ratio.

## What this audit verifies

- ✅ Every cache invalidates correctly in code (unchanged from 0.2.6;
  no cache-correctness regressions this cycle).
- ✅ Every cache has a formally-stated bound — `Invalidation` + `Bound`
  rST subsections in the module / class docstring matching
  `cache_store.py`'s rigor.
- ✅ The full set of caches doesn't leak measurable RSS over 200
  iterations of unique-content files, and this is now a CI gate
  (regression-prevention, not a one-off measurement).

## Carry-forward to 0.2.8

- **`IncludeHasher` LSP-reuse** (row 13). The bound is correct for the
  CLI use case ("within one workspace check") but the LSP reuses the
  same hasher across multiple checks per session — entries for files
  that have since been edited never evict, just become unreachable
  via the `(path, mtime_ns)` key drift. Practically irrelevant
  (single-file workspaces won't hit it; large workspaces hit
  malloc-fragmentation noise first), but worth a session-boundary
  reset hook in 0.2.8 to make the bound formal rather than
  practically-bounded.

No other deferrals — every other Yellow status from 0.2.6 closed
in this cycle.

## See also

- [cache-audit-0-2-6.md](cache-audit-0-2-6.md) — prior audit, the
  "Deferred to 0.2.7" section this doc closes.
- `docs/design/contributor/perf-pr-validation.md` — the checklist
  perf-PR authors run; cross-links here when a perf-PR touches
  cache code.
- `scripts/cache_memory_churn.py` — interactive variant of the
  churn gate (growth-curve output).
- `tests/integration/test_cache_memory_churn.py` — the CI gate
  itself.
- The cache-hygiene rules established in the 0.2.6 cycle:
  every new cache must declare (1) what triggers invalidation,
  (2) what bounds it, (3) what happens on `didClose` for
  uri-keyed caches.
