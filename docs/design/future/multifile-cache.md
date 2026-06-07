# Multifile cache — design spec (FUTURE)

**Status:** future feature, design exploration. Captures the
optimisation direction surfaced during the in-editor smoke walk for
the 0.2.4 coverage stats bar. Targets 0.2.5.

The goal is to make `check_files` cheap on repeated invocations
across a session by caching the load + index phases in addition to
the per-file diagnostic output (which the existing
`CacheStore` already handles). DimFort-wide infrastructure work
that benefits the active-file LSP loop, the `dimfort.checkWorkspace`
command, and the workspace coverage stats bar simultaneously.

## 1. Problem this solves

`check_files` is currently the bottleneck under every interactive
workflow that touches more than one file:

- Per-keystroke `didChange` runs the checker over the active file's
  use-closure. On a closure of 100 files this is in the seconds
  range every time.
- `dimfort.checkWorkspace` re-walks every workspace file on every
  invocation.
- The coverage stats bar's workspace-scope check (0.2.4) re-walks
  every workspace file on every refresh.

All three share a phase breakdown that the `--timings` flag exposes.
A representative measurement on a 1900-file real-world Fortran
codebase (cold cache):

| Phase | Time | Cached today? |
| --- | --- | --- |
| load (A) | 17.84 s | **No** — tree-sitter re-parses every file |
| aggregate (B) | 0.09 s | n/a (cheap) |
| index (C) | 3.51 s | **No** — module exports re-walked every file |
| check (D) | 28.63 s | **Yes** — diagnostics cached by `CacheStore` |
| total | 50.06 s | |

With the diagnostic cache hot, an edit-driven re-run drops the
check phase to ~5 s but still pays load + index = ~21 s every time.
That's the floor any caller hits, regardless of how cheap the
post-check projection or aggregation is.

## 2. What we'd cache

Two new caches, both keyed by file-content hash and held in memory
across `check_files` calls within one session:

### 2.1 Tree-sitter tree cache

Key: `(file_content_hash, parse_mode)` where `parse_mode` is `raw`
or `cpp` (because `.F90` files with active `#ifdef`s get a cpp-
expanded tree in addition to the raw tree).

Value: the parsed tree-sitter `Tree` object and its source bytes
— exactly what `_Loaded.tree` and `_Loaded.source` carry today.

When a file's content matches the cached hash, skip
`_ts.parse_text()` (or `_ts.parse_with_cpp()`) entirely and
return the cached tree.

Estimated saving on the reference workset: 17.84 s → ~10 ms for
the case where only one file changed between calls (every other
file's tree comes from cache, the changed file re-parses).

**Implementation note (2026-06-07):** the scanner in
`core/annotations.py` (`_scan_declarations` → `_ts.parse_text`)
runs its own parse over the same source as part of `scan_text`,
ahead of the parse the cache covers in `_load_one`. The tree
cache as initially landed therefore skips only the second of
those two parses on a hit. Eliminating the scan-internal parse —
either by hoisting the parse above `scan_text` and threading the
tree in, or by letting the scanner consult the same cache —
should roughly double the load-phase saving and is queued as a
follow-up in the 0.2.5 window.

### 2.2 ModuleExports cache

Key: `(tree_identity, var_units_table_identity)` — both inputs
to `collect_function_signatures_and_module_exports`. The tree
identity comes from the tree cache (so module exports invalidate
naturally when the tree re-parses); the table identity ties the
cache to the resolved var-units context.

Value: the `(sigs, modules)` tuple `collect_function_signatures_and
_module_exports` returns.

Estimated saving: 3.51 s → ~50 ms for the single-file-changed case.

## 3. Where the caches live

A new module — call it `core/multifile_cache.py` — exposes:

```python
class TreeCache:
    def get(self, key: TreeKey) -> Tree | None: ...
    def put(self, key: TreeKey, tree: Tree, source: bytes) -> None: ...

class ModuleExportsCache:
    def get(self, key: ExportsKey) -> tuple[Sigs, Exports] | None: ...
    def put(self, key: ExportsKey, value: tuple[Sigs, Exports]) -> None: ...
```

`check_files` accepts optional cache instances via two new params:

```python
def check_files(
    files: list[Path],
    *,
    tree_cache: TreeCache | None = None,
    exports_cache: ModuleExportsCache | None = None,
    ...
)
```

When unset (the default for current callers), behaviour is byte-
identical to today — no change to the CLI, no change to existing
tests.

The LSP layer instantiates one of each at server start and threads
them through every `check_files` call:

- Active-file `didChange` path uses them.
- `dimfort.checkWorkspace` uses them.
- The coverage stats bar's workspace check (`lsp/coverage.py`) uses
  them.

Lifetime: the LSP session. No on-disk persistence (the entries are
in-memory Python objects, not easily serialised).

## 4. Invalidation

Both caches invalidate naturally by content hash:

- A file whose content hash is unchanged returns the cached tree
  (no re-parse).
- An edit changes the content hash, the cache misses for that
  file, the tree is re-parsed and the new entry stored. The
  module-exports cache, keyed by tree identity, misses for that
  file too.

Edge cases:

- `cpp_defines` / `include_paths` change → tree cache key includes
  these (folded into `parse_mode` as part of the cpp config
  fingerprint).
- `units_file` change → exports cache key includes the resolved
  var-units table identity, which depends on the units file.

## 5. Memory cost

A tree-sitter `Tree` is in the tens-of-kilobytes range per file. A
1900-file workset → ~30-100 MB of cached trees, on the same order
as a single `WorksetResult`. Acceptable for an LSP that already
holds one `WorksetResult` in memory.

`ModuleExports` objects are smaller (signatures + module index for
one file). Negligible compared to the tree cache.

Hard memory cap: not in v1. If profiling shows runaway growth on
2400-file worksets, a simple LRU bound by entry count would
backpressure.

## 6. Alternative ideas (not pursued)

- **Disk persistence.** The existing `CacheStore` is disk-backed.
  We could persist trees + exports too. Rejected for v1 because
  tree-sitter `Tree` objects don't serialise well, and the
  hot-loop benefit is fully captured by in-memory caching.
- **Cross-process sharing.** Could let the CLI and LSP share a
  cache. Not in v1; the CLI's existing disk-backed `CacheStore` is
  fine for batch use, and the LSP's in-memory cache is local to
  the running server.

## 7. Adjacent optimisations (parked separately)

Three ideas surfaced during 0.2.4 design discussion that are
independent of the cache work but worth implementing on the same
side of the optimisation envelope:

### 7.1 Diff-skip on the LSP layer

Track each file's "meaningful state" (e.g., non-comment, non-
whitespace token list, or an AST hash with comments stripped). On
`didChange` / `didSave`, diff new vs old; if meaningful state is
unchanged, **do not mark anything dirty**. Comment edits and
whitespace-only edits skip the workspace check entirely.

