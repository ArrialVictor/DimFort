# Changelog

All notable changes to DimFort are documented here. Format inspired by [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.1.2] — 2026-05-19

Second post-release hotfix.

- **README uses absolute URLs everywhere**. PyPI's readme renderer
  rejects relative image references; the project page on PyPI showed
  a broken `social_preview.png` (and a "Bad url scheme" error when
  opened directly). Every `(local-path)` link in the README now
  points at `https://raw.githubusercontent.com/.../main/...` (for
  images) or `https://github.com/.../blob/main/...` (for files).
  GitHub renders both forms; PyPI only renders the absolute form.
- **CI matrix expanded back to 3×3**. Runs `pytest` + `ruff` on
  Linux/macOS/Windows across Python 3.11, 3.12, and 3.13. Was
  reduced to a 3.11-only matrix earlier to save private-repo CI
  minutes; now that the repo is public, GitHub Actions minutes are
  unlimited so the wider grid is back.

## [0.1.1] — 2026-05-19

Post-release hotfix.

- **`dimfort --version` now reports the installed version**.
  Previously hardcoded in `src/dimfort/__init__.py` and missed the
  0.1.0 bump; the CLI printed `0.0.1` against a `0.1.0` wheel.
  `__version__` is now pulled from `importlib.metadata.version` so
  `pyproject.toml` stays the single source of truth.
- **README install instructions favour `pipx`**. The original
  developer-mode `pip install -e .[dev,lsp]` doesn't work for users
  on modern Homebrew Python (PEP 668 refuses system-wide pip
  installs). The README now leads with `pipx install 'dimfort[lsp]'`
  for users; the source-checkout / dev path is preserved under a
  separate heading.

## [0.1.0] — 2026-05-19

First public release. Pre-alpha; expect breaking changes between
`0.1.x` versions as the tool matures against real-world Fortran
codebases.

### Highlights

- **CLI**: `dimfort check FILE/DIR [...]` with per-file H-/U-summary;
  `dimfort lsp` over stdio.
- **Annotation pipeline**: scoped per `(subroutine|function, name)` so
  same-named parameters across two routines in a file don't alias.
- **Checker**: full H001-H004 (assignment, arithmetic, intrinsics,
  user-defined calls, derived-type fields, rational `**` exponents)
  across multi-file worksets.
- **Workspace orchestration**: `use`-chain resolution plus a
  workspace-wide top-level-procedure index for F77-vintage external
  procedures.
- **LSP server**: live diagnostics, hover (scope-aware bare
  identifier, derived-type member chains, call signatures, module-
  summary on `use foo`), inlay hints, go-to-definition for variables,
  callables, and module names, code lens, completion inside
  `@unit{...}`, "Add unit annotation" code action, the
  `dimfort.checkWorkspace` command, didClose republish,
  `workspace/inlayHint/refresh` push, tab-switch-safe republish, and
  a `/tmp/dimfort-lsp.crash` excepthook for silent-crash diagnostics.
- **Editor companions** (separate repos): VSCode, Neovim ≥ 0.11,
  Emacs (eglot + lsp-mode).
- **Project config**: `.dimfort.toml` with `[project] src_paths`,
  `[workset] external_modules` / `max_size`, `[parser] cpp_defines`
  / `include_paths`, `[units] file`.
- **Test coverage**: 228 unit + integration tests, ruff-clean.

### 2026-05-19 — Scope-aware annotations, external-procedure index, tab-switch republish

