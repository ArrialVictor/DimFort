# Changelog

All notable changes to DimFort are documented here. Format inspired by [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fix: scope bleed — unannotated param inheriting a sibling routine's unit

- An annotated formal parameter leaked its unit to a same-named
  **unannotated** parameter in a sibling routine of the *same file*,
  via the flat first-seen `var_units` fallback in `_Ctx.unit_for` (and
  the call/array resolver's flat scan). `_make_scoped_lookup` already
  avoided this, but `unit_for`'s `if self._by_scope_lc:` guard treated an
  *empty* scoped dict as "not scope-aware" and re-enabled the fallback.
- Added an explicit `_Ctx.scope_aware` flag (set whenever a by-scope
  table is supplied, even empty). In scope-aware mode resolution goes
  `(scope, name)` → `(None, name)` only — never the flat map. `use`-imports
  (which previously resolved through the flat map) are now merged into the
  by-scope table under the `(None, name)` layer in `multifile` and stored
  on the result, so the LSP resolves them identically. Regression tests in
  `test_var_units_scoping.py`.

### `@unit_assume` escape hatch for un-derivable expressions

- **`@unit_assume{ <unit> : <reason> }`** — a statement-level directive
  that tells the checker to stop *deriving* an assignment's RHS unit
  (suppressing D1.4 and any interior fire) and instead treat the result
  as the asserted `<unit>`. Intended for expressions DimFort cannot
  analyse dimensionally — chiefly empirical power-law fits that raise a
  dimensioned base to a non-rational exponent (e.g. the Brandes-2007
  snow-density law `rho = 1.e3*0.178*(r*2.*1000.)**(-0.922)`), which no
  amount of PARAMETER-aware exponent work (OQ4) can close.
- **Suppresses derivation, not consistency.** The asserted unit is still
  checked against a declared LHS, so an assume that contradicts the
  variable's `@unit{}` still fires H001 — it can't mask a real conflict.
- **`reason` is mandatory** (a category + free text) so every assumption
  is auditable. Each use emits a **`U020` INFO** note at its site, and
  the directives are greppable (`grep @unit_assume`).
- Written as a trailing `!< @unit_assume{...}` on the assignment. v1
  keys by source line, so it is correct for raw-parsed files; a
  cpp-expanded `.F90` whose lines shift under preprocessing is a known
  limitation. CLI now renders INFO/HINT severities with their own label
  (previously everything non-error printed as `warning`).

### Side-panel info endpoint + R4.4 literal-init autocast

- **`dimfort/panelInfo` LSP request.** Returns structured data for an
  editor side panel: the unit-algebra tree for the expression under
  the cursor, plus the declarations of every *enclosing scope*
  (subroutine / function / module / program), stacked outermost-first.
  Each editor renders it natively (Neovim split shipped; Emacs /
  VSCode to follow). Spec: [`docs/design/panel-info.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/panel-info.md).
- **R4.4 — literal initialization autocast.** When the sole RHS of an
  assignment is a pure-numeric constant (literal, unary-minus literal,
  or arithmetic of literals), it takes on the LHS's unit and no
  diagnostic fires — `t = 2.0` where `t : s` is initialization, not an
  implicit cast. The existing D1.5 H010 still fires for literals buried
  in compound expressions (`t = c + 2.0`). Documented in
  [`docs/unit-algebra.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/unit-algebra.md).
- **`AutocastEvent` + `WorksetResult.autocast_events`.** Each R4.4 fire
  is recorded as a structured event (file, span, literal text, inferred
  unit) for audit tooling / a future strict-mode that promotes them to
  Information-severity diagnostics. Not part of the diagnostic stream.
- **`ts_checker._assignment_homogeneity`** is the single source of
  truth for an assignment's verdict (homogeneous / autocast /
  wrapper_untag / mismatch / unresolved) + its units. The checker and
  every LSP render site (hover-short, hover-detailed tree, panel) call
  it, so markers can no longer disagree with the diagnostic stream.
  Fixes a panel bug where `d = fall_distance(t)` (matching units)
  showed 🟡 instead of 🟢.
- Assignment rows in hover trees and the panel no longer show a `: ?`
  unit column — assignments are statements, not expressions, so only
  the marker is shown.

### Content-hash cache for workspace check

- **Per-file content-hash cache.** Workspace checks can now cache the
  per-file check phase keyed by `(source bytes, cpp closure hashes,
  per-file config, DimFort version, OUTPUT_VERSION)`. On a warm cache
  the per-file check is replayed from disk instead of recomputed.
  LMDZ-scale measurement: cold 33 s → warm 20 s; the check phase alone
  drops from 15 s to ~3 s. Cold-run floor is unaffected.
- **Per-module dependency invalidation.** Every cached entry records the
  set of workspace modules its file consumed via `use` clauses; when
  any of those modules' exports change, the entry is flagged dirty and
  re-checked. Self-edits invalidate only the edited file plus its
  direct consumers.
- **CLI surface.** `--cache {off|read-only|read-write}` (default off),
  `--cache-dir DIR`, `--clear-cache`. `--timings` gains a Cache section
  with hit / miss / dirty / write counts.
- **LSP surface.** `initializationOptions.cacheMode` and
  `initializationOptions.cacheDir`. Workspace-check completion toast
  appends `[cache: N hit / N miss / N dirty]` when active. Restart the
  server to change mode.
- **Storage.** `{workspace}/.dimfort-cache/v{N}/{first2}/{rest}.json.gz`
  by default. Atomic-rename writes, corrupt-entry recovery on read,
  LRU sweep at 500 MB / 30 days at end of read-write runs.
- **Correctness gate.** 100-iteration parametrised stress test
  (`tests/unit/test_cache_stress.py`): cold-populate → random edit →
  cached run vs fresh cold run must produce byte-identical diagnostics.
  Documented in [`docs/design/content-hash-cache.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/content-hash-cache.md);
  user guide in [`docs/usage.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/usage.md#content-hash-cache).
- **Key dimensions covered.** Source bytes, cpp include closure,
  `external_modules`, `extra_defines`, `extra_include_paths`, the
  project units-table file contents (`units_file_hash`), and
  `[diagnostics]` severity overrides. Editing any of these
  invalidates affected entries.

### Workspace check perf

- **Phase-C consolidation.** `collect_parameter_values` folded into the
  existing combined `variable_declaration` walk (`collect_var_types_
  type_fields_and_parameter_values`). Recovers the ~2 s LMDZ check-
  phase regression introduced by the OQ4 PARAMETER-aware exponents
  work. Same pattern as the 2026-05-17 var-types + type-field-types
  merge.

### Unit-algebra for LOG / EXP-tagged quantities (Phase B)

Three new diagnostic classes cover wrapper arithmetic. Each is
emitted with an existing `H001` / `H002` severity code; the
specific rule appears as a `(D1.x)` tag in the message and as a
rule ID (`R5.6`, `R6.5`, etc.) in `--trace` output. The full rule
set is documented in
[`docs/unit-algebra.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/unit-algebra.md).

- **`LogWrap` and `ExpWrap` unit types** alongside the existing
  `Regular` 7-tuple. Wrappers form a recursive `UnitExpr` tree;
  `LOG ∘ EXP` and `EXP ∘ LOG` cancel at construction (R2.1 / R2.2),
  and any wrapper around dimensionless collapses immediately
  (R2.3). Annotations accept `@unit{LOG(Pa)}`, `@unit{EXP(K)}`,
  and nested forms.
- **Intrinsic typing.** `LOG` / `LOG10` / `LOG2` of a unit `U`
  produces `LOG(U)`; `EXP(U)` produces `EXP(U)`. Was previously
  fixed to require dimensionless input via `H003`. Cancellation
  through the smart constructors means
  `EXP(LOG(psol) − dgeop/RT)` now types cleanly to `Pa` (the
  hydrostatic-projection idiom).
- **`D1.2` Undefined wrapper op** (`H002`). Fires on
  `LOG(p) * LOG(q)` (R5.6), `LOG(p) * mass` with non-dim mass
  (R5.7), `LOG(p) ** 2` (R5.9), `EXP(t) * pressure` (R6.7),
  and `LOG(p) * EXP(t)` (R7.1).
- **`D1.3` Undefined wrapper sum** (`H002`). Fires on `EXP(t) +
  EXP(u)` (R6.5), `EXP(t) + variable` (R6.6), and `LOG(p) +
  EXP(t)` (R6.6 for `+/-` between wrappers).
- **`D1.4` Runtime-dependent unit** (`H001`). Fires when a power
  exponent or the scalar coefficient on a `LogWrap` isn't a
  literal rational (R4.3 / R5.5).

### Implicit casts and untags (Phase A continuation + Phase C)

- **`D1.5` Implicit literal cast** (`H010` warning, severity
  unchanged). Captures the `1. + speed` regularisation idiom.
  Now applies to `ExpWrap + literal` too (R6.6 demotion).
- **`D1.6` Implicit wrapper untag** (`H010` warning) — new in
  Phase C. Assigning a `LogWrap(Pa)` or `ExpWrap(K)` to a
  Regular LHS whose unit matches the wrapper's inner is allowed
  with a warning instead of firing `H001`.

### Trace mechanism (Phase D)

- **`Provenance` records and `with_trace()` context manager** in
  `dimfort.core.trace`. Hooks at every rule fire in `combine`,
  `power`, `wrap_log`, `wrap_exp`. Off by default — `trace_step()`
  is a single dict lookup in the hot path when no trace is active.
- **`dimfort check --trace`** — prints the rule chain underneath
  each diagnostic. Each line reads
  `→ operands  ⇒  result  [Rx.y]`.
- **Per-statement traces on `Diagnostic.trace`** — the checker
  opens a fresh `with_trace()` around each top-level statement
  when tracing is active so each diagnostic carries just its
  statement's chain.
- **LSP `traceHoverEnabled` flag** — when on, hovers inside an
  assignment render the whole expression as an ASCII tree with
  per-node units and rule IDs. Header reads `🟢 / 🔴 / 🟡 DimFort`
  for OK / mismatch / unresolved respectively.
- **Trace hover beyond assignments** — the same flag also fires
  inside call arguments, IF / ELSEIF / WHERE conditions, DO loop
  bounds, and SELECT CASE selectors. There's no LHS to compare
  against, so the header uses the neutral `🟡 DimFort` marker and
  the tree is rooted at the cursor's sub-expression.

### Hover UX overhaul (Phase E)

- **Per-surface hover layouts.** Three settings —
  `dimfort.hover.functionCalls`, `dimfort.hover.subroutineCalls`,
  `dimfort.hover.expressions` — each Short or Detailed. Replaces the
  single `traceHoverEnabled` toggle (kept as a legacy master switch).
- **Call short** renders a header + one row per arg pairing formal
  vs. actual unit with 🟢/🟡/🔴 markers; aggregate header marker
  reflects the worst row.
- **Call detailed** adds a sub-tree under any computed actual
  showing how its unit was derived.
- **Expression short** — one-line homogeneity check on assignments
  (`LHS : u  ◂  RHS : u`) and relational expressions; bare hover on
  identifiers and literals; resolved-unit hover on computed
  sub-expressions.
- **Expression detailed** — the unit-algebra rule-chain tree.
- **Notation unified.** `:` between expression and unit, `◂`
  between target slot and value, 🟢/🟡/🔴 in row markers and headers.
- **Spec at [`docs/hover-ui.md`](docs/hover-ui.md)** — six layouts
  (3 surfaces × 2 levels), notation legend, conflict-resolution
  rules ("most-specific wins"), examples by cursor position.
- **Most-specific wins** dispatch: identifier, member, callee, and
  numeric-literal hovers run first; the expression-context hover
  fires only when nothing more specific matched.
- **Per-row markers in the trace tree.** Each row in the unit-algebra
  tree now carries a 🟢/🟡/🔴 marker in a right-aligned column. A
  🔴 propagates upward through `*` / `/` / function calls — anywhere
  a downstream homogeneity violation makes the parent unresolvable —
  so the reader can spot the failing spine at a glance. Header
  marker aggregates the worst row (incl. nested violations).
- **Line-continuation parser fix.** Fortran's `&` continuation
  appears as a sibling of `=` in the assignment AST; the previous
  RHS splitter picked it as the RHS instead of the actual
  expression on the next line. The hover now lands on the real
  expression for any continued assignment.

### PARAMETER-aware exponents and literals (OQ4)

- **`p ** kappa` no longer fires D1.4 when `kappa` is a PARAMETER**
  with a literal-rational initializer. The scanner collects every
  `REAL, PARAMETER :: name = value` declaration where `value` reduces
  to a `Fraction` (literal, `-literal`, or simple arithmetic of those:
  `2./7.`, `0.5`, `-3.14`, etc.). The resolver consults that table
  during `**` exponent evaluation and during the literal-detection
  inside `combine` for sign-propagation and log-wrapper math.
- **Scope.** Covers PARAMETERs declared *in the same file* as the
  expression. Doesn't yet handle `REAL` variables that are set once at
  runtime (the `kappa = R/Cp` idiom common in atmospheric dycores).
  Closing those needs a SAVE-once-init pass or an annotation-based
  literal-value opt-in — both deferred.
- **Surfaces broadened.** Same logic applies to:
  - `+` / `-` literal-zero detection (the existing R4.x sign-prop
    edge cases).
  - LogWrap multipliers (resolves a chunk of D1.4 fires from runtime
    `REAL`s annotated as dim'less but used to scale a `LOG()` —
    pending the matching annotation-based extension to fully close
    the Tetens family).
- **API.** New `ts_checker.collect_parameter_values(tree, source)`
  returns `{name_lc: Fraction|int}`. New `_Ctx.parameter_values` field
  (defaults empty, so existing callers stay compatible). LSP hover
  / inlay / trace paths all populate it per-tree alongside `var_types`.

### Symbolic exponents

Extends the unit algebra so that dimension-slot exponents can carry
**named symbolic terms** (constant-coefficient linear forms over Q),
not just literal rationals. Closes the family of D1.4s where the
exponent is a runtime `REAL` (the Exner-kappa pattern in atmospheric
dycores) that OQ4 couldn't reach. Full spec in
[`docs/design/symbolic-exponents.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/symbolic-exponents.md).

- **New `Exponent` type** (`core/units.py`): linear combination
  `q₁·x₁ + q₂·x₂ + … + c` with named opaque generators. Each
  dimension slot of `Unit` now carries an `Exponent` instead of a
  bare `int | Fraction`. `__post_init__` auto-promotes legacy
  `Number` slots; `Exponent.__eq__(Number)` keeps existing tests
  comparing slot vs. literal valid.
- **`**` resolver fallback.** When the literal-rational path fails,
  `_resolve_symbolic_exponent` maps the exponent identifier (or a
  linear arithmetic of identifiers) to an `Exponent`, then dispatches
  to `Unit.pow(Exponent)`. `Exponent × Exponent` is defined only when
  one side is pure-constant — otherwise the resolver falls back to
  D1.4 (kept linear by design).
- **Closed 3 Exner D1.4s** on LMDZ:
  `guide_mod.f90:772`, `exner_milieu_m.f90:105`,
  `exner_milieu_loc_m.f90:126`.
- **Rendering.** `format_unit` understands symbolic slots and prints
  `Pa^(2/7·kappa)` rather than the previous fallback.

### Symbolic LogWrap multipliers

Same machinery applied to `combine`'s R5.4 path (LogWrap × scalar).
The log-power identity `γ·LOG(p) = LOG(p^γ)` now accepts an
`Exponent` multiplier so dimensionless-but-symbolic scale factors no
longer fire D1.4. Spec:
[`docs/design/symbolic-logwrap.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/symbolic-logwrap.md).

- **R5.4 accepts Exponent multipliers.** `(2/7) * LOG(p)` →
  `LOG(p^(2/7))` (already worked); `xalpw * LOG(p)` with `xalpw`
  a symbolic linear form → `LOG(p^xalpw)`. Resolver fallback wired
  at `_resolve` and `_walk_expressions` for the `*_literal` slots.
- **Symbolic divisor on LogWrap is refused as D1.4.** `LOG(p) / κ`
  (i.e. `1/κ * LOG(p)`) is not a linear form in `κ`; the algebra
  honestly punts instead of guessing.
- **H010 demotion narrowed.** The R4.1 implicit-cast demotion now
  requires an actual `Number`, not a symbolic `Exponent`; previously
  a dimensionless variable reference could mis-trigger.
- **Closed 3 of 4 Tetens D1.4s.** `modd_csts.F90:263`, `:266`,
  `qsat_seawater_mod.F90:102`. The remaining `qsat_seawater2_mod.F90:85`
  is a #006 K-literal case (not algebra). Surfaced finding **#012**
  (`XALPW` / `XALPI` / `ZFOES` annotated dimensionless but the algebra
  computes `LOG(Pa × K^γ)` — annotation gap, not a tool bug).

### Other LSP / CLI changes

- **`Extract literal to a named PARAMETER` code action** on every
  H010 D1.5 diagnostic. The VSCode companion prompts via
  `showInputBox` for the parameter name, then inserts a typed
  declaration at the end of the enclosing routine's decl block
  and replaces the literal at the use site.
- **Hover on a Fortran intrinsic callee** (`exp`, `log`, `sqrt`,
  `sin`, `sum`, …) now shows the call's resolved unit and the
  full source text of the call rather than `name(...)`.
- **`H001` squiggles span the whole assignment**, not just the
  LHS identifier — easier to see at a glance.
- **All hover popups carry the `🟢 / 🟡 DimFort` header** matching
  the trace-mode style.
- **`mbar`, `hPa`, `bar` added to the default unit table** via a
  new `factor` field on derived-unit specs.

### Spec & test coverage

- **All 55 unit-algebra YAML fixture cases now run** (was 48 / 7
  skipped). The fixture runner gained slot-order translation for
  `Regular(...)` tuples (the spec uses spec slot order, the impl
  uses impl slot order) and multi-statement support (`;`-separated
  statements share the synthetic subroutine scope, the last
  statement's RHS is what's compared).
- **OQ5 resolved**: missing-annotation propagation. `x + y` with
  `x` unannotated produces `unknown` (None); DimFort never silently
  infers the missing annotation from a sibling operand. Same path
  applies inside wrapper intrinsics — `LOG(unannotated)` is
  unknown, U005 fires on the declaration. Recorded in
  [docs/unit-algebra.md §11](https://github.com/ArrialVictor/DimFort/blob/main/docs/unit-algebra.md#oq5--resolved-missing-annotation-propagation).
- **Outer-unary-minus sign propagation in `_resolve`**: `-1.0 *
  LOG(p)` (parsed by tree-sitter as `-(1.0 * LOG(p))`) now sees
  R5.4 with `k = -1` and types as `LOG(1/Pa)` rather than the
  pre-fix `LOG(Pa)`.

### Tooling

- **Per-push CI**: ruff + pytest on `ubuntu-latest` / Python 3.12
  for every push to `main` and every PR. Full 3 × 3 OS × Python
  matrix still runs on tag push from `release.yml`.

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

- **Per-scope `@unit{}` annotations** (`attach.py`, `annotations.py`, `ts_checker.py`): annotations are now keyed by `(scope_lc, name)` where `scope_lc` is the lower-cased enclosing subroutine/function (or `None` at module/file level). Two routines in one file declaring same-named params with different units no longer alias. Flat `var_units` view retained as a back-compat first-seen surface for callers that don't carry scope info. `_make_scoped_lookup` no longer falls back to flat lookup when in scope-aware mode — this closed a real false-positive path where unannotated wrapper params (e.g. NetCDF `put_var(..., v)`) were absorbing the unit of unrelated same-named variables in the workset. Diagnostic count on the reference workspace trial dropped from 20 to 12 H-findings, all real (8 spurious, retracted from the trial's findings log).
- **Scope-aware hover** (`lsp/server.py`): bare-identifier hover consults the per-scope table and reports the *enclosing routine's* annotation, not the first-seen across the workset.
- **Module-name hover and goto-def** (`lsp/server.py`, `lsp/ts_helpers.py`): hover on the module-name token of a `use foo` statement renders a summary of the module's exports — variables with units, contained procedures with signatures, `(N/M annotated)` count when there's a gap. Goto-def on the same token jumps to the `module foo` header.
- **Workspace external-procedure index** (`core/workspace_index.py`, `core/multifile.py`, `lsp/server.py`): a workspace-wide name map from top-level `SUBROUTINE`/`FUNCTION` to defining file, populated at LSP startup (~4.5 s on a ~2,400-file reference workspace). Resolves F77-vintage external procedures (called without a `USE` clause), so goto-def, hover signatures, and H004 all follow such calls. `resolve_workset`'s BFS now expands via `CALL` edges too; topo sort honours them; the per-file workset cap pins direct deps (modules used + procedures called) so shallow callees can't be sliced out.
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
- **Phase 3 hardening (2026-05-15)** — exercising the trial workspace on a 117-file subdirectory surfaced two bugs in the Phase 2/3 multifile orchestrator: missing U-series emissions (U001 scan errors, U002 unit-parse failures, the U006/U-conflict/U010 set from `_attachment_diags`) and a cross-file bare-name leak through `merged_var_units`. Fixed by reusing `multifile._attachment_diags`, emitting U001/U002 in the per-file pass, and scoping each file's check from its own `attachment.var_units` (cross-file imports still arrive explicitly via `apply_use_clauses`). Impact on that subdirectory: false-positive H001s dropped from 47 to 6; previously-suppressed H004s now surface (11). New regression fixture `tests/fixtures/multifile_scope/` + `test_ast_scope.py`.
- **Phase 4 (backend selection, 2026-05-15)** — `[checker] backend = "ast" \| "asr"` lands in `dimfort.config.DimfortConfig`. CLI gains `--backend ast\|asr` on the `check` subcommand. LSP server reads `backend` from `initializationOptions` (falling through to config, then default `"asr"`). VSCompanion repo's `ast-only` branch adds `dimfort.backend` (enum) to the settings schema and forwards it. Backend is logged in the init notification (`backend=…`). 5 new config tests + 3 new CLI integration tests. Default stays `"asr"`; Phase 5 will flip it once the AST path has soaked.
- **Phase 4.6 (`.intfb.h` stubs + cpp_defines, 2026-05-16)** — `[lfortran] include_paths` and `[lfortran] cpp_defines` in `DimfortConfig` thread `-I` and `-D` through to LFortran. Unblocks third-party headers (after stubbing them empty) and `#ifdef`-branched modules. `lf.dump_tree` decodes stdout/stderr with UTF-8 → Latin-1 fallback so non-ASCII-comment files don't crash the workspace check. Adds the "DimFort: Check Whole Workspace" LSP command with phase-tagged ($/progress) per-file reporting ("loading 412/2435", "indexing modules", "checking"). Reference workspace trial: 2435 files → 16 unloadable + ~13 cascade U007s (all LFortran 0.63 bugs).
- **Phase 5 (default backend → AST, 2026-05-16)** — `cli.py`, `lsp/server.py`, and VSCompanion `package.json` all now default to `backend = "ast"`. ASR remains selectable via `--backend asr` (CLI), `[checker] backend = "asr"` (config), or the `dimfort.backend` VSCode setting. Fixes a long-standing round-trip bug in `ast_multifile`: it converted parsed `Unit` objects back to text via `format_unit()` before handing to `ast_checker.check`, which then re-parsed — but `format_unit` emits Unicode (`m/s²`, `kg×m/s²`) that the parser doesn't accept. `ast_checker.check` now accepts `Unit` objects directly for both `var_units` and `field_units`; the multifile path passes them through without round-tripping. Caught when the existing CLI integration tests (which previously ran via ASR by default) started failing — they exercise H001 on a single-file workset where this round-trip had been silently dropping the only annotation.
- **Phase 6a (parallel loading, 2026-05-16)** — `check_files_ast`'s Phase A now uses a `ThreadPoolExecutor` (default workers = `cpu_count() - 1`). Subprocess.run releases the GIL while LFortran is running, so threads parallelise without the pickling overhead a process pool would impose. Progress callback fires in completion order under a small lock. Reference workspace benchmark (2435 files, 8 cores): 223s → 170s (1.3×). Modest gain — GIL contention during large-AST JSON parsing now dominates the residual.
- **Phase 6b (AST cache, 2026-05-16)** — New `cache.load_single_tree_cached(path, mode='ast', …)` mirrors `load_trees_cached` but caches one tree at a time. Stored under `<cache>/<sha1>.ast.json`, keyed on content sha256 mixed with `include_paths` + `cpp_defines` (so config changes invalidate cleanly). `ast_multifile.check_files_ast` now accepts a `cache_dir=` kwarg and threads it into `_load_one`; the LSP passes `_cache_dir` (already resolved at initialize). LSP buffer overrides bypass the cache for that file only — sibling files still benefit. 3 new unit tests covering round-trip, include-path invalidation, and cpp-define invalidation. Warm-run workspace check now dominates JSON-load cost rather than LFortran, dropping wall time to a fraction of the cold run.
