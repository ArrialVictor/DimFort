# Changelog

All notable changes to DimFort are documented here. Format inspired by [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Fixed

- **Coverage projection: expand red and yellow tier code sets**.
  Surfaced during the poly_qa.f90 smoke walk: H020 / H021 / H022 /
  H023 (polymorphism unification failures) fire at ERROR severity
  but were missing from `_RED_CODES`, so lines firing those codes
  painted green instead of red. Also added S003 (invalid affine
  conversion) and U002 (unparseable annotation) to the red tier,
  and S001 / S002 (scale / offset mismatch) to the yellow tier.
  All severity-ERROR consistency-family codes now paint red and
  all severity-WARNING quality / scale codes paint yellow.
- **Coverage projection: paint every annotated declaration regardless
  of scope**. Surfaced during the same smoke walk: a polymorphic
  variable name (`x`, `mean`, `half`) declared in multiple routines
  of a module would show uncoloured at every declaration except the
  first. Cause: the projection read
  `attachments.var_units_span`, which is keyed first-seen-wins on
  the variable NAME, so same-name declarations across scopes
  weren't recorded. Fix: walk tree-sitter `comment` nodes for an
  ``@unit`` substring and paint every line carrying an annotation
  comment green. Robust against name collisions across scopes.
  Design spec §10.2 updated accordingly.

### Changed

- **Coverage projection: propagate `U005` to use sites**. The previous
  implementation followed the literal "diagnostic owns the line" rule
  and painted the use site of an unannotated variable green when no
  diagnostic happened to own that specific line. The consequence,
  observed during the VSCompanion smoke walk: removing an annotation
  could make a previously-red use site look green, because the H001
  that had been firing on the mismatched expression now had no unit
  to evaluate. The projection now lifts the unannotated names out of
  the file's `U005` diagnostic messages and paints every use site of
  those names yellow. The transition on annotation removal is now
  red → yellow rather than red → green. Worst-wins still applies;
  red / blue diagnostics still win. New helper
  `_unannotated_names_for_file` in `core/coverage.py`; design spec
  §3.3 documents the rule.

### Added

- **Coverage visualisation (server-side)**: new `dimfort/lineStatus`
  and `dimfort/coverageStats` LSP methods, plus a new `dimfort
  coverage` CLI subcommand. The four-tier projection (green / yellow
  / red / blue + no-decoration) is computed from the cached workset
  check result without re-running the checker. New module at
  `core/coverage.py` carries the pure projection logic; the LSP
  wrapper in `lsp/coverage.py` serialises tree traversal under
  `state.ts_handler_lock`. CLI flags: `--summary`, `--by-module`,
  `--json`, `--no-color`. Companion-side rendering work follows in
  separate PRs. See `docs/design/future/coverage-visualization.md`
  for the design spec.

### Fixed

- **Stale doc-path references in `lsp/` comments**: four `#` comments
  and docstrings in `lsp/server.py` and `lsp/panel.py` pointed at
  `docs/design/panel-info.md`; the actual file lives at
  `docs/design/shipped/panel-info.md`. A fifth reference introduced
  in the `lsp/` docstring sweep mis-targeted `docs/design/side-panel.md`
  (no such file — the user-facing description is at
  `docs/editor-integration/side-panel.md`; the implementer-facing
  wire spec is the `shipped/panel-info.md` path); both reference
  styles now point at the correct location.
- **Stale "non-VSCode clients see this as a no-op" framing in
  `lsp/code_action.py`**: two `#` comments described the
  `dimfort.insertSnippet` and `dimfort.extractToParameter` code
  actions as VSCode-only, with non-VSCode clients seeing them as
  no-ops. All three shipped editor companions (VSCode, Nvim, Emacs)
  register both commands; the comments now say so.
- **Over-claiming VSCode-specific framing in `lsp/hover_render.py`**:
  the module-hover cap comment treated truncation as a cosmetic
  safety belt because "VSCode's hover popup is scrollable." Some
  clients (e.g. Neovim's default floating preview) do not scroll, so
  the cap is load-bearing for both cases; updated.

### Added

- **Docstring style enforcement**: ruff now selects the `D` rule set
  with `[tool.ruff.lint.pydocstyle] convention = "google"`. Missing
  or malformed docstrings in `src/dimfort/` fire under `ruff check .`,
  keeping the post-sweep Google style from drifting. Tests under
  `tests/` and dev utilities under `scripts/` are exempt; the empty
  `lsp/__init__.py`, the side-effect `core/__init__.py` shim, and the
  thin `__main__.py` entry stub carry narrow per-file exemptions.
  `CONTRIBUTING.md` tightened to mention the convention. Two
  previously-missed public functions in `cli.py` (`build_parser`,
  `main`) gained their docstrings; three `D301` fires on docstrings
  containing literal backslashes gained their `r"""` prefix; one
  `D205` fire on the module docstring of `core/unit_patterns.py` was
  reflowed to a single-line summary.

### Changed

- **Top-level docstring sweep**: module + class + public-function
  docstrings under `src/dimfort/` (top level only: `__init__.py`,
  `cli.py`, `config.py`) rewritten in Google style (Args / Returns /
  Attributes / Note sections), with each docstring verified against
  current behaviour. No code changes; behaviour unchanged. First of
  three sweep PRs (top-level → `core/` → `lsp/`); convention is
  enforced via ruff in a follow-up.
- **`core/` docstring sweep**: same treatment applied across 16 of
  the 19 modules under `src/dimfort/core/` (the 17th, `__init__.py`,
  carries only a side-effect import comment and was unchanged).
  ~136 existing docstrings reshaped to Google style and ~98 added
  to previously-undocumented callables (including private helpers).
  Class-level `Attributes:` blocks consolidate per-field rationale
  where appropriate; rich inline per-field comments are preserved
  when richer than an `Attributes:` block could carry. Every claim
  was verified against current behaviour; no stale docstring was
  found. No code changes; behaviour unchanged.
- **`core/ts_checker.py` heavier follow-up pass**: the prior pass
  left the 3.7k-line checker's existing one-line docstrings on
  AST-dispatch helpers in their original prose form. This follow-up
  reshapes ~65 of them into multi-section Google form (explicit
  `Args:` / `Returns:` / `Raises:` / `Note:`), bringing the file in
  line with the rest of `core/`. No new docstrings (those were added
  in the prior pass). No code changes; behaviour unchanged.
- **`lsp/` docstring sweep**: same treatment applied across all 17
  populated modules under `src/dimfort/lsp/` (`__init__.py` is empty
  and was unchanged). ~98 existing docstrings reshaped to full Google
  form and ~35 added to previously-undocumented callables (including
  private helpers and nested closures). LSP handler docstrings now
  call out the LSP method they implement (e.g. "Implements
  ``textDocument/hover``."). Two small staleness corrections: a
  VSCode-specific reference in `hover.py` generalised to "the editor"
  (LSP serves all four companions); a `docs/design/panel-info.md`
  pointer in `server.py` updated to the canonical
  `docs/design/side-panel.md`. No code changes; behaviour unchanged.

## [0.2.3.1] — 2026-06-07

Patch release fixing the in-editor surface of the 0.2.3 polymorphism
feature. The 0.2.3 server emitted correct H020 / H023 diagnostics but
the hover and side-panel tree rendered them with the pre-polymorphism
`(expected …)` trailer at warn-level — a confidence bug where the
Problems-panel said 🔴 and the inline UX said 🟡. Eight server- and
companion-side fixes bring the in-editor surface in line with the spec
(`docs/design/shipped/polymorphic-units.md` §H020).

Multi-repo release: DimFort + VSCode / Neovim / Emacs companions all
bump to 0.2.3.1. The wire format gains one optional `collides` field
on `ExpressionNode` and one optional `polymorphism_conflict` field on
`Diagnostic` (server-internal, not LSP-exposed). Old companions stay
forward-compatible (omitted field reads as null/None).

### Fixed

- **Panel/hover marker for H020 / H021 / H022 / H023**: the polymorphism
  diagnostic codes were missing from the consistency-family marker
  driver list, so the panel tree fell back to the resolution axis (🟡)
  even though the diagnostic correctly fired 🔴. They now paint 🔴 on
  the offending node, and worst-of-children lifts it to the call /
  assignment root — matching the H001 / H002 UX.
- **H020 hover and panel rendering** now mirrors the spec form:
  `arg N (name): 'a = unit` in the unit column, `(collides with arg N)`
  trailer to the right of the marker (parallel to `(expected …)` and
  `(assumed: …)`), marker hard-pinned 🔴 on every contributing row.
  Previously rendered as `unit 🟡 (expected 'a)` — the wrong wording
  (spec mandates `collides`, not `expected`) at the wrong severity.
- **H020 diagnostic message reformatted** from a single 200-char run-on
  sentence to multi-line: tightened lead phrase (`type variable 'a
  cannot unify across these args of '<fn>':`), one row per contributing
  slot, em-dash separator before the partner list, bare `arg N` partner
  labels (no `(name)` parenthetical — the partner's own row carries the
  name).
- **Clean polymorphic call** no longer paints arg rows 🟡 with
  `(expected 'a)`: the dimensional comparison driving `(expected …)` is
  irrelevant when the formal is a tyvar (the unifier handles binding).
  Clean rows now render `unit 🟢` with no trailer.
- **Polymorphic function return** now applies the call-site unifier's
  substitution to `sig.return_unit`, so a clean `r:m = f(m, m)` against
  `f(x: 'a, y: 'a) → 'a` resolves the RHS to `m` (the bound return),
  not the formal `'a`. Pre-fix this fired a spurious H001 on every
  polymorphic-function assignment. On unification failure (H020), the
  call resolves to `None` to prevent H001 from double-firing on top of
  H020's existing report.
- **H020 call-row rendering**: when a polymorphic call's unifier
  rejected, the call_expression itself now renders `'a = ?` in its
  unit column (binding indeterminate) rather than a bare `?`
  ("unknown for some unspecified reason"). Mirrors the arg rows'
  `'a = unit` form. Companion-side: the trailing `?` is dimmed so it
  reads at the same visual weight as a bare absence-glyph.

### Changed

- **CHECKER_OUTPUT_VERSION bumped 7 → 9** (one bump per cached-diagnostic
  shape change in this release). Upgrading from 0.2.3 → 0.2.3.1 auto-
  invalidates pre-fix entries on first re-check; no user intervention
  required. The previous "Clear Content-Hash Cache" workaround is no
  longer necessary.

### Wire format

- **`Diagnostic.polymorphism_conflict`** (server-internal, not LSP-
  exposed): structured per-arg conflict data on H020 — each row carries
  `(slot_index, slot_name, binding_text, partner_slot_indices)`. The
  LSP panel-render path reads this to draw the spec form on each
  conflicting arg row.
- **`ExpressionNode.collides`** (LSP wire): optional string field
  carrying the H020 partner-arg list (`"arg 2"` /
  `"arg 1, arg 3"`). Companion renders it as `(collides with …)`.
  Absent on every non-H020 row; absent on pre-0.2.3.1 servers.

## [0.2.3] — 2026-06-06

Largest 0.2.x shipment to date — adds parametric polymorphism (a whole
new feature) and lands the pre-release audit fix cycle while remaining
in beta. The wire format, diagnostic codes, and LSP protocol still
aren't frozen; another `0.2.y` will land before the `0.x → 0.y+1`
graduation jump.

### Added

- **Polymorphic units** (`'a`, `'b`, …) in `@unit{}` annotations.
  OCaml-style type variables let a function declare a signature that
  works for any unit — `subroutine avg(x: 'a, y: 'a, out: 'a)` accepts
  two kg and returns a kg, or two m and returns a m. DimFort
  Hindley-Milner-unifies the tyvars per call site (Kennedy 1996
  AG-unification over the multiplicative unit algebra) and enforces
  consistency at both function definition and every call site.
  Composes cleanly with the existing symbolic-exponent and LogWrap/
  ExpWrap machinery. See [docs/reference/polymorphism.md](docs/reference/polymorphism.md)
  for the how-to and
  [docs/design/shipped/polymorphic-units.md](docs/design/shipped/polymorphic-units.md)
  for the design rationale. Four new diagnostic codes:
  - **H020** — polymorphic call-site unification failure (symmetric
    "collides with arg N" trailer).
  - **H021** — type variable in a forbidden declaration position
    (module-level, PARAMETER, derived-type component, SAVE'd local,
    COMMON block member).
  - **H022** — cannot bind a tyvar to an affine unit (e.g. `degC`).
  - **H023** — polymorphic body forces a binding on a quantified
    tyvar; the signature is dishonest. Strict (no warning form).
- LSP signature hover prefixes polymorphic functions with `∀` for each
  declared tyvar (sorted): `∀ 'a. avg(? : 'a, ? : 'a) : 'a`.

### Fixed

A pre-release audit (whole-codebase, multi-agent, adversarially
verified) surfaced and landed 25 must-fix / worth-fix items plus a
curated subset of the green-band findings. Highlights:

- `cache_serde` now round-trips `Unit.offset`, all four
  `ModuleExports` visibility / `inner_uses` fields, and
  `Diagnostic.suggested_rewrite`. Each was previously dropped at
  serialization time — affine units were silently turned into their
  base, the U002 "did you mean…?" suggestion was lost on warm runs,
  and the visibility fields were waiting to silently mis-cache the
  moment visibility-aware checks land.
- `info` severity overrides are now honoured end-to-end. The literal
  `U021 = "info"` example shipped in `dimfort-toml.md` was silently
  rejected by the config parser and the override pipeline only had
  `error` / `warning` arms — the documented example was unreachable.
- LSP concurrency: `workspace_index_lock` now held across
  `update_index`; cached-tree fallback dropped from `panel` /
  `interactions` (matched the documented "permanent concurrency
  gotcha" — could crash tree-sitter natively).
- LHS subscripts now walked for nested-expression diagnostics
  (`arr(int(i+j), 1) = 1.0` with mixed-unit indices was silent
  before). Keyword arguments (`call f(b=x)`) now bind to the named
  formal slot — H003 / H004 / H020 / H022 no longer silently miss
  keyword-only call sites.
- `attach` threads `RawAnnotation.end_column` through to
  `var_units_span` so configurable comment delimiters (`! [m/s]`
  etc.) get correctly-positioned U002 squiggles + LSP hover ranges.
- Algebra: `abs()` reclassified from TRANSFORMING to TRANSPARENT
  (preserves `LogWrap` through `abs(log(p))`); `equal_strict`
  includes offset (matching `Unit.__eq__`); `Exponent.__hash__`
  matches the bare-Number hash when pure-constant (honours
  `a == b ⇒ hash(a) == hash(b)`); `power()` catches `UnitError`
  specifically and returns D1.4 rather than silently `(None, None)`.
- CLI: malformed `.dimfort.toml` now exits 2 per the documented
  contract (LSP keeps the soft-degrade path); bare `dimfort` exits 2
  with help to stderr; `--clear-cache` confirms in stderr;
  `unit_config` rejects floats for `factor` / `offset` (forces the
  string form so `Fraction(0.1)` doesn't poison every downstream
  diagnostic).
- Workspace: non-OSError exceptions during `_load_one` no longer
  abort the whole workset (UnicodeDecodeError on a stray binary
  file etc. is recorded per-file and the rest proceeds);
  `_iter_fortran_files` sorts `rglob` output so first-wins
  procedure / module winners are deterministic across OSes.
- LSP rendering: `panel.fileDiagnosticCounts` tallies info + hint;
  bare-identifier + numeric-literal hovers consult `_node_marker`
  (no more 🟢-vs-🔴 cross-surface mismatch); completion trigger
  guarded against string literals and requires an active comment.
- `suggest_rewrite` widens to `except Exception` so a
  `ZeroDivisionError` from a candidate like `m^(2/0)` no longer
  escapes into the U002 emission site.

See [release-prep PR
#43](https://github.com/ArrialVictor/DimFort/pull/43) for the
complete punch-list.

### Cache

- `CHECKER_OUTPUT_VERSION` 4 → 5: tyvar field in `Unit` payloads.
- `CHECKER_OUTPUT_VERSION` 5 → 6: `Unit.offset` now round-trips
  (see *Fixed*).
- `CHECKER_OUTPUT_VERSION` 6 → 7: `ModuleExports.inner_uses` /
  visibility fields + `Diagnostic.suggested_rewrite` now round-trip.
  Refreshes any v6 entry holding a previously-truncated payload.

## [0.2.2.1] — 2026-06-04

Documentation patch. No behaviour changes — the running checker,
LSP server, and `default_units.toml`-parsed values are identical
to 0.2.2.

### Documentation overhaul

The `docs/` tree was regrouped by audience into `quickstart/`,
`reference/`, `editor-integration/`, and a three-way split of
`design/` into `shipped/` / `future/` / `contributor/`. The flat
top level kept only the user guide (`usage.md`), the landing page
(`index.md`), troubleshooting, and the maintainer-only
`release-process.md`.

New pages: `quickstart/install.md`,
`quickstart/first-check.md`,
`quickstart/bringing-to-existing-codebase.md` (extracted from the
previous `usage.md`), `reference/cli.md`,
`reference/diagnostic-codes.md` (single source of truth for the
H / U / S / X / P code catalog),
`reference/dimfort-toml.md` (every config key),
`reference/intrinsics.md`,
`reference/units-file.md` (full schema for the `[units] file`
extension), `troubleshooting.md`,
`editor-integration/side-panel.md` (canonical description of the
six-section side panel rendered by all three companions).

Reshaped pages: `index.md` is now a real landing page;
`usage.md` is trimmed to the feature surface and content-hash
cache; `editor-integration/lsp-protocol.md` (was `lsp.md`) is
rewritten against the running server — the previous version
documented the pre-redesign hover and the legacy
`traceHoverEnabled` key.

The README gains an "Adopting on an existing codebase" section
between the status banner and the quick tour, surfacing the
0.2.2 configurable-delimiter feature early as the adoption
story.

### Source touch

`src/dimfort/core/default_units.toml`: large integer literals
switched to TOML's underscored form
(`G = 1_000_000_000`, `M = 1_000_000`, `k = 1_000`,
`bar.factor = 100_000`). Same parsed values; cosmetic
readability change. Verified by the full test suite.

## [0.2.2] — 2026-06-03

### Add: configurable comment delimiters for the three unit directive families

A project can now opt into bracket-style (or any other literal-string)
comment delimiters for unit annotations, alongside the canonical
`@unit{...}`, `@unit_assume{...:...}`, and
`@unit_affine_conversion{...->...}` forms. The driving use case is
**bringing DimFort to an existing codebase** whose authors already
used `! [m/s]`-style inline labels — without 0.2.2 those labels
would have been invisible to the checker, and the cost to migrate
each one to `@unit{m/s}` was a real adoption blocker.

Three new `[parser]` keys in `.dimfort.toml`, each independent of
the others (a project that opts into bracket-shaped `@unit{}` does
*not* automatically opt into bracket-shaped assumes / affines —
those have higher safety stakes, so the choice is per-directive):

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
# unit_assume_comment_delimiters and
# unit_affine_comment_delimiters have the same shape, plus a `sep`
# field that splits the inner text (`:` for assume → unit + reason;
# `->` for affine → src + tgt). Defaults keep the canonical forms.
```

Each list **replaces** its default; to keep canonical alongside a
custom form, list both. Setting a list to `[]` is an error
(the loader logs and falls back to the default) — empty would
silently disable that directive family, almost certainly a typo.
The full design lives
at `docs/design/unit-comment-delimiters.md`.

### Add: plain `!` comments are now eligible at statement-bearing positions

The pre-0.2.2 scanner required a Doxygen marker (`!<`, `!>`, `!!`)
for an annotation to be recognised. As of 0.2.2 a bare `!` comment
is also scanned, provided it sits at an eligible position:

- Trailing on a declaration line (for `@unit{}`) or an assignment
  line (for `@unit_assume{}` / `@unit_affine_conversion{}`).
- Standalone immediately above such a line, with strict
  immediacy: no blank line, no other statement, no second comment
  line between the bare comment and its target.

This is a deliberate, documented expansion — the diagnostic
emission set on the validation workspace was bit-for-bit unchanged
by the §16 baseline regression check, confirming no production
annotation drifted from "ignored prose" into "newly-checked
claim".

### Add: U021 / U023 diagnostics and U002 suggested-rewrite payload

Three new diagnostic codes surface configuration-time mistakes
that 0.2.2's flexibility makes possible:

- **U021 — conflicting unit comment patterns** (WARNING). Two
  configured patterns matched the same comment with disagreeing
  capture text. The first-listed wins (deterministic from
  `unit_comment_delimiters` order) and the diagnostic asks the
  user to remove one of the forms.
- **U023 — directive on wrong statement kind** (WARNING). The
  scanner saw a directive on a comment whose target statement
  doesn't match the directive family — `@unit_assume` on a
  `real :: x` declaration, `@unit{}` on a regular assignment, and
  similar. The directive is *dropped* (not silently applied) and
  the message suggests the directive that would attach correctly.
- **U002 — could not parse unit text** (existing). The diagnostic
  payload is extended with an optional `suggested_rewrite`. A
  one-step rewrite pipeline runs on the failed capture; if the
  transformed string parses cleanly against the project's unit
  table, the diagnostic message gets a trailing "did you mean
  `<X>`?" and (in the LSP) a Quick Fix code action that replaces
  just the inner unit text inside the directive token. The sole
  rewrite rule shipped in 0.2.2 is digit-suffix → caret exponent
  (`m2 → m^2`, `kg/m3 → kg/m^3`); the rule design is documented
  in `docs/design/unit-comment-delimiters.md` §12.5 so future
  additions stay disciplined.

When U001 ("more than one … on one line") fires, **no annotation
attaches** — the variable surfaces as unannotated rather than
silently picking the first match, and every capture site is
squiggled so the user sees the full extent of the ambiguity.
When U002 fires the panel hides the raw unparseable text (the
'error' kind still drives the unparseable badge) so the displayed
unit no longer implies DimFort accepted text that it could not
parse.

### Fix: cache invalidation on pattern config change

`PER_FILE_CONFIG_KEYS` now includes the three new pattern lists,
so toggling a pattern in `.dimfort.toml` correctly invalidates
each file's cache entry. Pre-fix, the cache would replay stale
assume- or affine-derived diagnostics for files whose source
bytes hadn't changed. The output-version is bumped to v4 so any
v3 entries written before this fix are orphaned automatically
(clean rebuild rather than serving stale schema).

### Backward compatibility

A project that does not set any of the new `[parser]` keys gets
exactly the pre-0.2.2 diagnostic emission set, with one
documented expansion: a bare `!` comment containing the default
`@unit{...}` (etc.) form is now eligible at its declaration's
position. The validation workspace ran the §16 pre/post baseline
regression check cleanly — 116 H + 52094 U diagnostics
unchanged, `var_units_by_scope` semantically identical
(sorted-diff of the 22 MB workset.json is empty).

### Spec & adoption

- Full spec: `docs/design/unit-comment-delimiters.md` — 17
  sections covering the unified model, eligibility, multi-var
  treatment (unified across pattern types post-Q1), conflict /
  wrong-kind handling, U002 rewrites, and the §16 pre-merge
  backward-compat protocol. **Read this first** before opening
  the related code paths.
- Migration guide: see §15 of the spec ("Migration and adoption
  guidance"). On a fresh codebase, expect a burst of new
  diagnostics on the first run after enabling bracket patterns —
  many of them real bugs that have been hiding behind doc-only
  annotations.

## [0.2.1] — 2026-05-30

### Add: `demos/` directory with a canonical, user-facing tour file

A new top-level `demos/` directory ships the first user-facing entry
point into DimFort: a short, self-contained Fortran source file
(`demos/tour.f90`, ~55 lines) plus a line-by-line walkthrough
(`demos/README.md`).

The demo is a textbook moist-thermodynamics routine — `T`, `p`, `rho`,
`v`, `R_d` — that exercises six high-impact behaviours on a single
page: pure-literal initialisation autocast (**R4.4**, silent), an
ideal-gas line that balances cleanly, a scale mismatch between `Pa`
and `hPa` (**S001**), a textbook homogeneity error (**H001**), a
missing-annotation case (**U005**), the non-derivable power-law
escape hatch (**D1.4** → **U020**), and a numerically-stable
log-space pressure ratio `exp(log(p) - log(p_ref))` that exercises
the `LOG(…)` / `EXP(…)` wrapper algebra end to end — `log` promotes
`Pa → LOG(Pa)`, the subtraction collapses to `LOG(1) → 1`, `exp`
strips back to dimensionless, all silent and with no annotation
beyond the LHS unit (a rewrite few static checkers cover). A small
internal function (`dyn_p`) with annotated
formals + a deliberately-mismatched call site exercises **H004**
(cross-procedure unit checking on call boundaries), and the
expected-output section shows what `--trace` adds to a diagnostic
(the firing rule chain, here `R4.2`).

`dimfort check --scale demos/tour.f90` produces the exact four-line
output captured in `demos/README.md` (one error, exit `1`), and the
walkthrough explains both the diagnostics that fire *and* the lines
where DimFort is deliberately silent (R4.4 autocast, balanced
homogeneity, LOG/EXP wrapper rewrites). README screenshots will be
taken from this file going forward, so they stay reproducible by
anyone with the repo checked out.

Two companion files ship alongside the main tour:

- **`demos/affine.f90`** — scale-family focus: **S001** (factor
  mismatch), **S002** (un-blessed offset mismatch), the verified
  `@unit_affine_conversion{degC -> K}` directive applied to a small
  `c_to_k` function (silent because verified, *not* trusted like
  `@unit_assume`), and **S003** for the case where the same
  directive is attached to arithmetic that doesn't actually perform
  the stated conversion.
- **`demos/broken.f90`** — a one-block-per-code lookup table for
  **H001 / H002 / H003 / H004 / H010 / U005**, with no prose. Each
  block is a single statement that fires exactly the one code its
  comment promises; use it as a quick "what does H002 look like?"
  reference.

The three companion repos (VSCode / Neovim / Emacs) link to the demo
rather than duplicating the fixture.

### Add: transitive `use`-clause resolution in the Imports panel section

`use` clauses are now followed transitively when building the panel's
**Imports** section. A symbol re-exported through a chain of modules —
e.g. `solver use phys_constants`, which in turn `use phys_base` —
now surfaces in the consumer's import list, attributed to the module
that *originally declared* it (so click-to-navigate jumps to the real
declaration, not the intermediate hop).

Rules honoured (Fortran 2008 §11.2):

- **Default visibility is PUBLIC.** A module without a bare `private`
  re-exports every name it imports.
- **`use foo, only: …`** along the chain narrows what passes through.
- **`use foo, local => remote`** renames carry through to consumers.
- **`private` / `public ::`** at module scope gate re-export per name.
- **Cycles** between modules terminate (in-progress set short-circuits
  the back-edge).

The closure is memoised once per workspace pass — per-cursor calls stay
O(direct uses). Imports rows now carry an optional `viaModule` field
naming the intermediate hop (when origin ≠ direct use). Checker
semantics are unchanged — only the panel surfaces transitive symbols.

### Change: 🔵 overlay + `(assumed: <reason>)` on the RHS row of `@unit_assume` assignments

`@unit_assume{<unit> : <reason>}` lines now carry a positive visual
signal in both the panel's Expression tree and the hover.
Previously the U020 INFO acknowledgment surfaced only in the
diagnostic list; the tree gave no indication that a row was
accepted via the escape hatch.

The overlay lives on the **RHS row** — the directive's syntactic
subject — not on the assignment itself:

- The RHS row carries the **asserted** unit (e.g. `kg·m⁻³`), not
  the computed `?`, so the reader sees what unit DimFort is using
  for the LHS homogeneity check.
- The RHS row paints **🔵** — a per-row overlay, **NOT a severity
  tier**. It doesn't participate in worst-of aggregation, doesn't
  propagate to ancestors, and doesn't compete with 🟡/🔴 elsewhere.
  The severity model stays a clean three-tier `error > warn > ok`.
- The RHS row's tail reads `(assumed: <reason>)` — same column as
  `(expected …)`; both can coexist (a declared-unit conflict
  shows both).
- The **assignment row stays 🟢** when the homogeneity check
  passes (LHS unit matches the asserted RHS unit). The hover
  header is the root row's marker, so a clean assumed line reads
  with a 🟢 header and 🔵 in the body — the assertion is visible
  where it lives.
- **A declared-unit conflict still fires H001**, painting the
  assignment row 🔴 (and the header). The RHS row then carries
  🔵 + `(expected <lhs_unit>) (assumed: <reason>)`. The assumption
  never masks a declared-unit conflict.
- **Ownership rule**: line-based, restricted to
  `assignment_statement` nodes (the directive is statement-level).
  U020's source position lives at the `@unit_assume` token in the
  trailing comment — outside the assignment's tree-sitter span —
  so span-based ownership wouldn't match.

Wire-format:
- `ExpressionNode.marker` adds the value `"assumed"` (companions
  render 🔵). Other markers stay `"ok"`/`"warn"`/`"error"`.
- `ExpressionNode.assumed: string | null` — the mandatory reason,
  set on the **RHS row** when assumed. `null` everywhere else.

Documented at [docs/design/markers.md](docs/design/shipped/markers.md) §4.6;
[panel-info.md](docs/design/shipped/panel-info.md) details the wire field;
hover-ui.md adds the `🔵` and `(assumed: …)` glyph rows.

### Change: every hover is the same tree shape — `◂` retired, intrinsics join the tree path

All short hovers — including `+`/`-`, assignment, and relational —
now render the same root-plus-immediate-children tree shape used by
the call hover. The `◂` notation (value flowing into target) is
retired: it was a learnable glyph that needed explanation, and the
density advantage was small (`a : K ◂ b : K` vs three short rows).
One shape across every hover wins on legibility and on mental
model.

- **Assignment short** carries `(expected <lhs_unit>)` on the RHS row
  when the homogeneity check fails — same mechanism as a call-arg
  mismatch, and the RHS row paints 🟡 from the 🟡-on-`expected`
  override. The directional information `◂` used to carry ("RHS
  flows into LHS") is now explicit in the annotation.
- **`+` / `-` short** lose the `◂` operand-pair form in favour of
  root row + operand child rows. A homogeneity violation paints the
  root 🔴 via `H002` (worst-of), and the operand rows show their
  resolved units so the reader sees *which* operand is wrong.
- **Relational short** loses the `◂` form too. Relational expressions
  are structural-no-unit (root row carries `-`), and the checker
  doesn't emit on operand mismatches at relational sites, so the
  root stays 🟡 (no consistency diagnostic) regardless of operand
  agreement — unchanged semantically; just the layout shifts.
- **Intrinsic call hovers** (`log(p)`, `exp(t)`, `sqrt(x)`, etc.)
  switch from the bare-identifier-fallback one-liner to the full
  call-tree renderer (`_render_call_tree`). User-defined calls and
  intrinsic calls now look structurally identical — same root row,
  same child rows, same alignment. Intrinsics have no `(expected …)`
  annotation on args (we don't track formal-arg units for them) and
  no associated diagnostic, but the unit resolution still works
  because the checker's `resolve_unit` handles intrinsics natively.

### Change: short hover for `*` / `/` / `**` and sub-expressions now shows root + immediate children

Brings these surfaces into line with the call hover: every short
hover means "this expression's unit, with one level of how it got
there". The cursor-on-`*` / `/` / `**` short hover and the generic
computed-sub-expression short hover both now render a root row +
one child per operand, using the same tree renderer as the call
hover (`_render_ast_tree` with `max_depth=1`). The `+` / `-`
homogeneity short hover, the assignment short hover, and the
relational short hover keep their `◂` one-liner shape — those are
homogeneity-check surfaces where `◂` carries direction semantics.

### Change: three glyphs, three meanings, for "no unit" — `-` vs `?` vs `(none)`

The hover trace, panel expression tree, and panel scope/import
sections previously rendered "no unit" three different ways
(hover used `?`, panel hid the column, scope/import used `(none)`).
Unified so each glyph has exactly one meaning:

- `-` — **structural-no-unit**: the row has no unit by design
  (assignment statements, relational expressions, subroutine calls).
  Rendered identically by hover and panel.
- `?` — **unknown unit**: the row could have a unit but doesn't yet
  (unannotated identifier, unsupported intrinsic, partial
  resolution). Used inside expression trees AND for unannotated
  declarations in the panel's scope / import sections (previously
  `(none)`).
- `(none)` — **empty (sub-)section header only** (e.g. `Scope:
  (none)`, `Imports: (none)`). Never used inside a row or for an
  individual variable.

Side effect on subroutine-call rows: a clean subroutine call now
paints 🟢 (it's in `_NO_UNIT_NODE_TYPES`, so its resolution-axis
base is 🟢), instead of the previous 🟡 from "unresolved unit". The
marker still rolls up worst-of-children, so 🟡/🔴 inside args still
propagates to the root. Spec at
[docs/design/markers.md](docs/design/shipped/markers.md) §4.5.

Wire-format: `ExpressionNode.unit` is now always a string (`"-"` /
`"?"` / a unit), never null. Companions that still treat null as
"hide the unit column" will silently render the string instead — no
crash, just a small visual change for pre-0.2.1 companions on
post-0.2.1 servers.

### Change: call hover unified with the side panel's Expression tree

- The **call hover** (function or subroutine, on the callee
  identifier) now renders through the same tree renderer as the side
  panel's Expression section. Root row reads `name(args) : ret` —
  full call as written, with the return unit attached and the overall
  verdict marker. Child rows are one per actual argument labelled by
  source text, with `(expected <formal>)` on a dimensional mismatch.
  Subroutines have no return unit so the root shows `?` and paints
  🟡 from the resolution axis (no consistency disagreement to report).
  Short mode renders root + children only; Detailed expands the
  per-argument sub-tree.
- The earlier intermediate `name: (u1, u2, …) → ret` header line on
  call sites is gone — it lives on now in the **pure-signature
  hover** (cursor on a function/subroutine *definition* header — no
  call site), which still collapses to that one-line signature with
  `?` slots flagging unannotated formals/return.
- **🟡-on-`expected` override.** On a call-arg mismatch the
  argument row paints 🟡 + `(expected <formal>)`, not 🟢. Rationale:
  the expression resolved cleanly here, but the caller disagrees with
  the formal it's flowing into — flagging silently with 🟢 would
  contradict the 🔴 painted on the enclosing call by H004. The
  override is bounded to "would otherwise paint 🟢 AND carries
  `expected`" so it never overrides a diagnostic-owned 🔴 or a 🟡 from
  resolution. Applies symmetrically in the trace hover and the panel
  payload — see [docs/design/markers.md](docs/design/shipped/markers.md) §4.4.
- The old "Signature ◂ Call" two-column pairing layout and the typed-
  language-style `name(arg: unit, …) : ret` signature line are gone.

### Change: rule IDs dropped from expression tree; `(expected …)` surfaces on call-arg rows

- The shared expression-tree renderer (powering both the in-buffer
  trace hover and the side panel's Expression section) used to append
  the unit-algebra rule ID (e.g. `(R4.1)`, `(R5.6)`) to every row.
  Removed — debug noise for the target audience; the information is
  reachable from logs and pytest when needed for checker triage.
- Replaced with the more useful `(expected <formal>)` annotation on
  call-argument rows whose actual unit dimensionally differs from the
  callee's formal. Closes the prior information gap between the call
  hover (which now surfaces the expected unit) and the panel tree
  (which only marked the row 🔴 with no context).
- Wire-format: `ExpressionNode.ruleId` → `ExpressionNode.expected`
  (see [docs/design/panel-info.md](docs/design/shipped/panel-info.md)). All
  three companions consume the new field.

### Change: misc polish + UX consistency

Several smaller fixes landed alongside the major changes above:

- **Uniform scale-mode-aware unit display.** With `--scale` (or
  `[scale] enabled = true` in `.dimfort.toml`), the multiplicative
  factor surfaces wherever a unit is rendered — Expression tree, the
  scope-var and imports "normalized" columns, hovers (e.g. `hPa`
  shows as `100×kg·m⁻¹·s⁻²`). Without scale mode the factor is
  hidden. Single rule: displays match what the checker is reasoning
  about, so a panel never claims more precision than the active
  checker mode.
- **Module procedures appear in the Scope panel** for module/program
  scopes, per Fortran host association — procedures defined in a
  module are visible from anywhere in that module. Pre-formatted as
  `name(args)` rows mirroring the Imports section convention.
- **`@unit_assume{...}` squiggle covers the full directive** (was
  just the leading `@`). U020 and the U002 emitted on a malformed
  `@unit_assume` unit now span the whole `@unit_assume{...}` range.
- **`P001` blue squiggle widens to cover the neighbour swallowed by
  error recovery.** Tree-sitter's error recovery commonly absorbs
  the immediately-following clean statement into the bad
  statement's parse node (the parent `assignment_statement` ends up
  with `has_error=True` and spans both lines); `P001` now uses that
  ancestor's span so the underline matches the actual untrustworthy
  region, instead of leaving the swallowed line silently empty in
  the Expression panel.
- **Hover format unified — fenced code blocks everywhere.**
  Variable, signature, and tree hovers all wrap their body in a
  fenced code block so markdown clients that style code blocks
  (VSCode, Neovim with rounded borders, etc.) get consistent
  monospace + tinted rendering across every hover surface. Bold
  name dropped from the variable hover.
- **`Undetermined read` → `Undetermined`.** The CLI's `interactions`
  group label, the panel's Interactions section header, and the
  `X001` conflict-message text all use the shorter form. The
  internal `KIND` value (`USES`) is unchanged.

### Docs: design-doc folder refreshed to match shipped behaviour

Several design docs in `docs/design/` had accumulated stale "draft /
not implemented yet" banners while their content had shipped.
Refreshed:

- `panel-info.md` — rewritten to reflect the shipped panel (six
  sections in order, including the new `viaModule` provenance field
  from the transitive-imports work).
- `content-hash-cache.md`, `interaction-points.md`, `scale.md` —
  each rewritten to match shipped behaviour (keys + wire format for
  the cache; the `dimfort/interactions` wire shape and `Undetermined`
  rename; the per-phase scale-mode status table and the uniform
  display rule).
- `markers.md` — wire enum updated to include `"assumed"`; the
  relational example corrected (relational is structural-no-unit,
  base marker 🟢 rather than the previously-described 🟡).
- `symbolic-exponents.md`, `symbolic-logwrap.md` — status banners
  flipped from "in design, no implementation yet" to "shipped
  2026-05-22". Algebraic rule tables remain authoritative; the
  step-by-step implementation plans are now historical.

This is a contributor / beta-tester quality-of-life pass, not user-
facing behaviour. The design-doc folder now matches what the code
actually does.

## [0.2.0] — 2026-05-27

First **beta**. Usable, tested, and proven against a representative
real-world Fortran codebase. The `@unit{}` annotation format, the diagnostic
codes, and the LSP protocol are deliberately **not** frozen yet — expect
they may still shift between `0.x` releases.

### Change: SI-style unit display + parser-safe `@unit{}` serializer

- Units now render in **SI style** everywhere they are displayed — a middle
  dot `·` between symbols and **signed-exponent superscripts** instead of a
  `/` denominator: `1/K` → `K⁻¹`, `m/s` → `m·s⁻¹`, `kg×m/s²` → `kg·m·s⁻²`. The
  `×` is now reserved for the numeric **scale factor** (`hPa` →
  `100×kg·m⁻¹·s⁻²`), so the separator distinguishes a factor from another base
  unit. Rational and symbolic exponents still fall back to `^(p/q)` /
  `^(<linear form>)`.
- The display is now produced by a **single** formatter shared by diagnostics,
  hover, and the side panel (the hover path previously had its own divergent
  renderer), so all three read identically.
- New `format_unit_source` serializer emits the ASCII `@unit{}` DSL
  (`kg*m/s^2`) that round-trips through the parser. The H010 *extract literal
  to a named PARAMETER* quick-fix now inserts a **parseable** annotation for
  compound units — previously the pretty form (`@unit{kg×m/s²}`) was not valid
  `@unit{}` syntax. The parser and the `@unit{}` grammar are unchanged; all
  existing annotations parse exactly as before.

### Change: detailed hover shows the assignment verdict on the root row

- In detailed-tree mode, the root (assignment) row now carries its
  🟢/🟡/🔴 marker on the row itself, matching the side panel, instead of
  only in the bold `DimFort` header. The header keeps its marker too.

### Feature: `panelInfo.imports` — use-imported symbols visible at the cursor

- The `dimfort/panelInfo` response now carries a structured **`imports`**
  list: every `use`-imported symbol visible at the cursor, grouped by
  source module, with each variable's `@unit{}` and each procedure's
  full **signature** (`name(arg-units) → return unit`; subroutines render
  with `—` for the return slot). Scoped by Fortran visibility — honours
  `only:` lists and `=>` renames, walks the enclosing scope chain, and
  carries the source location so the editor companion can click-navigate
  cross-file to where the symbol is declared. Implementation in
  `src/dimfort/lsp/imports.py`.

### Feature: `scaleMode` LSP initialization option

- New `scaleMode` initializationOption lets the editor companion override
  the project's `.dimfort.toml` `[scale] enabled` setting for the
  session: `"auto"` defers to the toml (default), `true`/`false` forces
  the magnitude layer (S001/S002) on or off. Surfaces in each companion
  as a setting + cycle command: VSCode `dimfort.scale.mode` /
  `DimFort: Cycle Scale Mode`; Nvim `scale_mode` setup arg +
  `:DimFortCycleScale`; Emacs `dimfort-scale-mode` +
  `M-x dimfort-cycle-scale-mode`. Reflected in `:DimFortStatus` (Nvim) and
  the companions' status surfaces.

### Feature: `P001` — "unparsed region" marker

- A new **info-level** diagnostic that flags regions tree-sitter couldn't parse.
  Where the parser left an `ERROR`/`missing` region the checker resolves
  nothing, so `P001` says so (a blue squiggle, no companion changes needed)
  rather than letting the absence of a squiggle imply the lines are clean.
- One `P001` per contiguous unparsed region (nested error nodes coalesced).
  Emitted inside `check`, so it inherits severity overrides and cpp line-map
  remapping. On by default; silence project-wide with `[diagnostics]`
  `P001 = "off"` (DimFort targets F90+, so a known-F77 file can opt out).
- Spec: `docs/design/unparsed-regions.md`.

### Fix: panel Scope section recovers under error-wrapped routines

- A single unparseable statement makes tree-sitter wrap the whole
  enclosing routine in an `ERROR` node, so the scope lookup found no
  `subroutine` / `function` node and the side panel's **Scope** section
  blanked for that routine — even though its declarations were still
  recoverable. The server now reconstructs the enclosing scopes
  line-based (`recover_scopes`) from the surviving header statements and
  matches each declaration to its innermost recovered scope, so the
  Scope section keeps listing the routine's variables (a module section
  still excludes its contained routines' locals; sibling routines don't
  bleed). The Expression section stays empty inside the unparsed region.
  Spec: `docs/design/panel-info.md`.

### Feature: `dimfort interactions <symbol>` — cross-site unit analysis + X001

- A new **on-demand** query that, for one variable, lists every site that reads
  or writes it across the workset, grouped by what each site says about the
  variable's unit: **Declaration** (the `@unit{}`), **Write** (the unit an
  assignment sets it to), **Read** (the unit a use requires of it), and
  **Undetermined read** (a read whose required unit couldn't be determined —
  none exists, or a coefficient was un-annotated).
- The required unit at a read is solved by propagating a known target down
  through `+`/`-`/`*`/`/` (a bare literal anchors a sum to `{1}`, even when a
  sibling term is unresolvable), reusing the existing resolver and
  `_assignment_homogeneity` — so the R4.4 literal-autocast rule applies and a
  literal init (`x = 0.0`) makes no false claim. No new dimensional logic;
  unknown stays unknown (never a false constraint).
- **New diagnostic `X001`** (ERROR, produced only by this query): fires when two
  sites disagree on a variable's *dimension* — **even when the variable is
  unannotated**, which the per-statement `check` pass cannot see. Phrased as
  conflicting unit *claims* (e.g. "write here claims `kg/(m³×s)`, but
  declaration … claims `1/s`"). `--scale` also treats magnitude disagreements as
  conflicts. Never crosses a scope boundary (same name in two routines = two
  variables).
- `--file` / `--scope` narrow a reused name. Array-element accesses (`x(i)`) and
  call-argument positions are handled. Spec: `docs/design/interaction-points.md`.
- **LSP**: new `dimfort/interactions` custom request — resolves the symbol under
  the cursor (or an explicit `symbol`), returns the serialised report
  (`points` + `conflicts`). Consumed by the editor companions' Interactions
  panel section.
- Internal: extracted `ts_checker._build_ctx` as the single source of truth for
  `_Ctx` construction, now shared by `check` and the new `interactions` query.

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
  VSCode to follow). Spec: [`docs/design/panel-info.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/shipped/panel-info.md).
- **R4.4 — literal initialization autocast.** When the sole RHS of an
  assignment is a pure-numeric constant (literal, unary-minus literal,
  or arithmetic of literals), it takes on the LHS's unit and no
  diagnostic fires — `t = 2.0` where `t : s` is initialization, not an
  implicit cast. The existing D1.5 H010 still fires for literals buried
  in compound expressions (`t = c + 2.0`). Documented in
  [`docs/unit-algebra.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/unit-algebra.md).
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
  Real-world workspace measurement: cold 33 s → warm 20 s; the check phase alone
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
  Documented in [`docs/design/content-hash-cache.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/shipped/content-hash-cache.md);
  user guide in [`docs/usage.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/usage.md#content-hash-cache).
- **Key dimensions covered.** Source bytes, cpp include closure,
  `external_modules`, `extra_defines`, `extra_include_paths`, the
  project units-table file contents (`units_file_hash`), and
  `[diagnostics]` severity overrides. Editing any of these
  invalidates affected entries.

### Workspace check perf

- **Phase-C consolidation.** `collect_parameter_values` folded into the
  existing combined `variable_declaration` walk (`collect_var_types_
  type_fields_and_parameter_values`). Recovers the ~2 s real-world-workspace
  check-phase regression introduced by the OQ4 PARAMETER-aware exponents
  work. Same pattern as the 2026-05-17 var-types + type-field-types
  merge.

### Unit-algebra for LOG / EXP-tagged quantities (Phase B)

Three new diagnostic classes cover wrapper arithmetic. Each is
emitted with an existing `H001` / `H002` severity code; the
specific rule appears as a `(D1.x)` tag in the message and as a
rule ID (`R5.6`, `R6.5`, etc.) in `--trace` output. The full rule
set is documented in
[`docs/unit-algebra.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/unit-algebra.md).

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
- **Spec at [`docs/hover-ui.md`](docs/editor-integration/hover-ui.md)** — six layouts
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
[`docs/design/symbolic-exponents.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/shipped/symbolic-exponents.md).

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
- **Closed 3 Exner D1.4s** in the validation workspace.
- **Rendering.** `format_unit` understands symbolic slots and prints
  `Pa^(2/7·kappa)` rather than the previous fallback.

### Symbolic LogWrap multipliers

Same machinery applied to `combine`'s R5.4 path (LogWrap × scalar).
The log-power identity `γ·LOG(p) = LOG(p^γ)` now accepts an
`Exponent` multiplier so dimensionless-but-symbolic scale factors no
longer fire D1.4. Spec:
[`docs/design/symbolic-logwrap.md`](https://github.com/ArrialVictor/DimFort/blob/main/docs/design/shipped/symbolic-logwrap.md).

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
  [docs/unit-algebra.md §11](https://github.com/ArrialVictor/DimFort/blob/main/docs/reference/unit-algebra.md#oq5--resolved-missing-annotation-propagation).
- **Outer-unary-minus sign propagation in `_resolve`**: `-1.0 *
  LOG(p)` (parsed by tree-sitter as `-(1.0 * LOG(p))`) now sees
  R5.4 with `k = -1` and types as `LOG(1/Pa)` rather than the
  pre-fix `LOG(Pa)`.

### Internals — LSP modularisation, public API, strict typing

- **LSP server split.** The `lsp/server.py` monolith (~3,900 lines) is
  now a ~1,200-line registration spine delegating to focused handler
  modules (`hover`, `panel`, `interactions`, `tree_access`, `tree_nav`,
  `expr_tree`, `decl_scan`, `markers`, …). Shared mutable state moved
  behind a single `lsp.state` singleton; cached-tree handlers serialise
  on `state.ts_handler_lock`. No behavioural change.
- **Public `ts_checker` API.** The checker's expression-resolution and
  assignment-verdict entry points (`resolve_unit`, `assignment_
  homogeneity`, `Ctx`, `build_ctx`) are now a documented, stable surface
  shared by the CLI, every LSP render path, and the `interactions`
  query — one source of truth so markers can't disagree with the
  diagnostic stream.
- **Strict typing end-to-end.** `mypy --strict` now runs clean over the
  whole `src/dimfort` package with zero per-module exemptions (the
  `ignore_errors` ratchet is gone) and is enforced in CI. The
  unit-value model is `UnitExpr = Unit | LogWrap | ExpWrap` throughout.

### Tooling

- **Per-push CI**: ruff + pytest (+ mypy) on `ubuntu-latest` / Python
  3.12 for every push to `main` and every PR. Full 3 × 3 OS × Python
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