- **Per-scope `@unit{}` annotations** (`attach.py`, `annotations.py`, `ts_checker.py`): annotations are now keyed by `(scope_lc, name)` where `scope_lc` is the lower-cased enclosing subroutine/function (or `None` at module/file level). Two routines in one file declaring same-named params with different units no longer alias. Flat `var_units` view retained as a back-compat first-seen surface for callers that don't carry scope info. `_make_scoped_lookup` no longer falls back to flat lookup when in scope-aware mode — this closed a real false-positive path where unannotated wrapper params (e.g. NetCDF `put_var(..., v)`) were absorbing the unit of unrelated same-named variables in the workset. Diagnostic count on LMDZ trial dropped from 20 to 12 H-findings, all real (8 spurious, retracted in `LMDZ_FINDINGS.md`).
- **Scope-aware hover** (`lsp/server.py`): bare-identifier hover consults the per-scope table and reports the *enclosing routine's* annotation, not the first-seen across the workset.
- **Module-name hover and goto-def** (`lsp/server.py`, `lsp/ts_helpers.py`): hover on the module-name token of a `use foo` statement renders a summary of the module's exports — variables with units, contained procedures with signatures, `(N/M annotated)` count when there's a gap. Goto-def on the same token jumps to the `module foo` header.
- **Workspace external-procedure index** (`core/workspace_index.py`, `core/multifile.py`, `lsp/server.py`): a workspace-wide name map from top-level `SUBROUTINE`/`FUNCTION` to defining file, populated at LSP startup (~4.5 s on full LMDZ). Resolves F77-vintage external procedures (called without a `USE` clause), so goto-def, hover signatures, and H004 all follow such calls. `resolve_workset`'s BFS now expands via `CALL` edges too; topo sort honours them; the per-file workset cap pins direct deps (modules used + procedures called) so shallow callees can't be sliced out.
- **Tab-switch-safe re-publish** (`lsp/server.py`): the single global `_last_result` was overwritten on every `didOpen`/`didSave`/`didChange`. Navigating caller↔callee opened the callee's tab, flipping the workset to its downward-only deps. Switching back was silent (no LSP event), so subsequent goto-def/hover/inlay on the caller failed with "not in trees". New `_ensure_uri_loaded` re-publishes synchronously when the requested URI isn't in the current workset.
- **H004 message includes argument name** (`ts_checker.py`): `"argument 5 (pbaru) unit mismatch: …"` instead of `"argument 5 unit mismatch: …"`. Index kept too — formal names can repeat across `INTENT(INOUT)` slots or in overloads, so position remains the unambiguous identifier and the name is the friendly hint.
- **Silent-crash trace hook** (`lsp/server.py`, opt-out via `DIMFORT_CRASH_LOG=""`): `sys.excepthook` + `threading.excepthook` + pygls/asyncio logger handlers mirror Python tracebacks into `/tmp/dimfort-lsp.crash`. Doesn't catch native segfaults / SIGKILLs, but makes future Python-level crashes immediately actionable.
- **Tree-handler serialisation lock** (`lsp/server.py`): defensive lock around `_hover`, `_definition`, and `_inlay_hint` so they can't traverse the same tree-sitter Tree from different threads. Today's bug turned out to be elsewhere, but the lock stays as cheap insurance against tree-sitter's C library not being thread-safe.

### 2026-05-17 — CLI directory mode, LSP didClose persistence, U005 usage hint

- **CLI**: `dimfort check` accepts directory arguments and walks them
  recursively for Fortran sources. New `--summary` flag prints a
  per-file H-/U-diagnostic count breakdown after the diagnostic stream.
  `FORTRAN_EXTS` and `discover_fortran_files` extracted to
  `core/_source_io.py` so the LSP and CLI share one definition.
- **LSP**: `didClose` no longer publishes an empty diagnostic list for
  the closed file — it now republishes the most recent workspace-check
  diagnostics for that path, so the Problems panel keeps showing real
  issues after the user closes a tab.
