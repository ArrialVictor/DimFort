# Content-hash cache for workspace check

The content-hash cache lets `dimfort check` (CLI and LSP) skip the
per-file check phase for files whose inputs haven't changed. A warm
run replays diagnostics from disk; a cold run is unaffected.

Shipped on `main` 2026-05-22 in the `content-hash-cache` merge
(`ef3ecbc`); the stress-test harness and LSP wiring landed alongside.
The user-facing summary lives in [CHANGELOG.md](../../../CHANGELOG.md)
under "Content-hash cache for workspace check"; the user guide is
[docs/usage.md](../../usage.md#content-hash-cache). This doc is the **wire-format and
key-shape reference** — what bytes go into the key, what bytes go
into a cache entry, and when an entry becomes stale.

When this doc and the code disagree, the code in
[src/dimfort/core/cache_key.py](../../../src/dimfort/core/cache_key.py),
[cache_serde.py](../../../src/dimfort/core/cache_serde.py), and
[cache_store.py](../../../src/dimfort/core/cache_store.py) is the
authoritative reference.

## What the cache stores

A cache entry covers exactly **one file's check-phase output** — the
diagnostics for that file, plus a per-module digest signature used
for cross-file invalidation. Panel data, hover trees, scope tables,
parameter values, and unit tables are **not** cached: they're
recomputed every run from the load/index phases (which the cache
doesn't touch). The cache shortcuts the check phase only.

Payload shape (see `cache_serde` for the tagged-dict encoding):

```json
{
  "schema": 1,
  "deps_signature": {"<module_lc>": "<sha256-of-exports>", ...},
  "diagnostics": [<dump_diagnostic(d)>, ...]
}
```

Diagnostics are stored **already remapped to source coordinates**
(post-cpp `line_map` applied), so replay doesn't need to redo the
remap. The `Diagnostic.trace` field is deliberately dropped on dump
— reattached cached traces would point at stale lines on the next
checker change.

## Cache key

`compute_file_key` in
[cache_key.py](../../../src/dimfort/core/cache_key.py) returns the hex
SHA-256 of five length-prefixed sections:

| Tag | Bytes covered |
|---|---|
| `SRC\0` | raw source bytes of the file |
| `CPP\0` | sorted `(abspath, sha256(contents))` of every file in the cpp closure |
| `CFG\0` | canonical JSON of the per-file-affecting config subset (see below) |
| `VER\0` | `dimfort.__version__` |
| `OUT\0` | decimal `CHECKER_OUTPUT_VERSION` (currently `3`) |

Length-prefixing means concatenations are unambiguous. Any change to
any section produces a different key, which falls outside the lookup
path automatically — there's no "invalidate" call.

### Per-file-affecting config

`PER_FILE_CONFIG_KEYS` is the authoritative list of config dimensions
that contribute to the key. Every dimension along which the checker's
diagnostics can change for the same source bytes belongs here; if a
dimension is missing, edits to it will silently serve stale entries.
The current set is:

- `external_modules` — changes which `use foo` clauses resolve.
- `extra_defines`, `extra_include_paths` — change cpp expansion.
- `units_file_hash` — content hash of the project units table.
  Caller hashes the file (`_hash_file` in `multifile.py`).
- `diagnostic_severities` — `[diagnostics]` overrides applied inside
  `ts_checker.check` before diagnostics are cached. v2 of
  `CHECKER_OUTPUT_VERSION` was bumped to orphan entries written by a
  pre-fix LSP that baked the un-overridden severity in.
- `scale_mode` — opt-in S001 checking; toggling must invalidate.

Missing keys normalise to a typed empty value (`[]`, `{}`, `""`,
`False`) so the contributed bytes stay stable across runs where the
user has not configured that dimension.

### CHECKER_OUTPUT_VERSION

Hand-bumped integer in `cache_key.py`. Bump on any change to:

- the serialized payload shape in `cache_serde`, or
- the checker's diagnostic emission semantics in a way that changes
  what a cached file would have produced.

The cache directory is sharded by `v{N}/`, so a bump orphans old
entries — they fall outside the lookup path and get reclaimed by the
LRU sweep. Historical bumps recorded in the source: `v2` fixed
severity-poisoned entries written before the LSP applied severity
overrides; `v3` covered the `@unit_affine_conversion` directive
(which suppresses S002 / introduces S003 for identical source bytes).

### cpp closure

`_run_cpp` in [ts_parser.py](../../../src/dimfort/core/ts_parser.py)
parses the `# <lineno> "file"` markers emitted by the system cpp and
returns the set of distinct files referenced (excluding `<built-in>`,
`<command-line>`, and system headers — system headers are stripped
so a CI image with different libc paths doesn't poison the closure).
`.f90` files and `.F90` files with no `#` directives have an empty
closure.

`IncludeHasher` (also in `cache_key.py`) hashes each closure entry
once per `(path, mtime)` tuple. Missing files map to the literal
digest `"missing"` so the key still distinguishes "include was here
last time" from "include disappeared".

## Invalidation

A cached entry is valid when **both** hold:

1. **Self-hash matches.** The recomputed key (source + cpp closure
   + config + versions) matches the on-disk filename.
2. **Consumed dependencies are stable.** Every `module_lc → digest`
   pair in the entry's `deps_signature` matches the digest of the
   current workspace's `module_exports[module_lc]`.

The self-hash covers everything intrinsic to the file. The
`deps_signature` covers everything the checker pulled in from other
files via `use` clauses. The two together are necessary: a file's
diagnostics can change either because its own bytes changed or
because a module it imports from changed shape.

### Granularity: per-module

`deps_consumed` records workspace modules the file pulls in via
`use` clauses (computed by `deps_consumed_from_uses` in
[symbols.py](../../../src/dimfort/core/symbols.py)). Per-module rather
than per-symbol: `use phys_constants, only: pi` and `use
phys_constants` both record the module, and any change to
`phys_constants`' exports — even one unrelated to `pi` — invalidates
the consumer. This is strict but simple, and on real-world Fortran
codebases the invalidation rate stays well below the threshold where
finer granularity would pay back the bookkeeping.

The module-exports digest itself comes from
`_digest_module_exports` in `multifile.py`: it serialises the
`ModuleExports` record via `cache_serde.dump_module_exports`, then
SHA-256s the canonical JSON. A module that has disappeared from the
workspace maps to the sentinel digest `"absent"` so disappearance is
treated as "changed".

### Cascade

The invalidation cascade is **single-pass and breadth-first** in
the current implementation: the check loop walks files in load order
and validates each entry against the *current* `module_exports`
table, which was built from the load/index phases of this run.
Because the load + index phases always run from source (the cache
doesn't memoise them), `module_exports` always reflects the live
state. A file whose `use`d module's digest changed since its entry
was written is treated as dirty and re-checked; its fresh exports
don't feed back into `module_exports` because exports are populated
in the index phase, before the check phase begins.

In practice that's enough. The exports-vs-diagnostics path is
one-way (diagnostics depend on exports, not vice versa), so a single
check-phase pass converges. A future change that lets check-phase
output influence the workspace index would need an iterate-to-fixed-
point loop here.

## On-disk layout

```
{cache_dir}/v{CHECKER_OUTPUT_VERSION}/{first2}/{rest_of_hash}.json.gz
```

- `cache_dir` defaults to `{workspace_root}/.dimfort-cache/`
  (`DEFAULT_CACHE_DIR_NAME` in `cache_store.py`). The CLI's first
  path argument supplies the workspace root; the LSP uses the first
  workspace folder. Both honour an explicit override
  (`--cache-dir` / `cacheDir` initialization option).
- `{first2}` is the first two hex chars of the key, keeping per-dir
  fan-out under control on large workspaces.
- Payload format is JSON + gzip. msgpack would be ~2× more compact
  but isn't in the stdlib; the JSON form keeps a hand-edit/inspect
  path open and hasn't shown up as a hot spot in profiles.
- Atomic writes: `tempfile.mkstemp` in the same directory, then
  `os.replace` into place. Concurrent writers from CLI + LSP just
  overwrite with byte-identical content.
- Pruning: LRU keyed on `mtime`, triggered lazily at the end of a
  `--cache read-write` CLI run via `CacheStore.prune()`. Drops
  anything older than `DEFAULT_MAX_AGE_DAYS` (30), then trims
  oldest-first until total size is under `DEFAULT_SIZE_LIMIT_BYTES`
  (500 MB). The sweep is best-effort — permission errors and
  concurrent removals are swallowed; a missed sweep just defers
  reclamation. No explicit locking.

Corrupt entries (gzip truncated, JSON malformed) are unlinked on
read and counted as a miss, so the next write fills the slot
cleanly.

## CLI surface

```
dimfort check --cache off          # default — no read, no write
dimfort check --cache read-only    # consult cache, never write
dimfort check --cache read-write   # consult and update
dimfort check --cache-dir DIR      # override cache location
dimfort check --clear-cache        # rm -rf the cache root, then run
dimfort check --timings            # adds hit / miss / dirty / write counts
```

Default is `off` so the previous behaviour is preserved bit-exact
when the flag is not passed. `--clear-cache` works in combination
with any mode; combined with `read-write` it repopulates from
scratch.

`--timings` reports phase wall-clocks (`load` / `aggregate` /
`index` / `check` / `total`) and a Cache section with hit / miss /
dirty / write counters. The counters are also surfaced by the LSP
status line when cache mode is non-`off`.

## LSP integration

The server reads `cacheMode` and `cacheDir` from
`initializationOptions` on `initialize`; see
[docs/lsp.md](../../editor-integration/lsp-protocol.md) for the client-side shape. `cacheMode`
takes the same `off | read-only | read-write` vocabulary as the
CLI flag, and defaults to `off`. `cacheDir` defaults to the
workspace-folder default if omitted.

Settings are written once at initialize-time today and do not
hot-reload on `workspace/didChangeConfiguration`. Companions that
expose a "toggle cache" UX restart the server to apply the new
mode.

## Concurrency

- **Reads** are lock-free. Entries are immutable once their atomic
  rename completes; a missing entry is just a miss.
- **Writes** go via temp file + `os.replace`. A second writer
  racing on the same key writes byte-identical content, so the
  result is byte-stable regardless of who wins.
- **Pruning** is unsynchronised. If two processes prune
  concurrently, one may `unlink` a file the other already removed
  — that raises `OSError` and is swallowed. There is no global
  `flock`.

No process-level locking is needed on the hot path. The trade is
that two processes can pay the same compute cost simultaneously on
a cold key; that's deliberate.

## Cross-references

- [docs/usage.md#content-hash-cache](../../usage.md#content-hash-cache)
  — user-facing guide: when to clear, what triggers invalidation,
  where the cache lives.
- [docs/lsp.md](../../editor-integration/lsp-protocol.md) — `cacheMode` / `cacheDir` shape in
  `initializationOptions`.
- [docs/design/panel-info.md](panel-info.md) — panel data is **not**
  cached; it's recomputed every run from the load/index phases.
- [docs/design/markers.md](markers.md) — marker glyphs are derived
  from diagnostics; cached diagnostics replay through the same
  finalisation path, so markers stay consistent with a cold run.
- The internal findings log captures the stress-test numbers
  (cold/warm parity over 100+ random-edit cycles) used to gate the
  initial merge.

## Open questions

1. **Per-symbol granularity.** The current per-module digest
   invalidates a consumer on any export-set change to a module it
   imports. On a benchmark workspace the spurious-invalidation rate
   is low, but a master constants module touched in a refactor
   currently cascades to every consumer regardless of which symbol
   moved. Per-`(module, symbol)` `deps_consumed` would localise that;
   we haven't measured the bookkeeping cost yet.

2. **First-run UX.** The first run after a `dimfort` upgrade or a
   `CHECKER_OUTPUT_VERSION` bump pays the full cold cost plus
   cache-write overhead. There's no surfaced "building cache"
   indicator today; users see a normal cold run with no hint that
   subsequent runs will be faster.

3. **Settings hot-reload.** `cacheMode` and `cacheDir` are read
   once at `initialize` time. Changing them via
   `workspace/didChangeConfiguration` is silently ignored until
   the server restarts. The hover-settings reload precedent from
   2026-05-21 could extend here.

4. **Cross-run include hashing.** `IncludeHasher` memoises within a
   single run via `(path, mtime)`. A persistent on-disk map would
   skip re-hashing shared headers (e.g. `netcdf.inc`) across runs;
   the per-run cost isn't large enough to have justified the work
   yet.

5. **Tree-sitter tree caching.** Re-parse cost in the load phase
   is in single-digit ms per file. Caching trees would shave dirty-
   file reparse off warm runs; serializing tree-sitter trees is
   awkward and the saving is small, so this has stayed deferred.