Cost: a few hundred lines of LSP-side change detection plus a
per-file meaningful-state cache. Maybe a day.

Combined with the 0.2.4 async stats architecture, this turns
"edit a comment → ~5 s background refresh" into "edit a comment →
nothing fires."

### 7.2 Incremental WS aggregation

When only one file changed, instead of re-running `check_files`
over the whole workspace:

- Run `check_files` on just the changed file plus its reverse-
  dependency set (the files that `use` it).
- Reuse cached per-file `FileCoverage` projections for everything
  else (the per-file projection cache, originally parked in the
  coverage spec's §10.5 follow-ups).
- Patch the workspace aggregate with the new numbers.

Cost: requires a reverse-dep index (not currently maintained), an
incremental `check_files` API, and the per-file projection cache.
3-5 days, touches `core/multifile.py` and `core/workspace_index.py`.

Combined with the cache work in this doc, this would drop the
typical edit-driven WS refresh from ~5 s (cache-warm) to sub-
second.

### 7.3 Per-file projection cache

Cache the output of `project_file` keyed by per-file
`(tree_identity, diagnostics_identity)`. Tiny absolute win on its
own (the projection step is ~1-2 s out of the 30-50 s total), but
necessary infrastructure for §7.2.

Cost: small, ~half day.

## 8. Implementation order

If we ship all three in one 0.2.5 release window, the suggested
order:

1. **Tree cache** — biggest single win. Lands first; the active-
   file LSP loop immediately feels snappier.
2. **ModuleExports cache** — small absolute win, pairs with the
   tree cache (same invalidation model).
3. **Per-file projection cache** (§7.3) — small win, prerequisite
   for §7.2.
4. **Incremental WS aggregation** (§7.2) — once §7.3 is in place,
   this is incremental code.
5. **Diff-skip** (§7.1) — independent of the others; can ship
   anywhere in the sequence. Probably last because it's the most
   speculative (depends on what "meaningful state" should be).

The first two are the load-bearing changes. Sequence 3-5 can drop
out of 0.2.5 if scope balloons.

## 9. Migration

0.2.5 contents:

- New module `core/multifile_cache.py` with `TreeCache` +
  `ModuleExportsCache`.
- `check_files` gains the two new params; passes the trees /
  exports through to the load + index phases.
- LSP state gains two new cache instances; all internal
  `check_files` calls thread them through.
- Coverage spec §13.3 becomes coverage spec §13.2's actual
  release sequence (the bar gets its `automatic` default flip in
  the same release window).
- New `--no-tree-cache` and `--no-exports-cache` CLI flags for
  debugging cache-related bugs.

This doc moves from `docs/design/future/` to `docs/design/shipped/`
when the implementation lands.

## 10. Out of scope for this design

- **Persisting trees across sessions.** Trees are rebuilt from
  source on server start.
- **A general-purpose computation cache.** The two caches here are
  specifically tied to known-expensive phases of `check_files`. A
  generic memo wrapper would add ceremony for no benefit.
- **Parallelising the load phase.** Tree-sitter parsing is CPU-
  bound and Python's GIL limits process-level parallelism. Could
  be revisited as a `multiprocessing` follow-up if the cache alone
  proves insufficient.
