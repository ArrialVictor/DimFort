# Core — internal architecture

How `src/dimfort/core/` is organised: the unit-checking pipeline that both the
CLI and the language server run. For the *user-facing* annotation syntax see
[`docs/annotations.md`](../../reference/annotations.md) and the unit-algebra rules in
[`docs/unit-algebra.md`](../../reference/unit-algebra.md); this document is for people editing
the checker itself. The editor layer that sits on top is documented separately
in [`lsp-architecture.md`](lsp-architecture.md).

`core/` is already modular (~17 files, ~8.5k lines) — this is a map, not a
split plan. The biggest module, `ts_checker.py` (~2800 lines), is cohesive; see
[the split assessment](#is-ts_checkerpy-too-big) below.

## The one entry point

Everything funnels through **`multifile.check_files(paths, …)` → `WorksetResult`**.
The CLI (`dimfort check`, `dimfort interactions`) and the LSP server both call
it; nothing else runs the pipeline. `WorksetResult` (defined in `multifile.py`)
is the single payload every consumer reads: per-file `diagnostics`, the parsed
`trees`, the merged/scoped unit tables, cross-workset `signatures` and
`module_exports`, autocast events, and phase timings.

## The pipeline

`check_files` runs four phases (see the `multifile.py` module docstring for the
authoritative version):

1. **Phase A — load** *(per file, parallel via a thread pool; the tree-sitter C
   grammar releases the GIL).* Read source (`_source_io`), scan `@unit{…}`
   annotations + declaration sites (`annotations`), join them (`attach`), and
   parse to a tree-sitter tree (`ts_parser`).
2. **Phase B — aggregate annotations.** Merge the per-file `var_units` /
   `field_units` tables and parse each unit string to a `Unit` once (`units`).
3. **Phase C — index.** Walk every loaded tree to collect module exports and
   function/subroutine signatures (`symbols` / `ts_checker` collectors) so
   cross-file `use` resolution and H004 (call-arg mismatch) work.
4. **Phase D — check.** Per file, splice imported names into a local-scope copy
   of `(var_units, signatures)` via `apply_use_clauses`, then run
   `ts_checker.check`, which walks the tree and emits the diagnostics.

```
source ─▶ scan(@unit) + parse ─▶ attach ─▶ aggregate units ─▶ index sigs/exports ─▶ check ─▶ diagnostics
         [annotations]  [ts_parser] [attach]   [units]          [symbols/ts_checker]  [ts_checker]
```

A content-hash cache can short-circuit Phase D for unchanged files — see
[`content-hash-cache.md`](../shipped/content-hash-cache.md).

## Module map

### I/O and parsing
| Module | Owns |
| --- | --- |
| `_source_io.py` | Encoding-tolerant Fortran source reading + `discover_fortran_files`. |
| `ts_parser.py` | The tree-sitter Fortran parser wrapper (`parse_text`, node `walk`/`node_text`). The single place that touches the grammar. |

### Annotation pipeline (source → attached units)
| Module | Owns |
| --- | --- |
| `annotations.py` | Stage 1: a string-aware comment scanner that extracts every `@unit{…}` (`RawAnnotation`) and a tree-sitter declaration scanner that finds every declaration (`DeclarationSite`). |
| `attach.py` | Stage 2: joins annotations to declarations by physical line range (POST `!<` / PRE `!>`), producing an `AttachmentResult` (`var_units`, `field_units`, provenance, the per-variable continuation-attach rule shipped in 0.2.7). |

### Unit model
| Module | Owns |
| --- | --- |
| `units.py` | The `Unit` value type — a 7-slot SI dimension vector (M, L, T, Θ, I, N, J) with a `Fraction` prefactor — its parser and algebra (`*`, `/`, `^`, `compare`, `equal_dim`, `format_unit`), plus the symbolic-exponent / log-wrap types (`Exponent`, `ExpWrap`, `LogWrap`). See [`symbolic-exponents.md`](../shipped/symbolic-exponents.md) and [`symbolic-logwrap.md`](../shipped/symbolic-logwrap.md). |
| `unit_config.py` | Loads the unit table (`DEFAULT_TABLE` + `dimfort.toml` overrides) that maps unit names to `Unit`s. |

### Checker
| Module | Owns |
| --- | --- |
| `ts_checker.py` | The checker. `check(tree, …)` walks the tree and emits the H-series (H001 assignment, H002 operands, H003 intrinsic-arg, H004 call-arg) plus scale (S00x) and derivation (D1.x) diagnostics. Also the cross-workset collectors (`collect_var_types`, `collect_function_signatures`, `collect_module_exports`, …) and the **intended-public resolution API** (`Ctx`, `resolve_unit`, `assignment_homogeneity`, `resolve_member_chain`, `is_pure_numeric_constant`) that the editor layer and `interactions` consume. Marker semantics live in [`markers.md`](../shipped/markers.md); scale rules in [`scale.md`](../shipped/scale.md). |
| `symbols.py` | Parser-agnostic symbol data: `FuncSig`, `ModuleExports`, `apply_use_clauses`, the intrinsic-classification tables (LOG / EXP / dimensionless / …), and the `CODES` diagnostic registry (every diagnostic code + severity is declared here). |

### Diagnostics and provenance
| Module | Owns |
| --- | --- |
| `diagnostics.py` | The `Diagnostic` / `Severity` / `Position` types shared by CLI + LSP, `AutocastEvent`, and severity-override finalisation (`dimfort.toml`). |
| `trace.py` | Optional provenance tracing: which unit-algebra rule fired at each step (`with_trace`, `format_trace`). Drives the CLI `--trace` and the LSP detailed-hover tree. |

### Orchestration and discovery
| Module | Owns |
| --- | --- |
| `multifile.py` | `check_files` (the pipeline above) + `WorksetResult`. The orchestrator; imports nearly every other core module. |
| `workspace_index.py` | Workspace module discovery + workset resolution (which files a given target depends on through `use` chains). |

### Cross-site analysis
| Module | Owns |
| --- | --- |
| `interactions.py` | On-demand, per-symbol analysis: collect every read/write of one variable across the workset, tag the constraint each site places on its unit, and flag `X001` when two sites disagree. Built on the public checker API. See [`interaction-points.md`](../shipped/interaction-points.md). |

### Content-hash cache
| Module | Owns |
| --- | --- |
| `cache_key.py` | Per-file cache-key derivation (content hash + include closure). |
| `cache_store.py` | The on-disk cache store. |
| `cache_serde.py` | JSON (de)serialisation of cacheable artefacts (diagnostics, module exports). |

## Dependency direction

```
              cli.py / lsp/*          (call check_files / collect_interactions)
                    │
                    ▼
 workspace_index ─▶ multifile ◀─ interactions
                    │                  │
                    ▼                  │  (uses the public checker API)
                ts_checker ◀───────────┘
                ╱    │    ╲
          symbols  units  ts_parser
              │      │
              ▼      ▼
        diagnostics  unit_config
              │
              ▼
            trace  ←──(deferred import)──  units      annotations ─▶ attach
                                                       (load stage; on ts_parser/_source_io)
```

The flow is one-directional: leaf modules (`ts_parser`, `_source_io`, `units`,
`diagnostics`, `symbols`) depend on little else in `core/`; `ts_checker` builds
on them; `multifile` orchestrates; `cli` / `lsp` sit on top. `interactions`
sits beside `ts_checker` and consumes its **public** API, not its internals
(audit item #9; see [the LSP architecture note](lsp-architecture.md)).

The one intentional cycle is `units` ↔ `trace`: `units` calls `trace.trace_step`
to record which algebra rule fired, and `trace` references `units` types only
for formatting. Both sides use **function-local imports** to break the cycle at
module-load time. The cache modules (`cache_*`) are an orthogonal layer that
`multifile` drives; they don't participate in resolution.

## Is `ts_checker.py` too big?

At ~2800 lines it is the largest module, but unlike the old `server.py` (which
spanned ~6 unrelated LSP feature areas and was split) `ts_checker.py` is
**cohesive** — it is one thing (the checker) with three internal bands:

1. **Collectors** — `collect_var_types`, `collect_function_signatures`,
   `collect_module_exports`, … (Phase C inputs).
2. **The resolver** — `_resolve` and its per-node-type expression handlers
   (the bulk), exposed publicly as `resolve_unit`.
3. **The emitters** — `check()` and the statement-walk that produces H/S/D
   diagnostics.

Those bands are the natural seams *if* a split is ever wanted (e.g.
`ts_collect.py` / `ts_resolve.py` / `ts_check.py`). For now a split is **not
recommended**: the three bands share the `_Ctx`/`Ctx` context and a large body
of small tree-shape helpers, so separating them would trade one cohesive file
for three tightly coupled ones plus a re-export surface — net negative. Revisit
only if a genuinely independent concern grows inside it.