- **Checker**: U005 ("variable used in unit-checked expression but
  has no `@unit{}` annotation") now appends `(e.g. used at line N)`
  pointing at the earliest usage site, so the user can jump from the
  unannotated declaration to a concrete consumer.
- **Branding**: `scripts/make_branding.py` renders a 1280×640
  `social_preview.png` at the repo root. Design palette mirrors the
  VSCompanion icon (translucent Clarendon F watermark, rounded
  frame, `[m·s⁻²]` glyph).

### Branch `ast-tree-sitter` (2026-05-16) — LFortran retired, tree-sitter takes over

Parser swap: LFortran subprocess → in-process tree-sitter Fortran grammar. The diagnostic pipeline, the LSP enrichments, and the on-disk caching are all re-implemented; CLI and config simplified accordingly.

- **Phase 0** (`df8a793`) — new `core.ts_parser`: parse_text / parse_file / walk, plus a CPP shim with line-map remap for `.F90` files. 18 unit tests pin the `&`-continuation drift case and the CPP shim's define/include/missing-include paths.
- **Phase 1** (`a823a73`) — declaration scanner ported. `core/annotations.py` walks tree-sitter `variable_declaration` and `derived_type_definition` nodes instead of the regex matcher; recovers names from `sized_declarator` / `init_declarator` wrappers. Net −155 / +174 lines; +1 test pinning the new "recover declarations after a syntax error" capability.
- **Phase 2** (`75459fd`) — full checker port. New `core/ts_checker.py` mirrors `core.ast_checker` 1:1 against tree-sitter nodes: `_resolve` for expressions, H001-H004 emitters, intrinsic dispatch, derived-type chain resolution, `**` exponent handling including negatives. `core/ast_multifile.py` switched to drive the new checker; 8 new unit tests at `tests/unit/test_ts_checker.py`.
- **Phase 3** (`d9d7c1c`) — LSP enrichments rewritten on tree-sitter. New `lsp/ts_helpers.py` (position containment, targeted walks, "is this the callee?" / "is this inside a declaration?" predicates). Hover, inlay hints, go-to-definition, and code-lens handlers all rewired; identifier-to-unit resolution shared with the diagnostic pipeline so there's a single source of truth. The most elaborate hover renderers (multi-variable expression / assignment hovers) intentionally skipped — they degrade to "no hover at that position" and can be reinstated later. Net +284 / −640.
- **Phase 4** (this commit) — LFortran path retired entirely. Deleted `core/lfortran.py`, `core/ast_checker.py`, `core/checker.py`, `core/ast_multifile.py`, `cache.py`, `core/parser.py`. New `core/symbols.py` holds the parser-agnostic data (FuncSig, intrinsic tables, ModuleExports, apply_use_clauses). `core/multifile.py` rewritten as a clean tree-sitter orchestrator (was the ASR orchestrator). CLI: `--backend`, `--lfortran`, `--no-cache`, `--cache-dir` flags removed; `cache` subcommand removed. Config: `[lfortran]` and `[checker]` sections silently ignored for backward compatibility but no longer exposed as fields. LSP: backend dispatch deleted, cache wiring deleted. Test count went from 287 → 183 — the deleted tests covered the deleted code.

### Branch `ast-only` (previous, preserved on `ast_and_asr`)

- **Phase 0 (spike, 2026-05-15)** — minimal AST-only checker landing as `core.ast_checker.check`. Walks LFortran's AST (no ASR involvement, no `lfortran -c`) and emits H001 + H002 for `Name` / `Num` / `BinOp(+,-,*,/)` / `Assignment` node combinations. Demonstrated end-to-end on `tests/fixtures/smoke_check.f90`: H001 fires on the dimensionally-wrong assignment, not on the clean one. Design notes in `docs/ast-only-design.md`; rest of the H/U series, cross-file `use`-chain resolution, intrinsics, derived types, casts, and array sections are TBD across Phases 1–5.
- **Phase 1 (single-file H/U series, 2026-05-15)** — `core.ast_checker` extended to cover the full single-file H-series: H003 (dimensionless-intrinsic violation), H004 (call argument mismatch), plus `Pow` with constant exponent (integer or rational via `Fraction.limit_denominator`), `UnaryMinus`, `Real` literal, and the six intrinsic categories (`DIMENSIONLESS`, `TRANSFORMING`, `TRANSPARENT`, `SAME_UNIT_ARG`, `PRODUCT`, `REDUCTION`) re-used verbatim from `core.checker` — no duplication of intrinsic tables. `collect_function_signatures(ast, var_units)` walks the AST for `Function` / `Subroutine` definitions and builds the same `FuncSig` table the ASR-side checker produces; `check()` accepts a `signatures=` kwarg so Phase 2 can pass a workset-wide map. New fixture `tests/fixtures/smoke_ast_phase1.f90` and integration tests `test_ast_phase1.py` (5 tests). Added `test_ast_parity.py` (3 fixtures) asserting the AST checker's H-series multiset matches the ASR checker's on `smoke_check.f90` / `smoke_intrinsics.f90` / `smoke_functions.f90` — the parity guard that catches regression once Phase 2+ extends scope further.
- **Phase 2 (cross-file use-chains, 2026-05-15)** — `core.ast_multifile.check_files_ast` orchestrates a full workset using AST only (no `lfortran -c`, no ASR). `ast_checker.collect_module_exports(ast, var_units)` walks `Module` nodes and produces a `ModuleExports` record per module (vars + signatures); `ast_checker.apply_use_clauses(uses, exports, ...)` splices the imported symbols into a consumer file's scope, honouring `only:` lists and `local => remote` renames. Missing modules surface as U007. New integration tests `test_ast_phase2.py` (4 tests) cover the cross-file H001/H004 path, workset-wide H-series parity with the ASR pipeline, order-independence, and the U007 emission. All 231/231 tests still pass.
- **Phase 3 (derived types + arrays, 2026-05-15)** — `ast_checker` now resolves derived-type access chains (`a%b%c`), array elements (`a(i)`), array slices (`a(:)`, `a(1:n)`). Adds `collect_var_types(ast)` and `collect_type_field_types(ast)` to build the per-file type maps from `Declaration` and `DerivedType` nodes; the resolver walks `Name.member` chains against those maps to reach the `field_units` table. `FuncCallOrArray` whose name matches a known variable now returns that variable's unit — closing the "is `a(1)` a function call or array indexing?" ambiguity LFortran's AST inherits. Fix to `Pow` and the transforming-intrinsics codepath to use `Unit.pow(exp)` instead of `Unit ** exp` (the latter falls through to `Fraction.__rpow__` and crashes on `float`). Extended parity test set to 5 fixtures including `smoke_derived_types.f90` and `smoke_rational_pow.f90` — all pass. New fixture `smoke_ast_phase3.f90` + 3 Phase 3-specific tests. Full suite: 236/236.
- **Phase 3 hardening (2026-05-15)** — LMDZ trial on `dyn3d_common/` (117 files) surfaced two bugs in the Phase 2/3 multifile orchestrator: missing U-series emissions (U001 scan errors, U002 unit-parse failures, the U006/U-conflict/U010 set from `_attachment_diags`) and a cross-file bare-name leak through `merged_var_units`. Fixed by reusing `multifile._attachment_diags`, emitting U001/U002 in the per-file pass, and scoping each file's check from its own `attachment.var_units` (cross-file imports still arrive explicitly via `apply_use_clauses`). LMDZ impact on `dyn3d_common`: false-positive H001s dropped from 47 to 6; previously-suppressed H004s now surface (11). New regression fixture `tests/fixtures/multifile_scope/` + `test_ast_scope.py`.
- **Phase 4 (backend selection, 2026-05-15)** — `[checker] backend = "ast" \| "asr"` lands in `dimfort.config.DimfortConfig`. CLI gains `--backend ast\|asr` on the `check` subcommand. LSP server reads `backend` from `initializationOptions` (falling through to config, then default `"asr"`). VSCompanion repo's `ast-only` branch adds `dimfort.backend` (enum) to the settings schema and forwards it. Backend is logged in the init notification (`backend=…`). 5 new config tests + 3 new CLI integration tests. Default stays `"asr"`; Phase 5 will flip it once the AST path has soaked.
- **Phase 4.6 (`.intfb.h` stubs + cpp_defines, 2026-05-16)** — `[lfortran] include_paths` and `[lfortran] cpp_defines` in `DimfortConfig` thread `-I` and `-D` through to LFortran. Unblocks LMDZ's ecRad headers (after stubbing them empty) and the `#ifdef ISO`-wrapped isotope-branch modules. `lf.dump_tree` decodes stdout/stderr with UTF-8 → Latin-1 fallback so French-comment files don't crash the workspace check. Adds the "DimFort: Check Whole Workspace" LSP command with phase-tagged ($/progress) per-file reporting ("loading 412/2435", "indexing modules", "checking"). LMDZ trial: 2435 files → 16 unloadable + ~13 cascade U007s (all LFortran 0.63 bugs).
- **Phase 5 (default backend → AST, 2026-05-16)** — `cli.py`, `lsp/server.py`, and VSCompanion `package.json` all now default to `backend = "ast"`. ASR remains selectable via `--backend asr` (CLI), `[checker] backend = "asr"` (config), or the `dimfort.backend` VSCode setting. Fixes a long-standing round-trip bug in `ast_multifile`: it converted parsed `Unit` objects back to text via `format_unit()` before handing to `ast_checker.check`, which then re-parsed — but `format_unit` emits Unicode (`m/s²`, `kg×m/s²`) that the parser doesn't accept. `ast_checker.check` now accepts `Unit` objects directly for both `var_units` and `field_units`; the multifile path passes them through without round-tripping. Caught when the existing CLI integration tests (which previously ran via ASR by default) started failing — they exercise H001 on a single-file workset where this round-trip had been silently dropping the only annotation.
- **Phase 6a (parallel loading, 2026-05-16)** — `check_files_ast`'s Phase A now uses a `ThreadPoolExecutor` (default workers = `cpu_count() - 1`). Subprocess.run releases the GIL while LFortran is running, so threads parallelise without the pickling overhead a process pool would impose. Progress callback fires in completion order under a small lock. LMDZ benchmark (2435 files, 8 cores): 223s → 170s (1.3×). Modest gain — GIL contention during large-AST JSON parsing now dominates the residual.
- **Phase 6b (AST cache, 2026-05-16)** — New `cache.load_single_tree_cached(path, mode='ast', …)` mirrors `load_trees_cached` but caches one tree at a time. Stored under `<cache>/<sha1>.ast.json`, keyed on content sha256 mixed with `include_paths` + `cpp_defines` (so config changes invalidate cleanly). `ast_multifile.check_files_ast` now accepts a `cache_dir=` kwarg and threads it into `_load_one`; the LSP passes `_cache_dir` (already resolved at initialize). LSP buffer overrides bypass the cache for that file only — sibling files still benefit. 3 new unit tests covering round-trip, include-path invalidation, and cpp-define invalidation. Warm-run LMDZ workspace check now dominates JSON-load cost rather than LFortran, dropping wall time to a fraction of the cold run.
