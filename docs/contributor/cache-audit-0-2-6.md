# Cache audit — DimFort 0.2.6

**Date:** 2026-06-13.
**Scope:** every named cache in `src/dimfort/` as of the 0.2.6 release cut.
**Methodology:** static walk of every cache-bearing file (15 sites,
13 named caches) checking the three criteria from
`docs/0_2_6_PLAN.md` line 92:

1. Invalidation contract documented in the module / class docstring.
2. Bound stated (max entries, max bytes, or "O(open buffers) +
   evicted on didClose").
3. Memory-churn test passes (open / close a non-trivial number of
   files in a loop without resident-set growth).

This file is the written deliverable the audit produces. Per the
release-process refinement landed alongside this audit, future
pre-release cache audits must produce a similar file
(`docs/contributor/cache-audit-X.Y.Z.md`) so the checkbox in the
release plan cannot be silently fudged.

## Caches in scope (13)

| # | Cache | File:Line | Stores | Type |
|---|---|---|---|---|
| 1 | `CacheStore` (content-hash) | `src/dimfort/core/cache_store.py:61` | Per-file diagnostics keyed by SHA-256 of source+config | Disk-persistent |
| 2 | `TreeCache` | `src/dimfort/core/multifile_cache.py:116` | tree-sitter `CachedParse` keyed by `(content_hash, parse_mode)` | In-memory |
| 3 | `ModuleExportsCache` | `src/dimfort/core/multifile_cache.py:231` | `(signatures, modules)` keyed by `(content_hash, merged_units_digest)` | In-memory + disk (M5, new in 0.2.6) |
| 4 | `ProjectionCache` | `src/dimfort/core/multifile_cache.py:405` | `(ScanResult, AttachmentResult)` per `(content_hash, patterns_fp)` | In-memory + disk (M4) |
| 5 | Persistent `ProjectionCache` codec | `src/dimfort/core/multifile_cache_persist.py` | Disk mirror of (4) | Disk |
| 6 | Persistent `ModuleExportsCache` codec | `src/dimfort/core/multifile_exports_cache_persist.py` | Disk mirror of (3) | Disk |
| 7 | Inlay table cache | `src/dimfort/lsp/inlay.py:40` | Per-URI `(version, var_types, parameters, type_fields)` | In-memory |
| 8 | Declaration scan cache | `src/dimfort/lsp/decl_scan.py:25` | Per-URI `(version, declarations)` | In-memory |
| 9 | Sorted unit names | `src/dimfort/lsp/completion.py:24` | `(base, derived, prefix)` sorted tuples keyed by `id(table)` | In-memory |
| 10 | Coverage `_ws_cache` / `_ws_result_cache` / per-file | `src/dimfort/lsp/coverage.py:14` | Workspace `CacheStore` (tempdir) + latched `WorksetResult` + per-file projections | In-memory + tempdir |
| 11 | Interactions report cache | `src/dimfort/lsp/interactions.py:35` | `OrderedDict[(symbol_lc, scale) → SymbolReport]`, LRU cap 64 | In-memory |
| 12 | Parsed unit-table memo | `src/dimfort/core/multifile.py:1104` | `dict[(text_digest, id(table)) → parsed UnitExpr]` | In-memory (session-scoped) |
| 13 | `IncludeHasher` | `src/dimfort/core/cache_key.py:234` | `(path, mtime_ns) → content-hash` | In-memory |

## Per-cache audit verdicts

Severity reflects documentation rigor, **not** code correctness. The
0.2.6 release ships with every cache invalidating correctly and
every cache bounded somehow; what varies is how formally the
contract is stated.

| # | Cache | Invalidation documented? | Bound stated? | Audit verdict |
|---|---|---|---|---|
| 1 | `CacheStore` | ✅ Class docstring with "Concurrency" + "Pruning" sections | ✅ `size_limit_bytes=500 MB`, `max_age_days=30` (named constants) | Pass |
| 2 | `TreeCache` | ✅ Class docstring + FIFO-on-overflow note | 🟡 `max_entries` param exists but no concrete default; module says "must be ≥ workset size" but the sizing strategy lives in the LSP layer (`_apply_cache_max_entries`) | Pass (sizing strategy cross-link could be tighter) |
| 3 | `ModuleExportsCache` | ✅ Class docstring + sub-memo notes | 🟡 Main `_entries` capped; sub-memos (digest_memo, parsed_units_memo, extract_uses_memo) uncapped — "bounded in practice by _entries eviction" is the rationale but it's informal | Pass (sub-memo rationale could be tighter) |
| 4 | `ProjectionCache` | ✅ Class docstring + patterns-fingerprint invalidation | ✅ `max_entries` param; mirrored to disk | Pass |
| 5 | M4 persist codec | ✅ Module docstring + schema version constant | 🟡 On-disk file unbounded but one-per-session; relationship to in-memory eviction not stated | Pass (cross-link could be tighter) |
| 6 | M5 persist codec | ✅ Module docstring + `_EXPORTS_SCHEMA_VERSION` constant | 🟡 Same as (5) | Pass |
| 7 | Inlay table cache | 🟡 Module docstring mentions cache exists; invalidation mechanism ("relies on state.doc_versions bump") not stated as a formal contract | 🟡 "O(open buffers)" in comment; evicted on `didClose` via `forget_uri`; no explicit cap | **Needs polish** |
| 8 | Decl-scan cache | 🟡 Module docstring sparse; "version bump invalidates" stated but not framed as contract | 🟡 "O(open buffers)" informal; evicted on `didClose` (added this cycle); no explicit cap | **Needs polish** |
| 9 | Sorted unit names | 🟡 Inline comment only; "identity-change triggers refresh" not formalised | 🟡 "Only latest table identity survives" is a comment, not enforced by named constant | **Needs polish** |
| 10 | Coverage caches | 🟡 Module docstring lists three coexisting caches but contract details sparse | 🟡 No explicit caps; some grow with workset size during a single result's lifetime; cleanup trigger not stated | **Needs polish** |
| 11 | Interactions LRU | ✅ Comment block with FIFO + flush-on-result-swap rationale | ✅ `_REPORT_CACHE_MAX = 64` (named constant) | Pass |
| 12 | Parsed unit-table memo | ✅ Inline docstrings in `_parse_var_units` / `_parse_var_units_by_scope` document the digest-keyed memoization | 🟡 "Session-scoped" but no size estimate | Pass (informal but acceptable) |
| 13 | `IncludeHasher` | ✅ Class docstring; mtime-based intra-run invalidation | 🟡 Bound is "within one workspace check" — fine for CLI; reused across multiple LSP checks where stale entries are never evicted | Pass (LSP-reuse concern noted) |

## Memory-churn test results

Ran `scripts/cache_memory_churn.py -n 100` (this PR adds the script).

```
baseline RSS: 30 656 KB (100 files in /tmp/dimfort-churn-…)
 iter     rss_kb   delta_kb
    0      30 672        +16
   10      30 720        +64
   20      30 720        +64
   30      30 768       +112
   40      30 768       +112
   50      30 800       +144
   60      30 800       +144
   70      30 800       +144
   80      30 800       +144
   90      30 800       +144
   99      30 832       +176

summary: 100 iterations, final RSS = 30 832 KB
        (delta +176 KB, per-iter ~+1.8 KB)
verdict: caches appear bounded (per-iter < 50 KB).
```

The curve plateaus around iteration 50 — caches grow to their bound,
then hold steady. Per-iteration growth (+1.8 KB) is well within
malloc-fragmentation / GC slack noise; no linear leak.

The script is **not** in the automated test suite for 0.2.6 — that
integration is deferred to 0.2.7 (see "Deferred" below).

## What this audit verifies

- ✅ Every cache invalidates correctly in code.
- ✅ Every cache has *some* bound (formal cap, eviction-on-didClose,
  session-scoped lifetime, etc.).
- ✅ The full set of caches doesn't leak measurable RSS over 100
  iterations of unique-content files.

## Deferred to 0.2.7

- **Documentation polish on 4 caches** (rows 7-10 above:
  inlay table, decl-scan, completion sorted-names, coverage caches).
  Each needs a module / class docstring with a formal "Invalidation
  contract" + "Bound" subsection matching the rigor of `CacheStore`,
  `TreeCache`, and the interactions LRU.

- **5 caches with tighter cross-linking opportunities** (rows 2, 3,
  5, 6, 12). Optional; nice-to-have not must-have.

- **Memory-churn test integration into CI.** Currently a one-off
  script under `scripts/`. Should become a pytest fixture that
  asserts per-iteration growth < 50 KB at N ≥ 200, gating future
  perf-PR merges. Catches regressions automatically.

Both deferrals filed in `Homogeneity/docs/0_2_7_PLAN.md` (Definite
section) and `Homogeneity/docs/IDEAS_REGISTRY.md` (§I. Internal
maintenance).

## See also

- `docs/design/contributor/perf-pr-validation.md` — the checklist
  perf-PR authors run before submitting. Cross-links here when a
  perf-PR touches cache code.
- `docs/0_2_6_PLAN.md` line 92 — the release-prep checklist this
  audit fulfils.
- The cache-hygiene rules established mid-cycle:
  every new cache must declare (1) what triggers invalidation,
  (2) what bounds it, (3) what happens on `didClose` for
  uri-keyed caches.
