# Coverage visualization — design spec (FUTURE)

**Status:** future feature, design exploration. Captures the design
direction reached at the close of the 0.2.4 planning discussion. Targets
0.2.4 as the first item in that release.

The goal is a per-line view of "what does DimFort know about this line"
that lets a user — typically a climate scientist onboarding DimFort onto
a partly-annotated codebase — see at a glance which regions are
checked-and-OK, which need attention, and which are out of scope.
Reuses the existing checker output: no new analysis, only a different
projection of data already computed by `check_files`.

## 1. Problem this solves

A team adopting DimFort on an existing Fortran codebase needs to know,
without running through diagnostics one by one, which regions of the
code are dimensionally verified, which are flagged, and which DimFort
hasn't been told about at all.

Today, the surface answer is the **Problems panel** (LSP) or the
diagnostic stream (CLI). Both list issues; neither communicates the
*positive* signal: "these lines are dimensionally consistent." A user
inspecting a 500-line subroutine has no way to see "this 80% is fine,
this 20% needs work" without scanning for the absence of squiggles —
which doesn't distinguish "no squiggle because it's clean" from "no
squiggle because DimFort hasn't analysed this region."

The visualisation closes that gap. It paints a per-line marker for
**every** line: green for verified-OK, yellow for needs-attention, red
for hard fire, blue for unparsed, uncoloured for out-of-scope. The
user reads adoption progress at a glance.

## 2. Why visualisation, not a new diagnostic

A diagnostic-driven approach (emit an info-level diagnostic on every
verified line) would flood the diagnostic stream. The Problems panel is
designed to show **issues**, not adoption status. A 500-line
fully-annotated routine emitting 500 INFO diagnostics drowns the
two warnings that actually matter.

Visualisation lives in a separate channel: a per-line decoration layer
the user toggles independently of diagnostics. The diagnostic stream
stays a stream of issues; the coverage layer stays a stream of status.

## 3. Status taxonomy

Four tiers + a no-decoration default. Reuses every shipped colour from
`markers.md`; introduces no new colour.

| Tier | Trigger | Existing parity |
| --- | --- | --- |
| **Green** | Line carries an `@unit` annotation comment, OR carries unit-typed expressions; checker resolved them and no consistency-family diagnostic owns the line, AND no identifier on the line is in the file's `U005` set | matches panel/hover green |
| **Yellow** | A `U005` / `H010` / `S001` / `S002` (warning-severity quality / scale) diagnostic owns the line, OR an expression on the line references an identifier from the file's `U005` set (use-site propagation; see §3.3), OR the line is a declaration of a unit-bearing type (`real`, `double precision`) without an `@unit` annotation (resolution-axis yellow; see §3.4) | matches panel/hover yellow |
| **Red** | An ERROR-severity consistency-family diagnostic owns the line — dimension homogeneity (`H001` / `H002` / `H003` / `H004`), polymorphism unification failure (`H020` / `H021` / `H022` / `H023`), affine-conversion-directive validation (`S003`), or unparseable annotation (`U002`) | matches panel/hover red |
| **Blue** | A `P001` (unparsed region) diagnostic owns the line | matches panel/hover blue |
| **— (no decoration)** | Line has no unit semantics (string assignments, control flow, comments, blank lines, decl-only lines with no expression) | uncoloured |

Three observations:

- The four-colour scheme is exactly the existing markers system. A user
  who has internalised `🟢🟡🔴🔵` from the side panel will read the
  gutter the same way.
- **No-decoration is distinct from green.** Uncoloured means "this line
  doesn't need checking" (a string assignment, a `goto`, a blank line).
  Green means "DimFort actively verified this line and it passed."
  Adoption progress reads as `green / (green + yellow + red + blue)`,
  with the no-decoration count showing how much of the file is out of
  scope.
- **Blue is distinct from no-decoration.** Blue means "DimFort tried to
  reach this line but the parser couldn't recover a unit-checkable
  AST." Blue lines are candidates for `#ifdef` cleanup or syntax fixes;
  no-decoration lines aren't.

### 3.1 Why no orange for `H010`

The earlier design proposed orange for hint-level fires. The shipped
scheme does not include orange; introducing it would commit a fifth
colour across panel, hover, and now this layer. The cost-benefit
doesn't justify it: `H010` is a hint-level fire that the panel and
hover already render in yellow, and the coverage layer's purpose is
"what's the status here" — `H010` is "needs attention," which yellow
already conveys. Folded into the yellow tier.

### 3.3 U005 propagation to use sites (validated 2026-06-06)

The checker emits one `U005` diagnostic per unannotated declaration,
attached to the *declaration line*. The coverage projection
deliberately propagates the yellow signal to every line that
*uses* the unannotated variable, not only the declaration line.

**Why.** Without propagation, removing an annotation from a
declaration can make a previously-flagged use site look *better*:
a line that fired `H001` (because the variable's now-missing unit
mismatched another) loses its red diagnostic (the checker can no
longer compute the RHS) and falls back to green via the other
annotated identifiers on the same line. That reads as "removing
an annotation fixed the line" — exactly the wrong signal.

**Rule.** During the green-paint walk, the projection inspects
expression-bearing statement nodes (per the existing walk) and
classifies each statement against two name sets:

- **Annotated names**: lower-cased keys of `attachments.var_units`.
- **Unannotated names**: lower-cased names extracted from the
  file's `U005` diagnostic messages (the variable name appears as
  a single-quoted token at the start of the message — stable
  across server versions because the message shape is part of the
  documented diagnostic surface).

A statement that references at least one unannotated name paints
its spanned lines yellow. A statement that references only
annotated names paints green. A statement with neither stays
uncoloured. Worst-wins still applies: a line already painted red
by step 1 stays red.

**Worked example** (qa.f90, 2026-06-06):

```
real :: c_sound   !< @unit{m/s}    (annotated)
real :: t                          (unannotated → in U005 set)
real :: bogus     !< @unit{kg}     (annotated)
bogus = c_sound * t                ! line N
```

Line N references `bogus` and `c_sound` (annotated) AND `t`
(unannotated). The unannotated reference dominates: line N paints
yellow. When `@unit{s}` is restored on `t`, the H001 fires (RHS
resolves to m, LHS is kg) and line N becomes red. The transition
on annotation removal is therefore red → yellow, never red → green.

This rule moves the yellow / green boundary from the literal
"diagnostic owns the line" interpretation to "the line participates
in an unannotated variable." It costs one regex match per `U005`
diagnostic on projection (cheap; bounded by the number of `U005`
diagnostics in the file, typically O(declarations)).

### 3.2 No green sub-tier for polymorphic verification

A line whose result type is `'a` (a tyvar — unit depends on the caller)
passes the polymorphic check just like a line whose result is `Pa*s`.
The polymorphic check verifies that any concrete instantiation will be
dimensionally consistent — soundness over an infinite family. There is
no weaker-than-concrete sense; both are fully verified. The coverage
view does not differentiate.

### 3.4 Unannotated unit-bearing declarations paint yellow (validated 2026-06-07)

A declaration of a `real` (or `double precision`) variable without an
`@unit{}` annotation paints yellow, regardless of whether `U005`
happens to fire. Matches the panel / hover resolution axis: 🟡 means
"could carry a unit, doesn't yet."

**Why this isn't already covered by `U005`.** `U005` fires only on
declarations whose variables are *also used* in a unit-checked
expression. A declared-but-never-used `real :: density` produces no
diagnostic — but the panel still shows it as unannotated 🟡, and the
coverage layer must agree. Without this rule, such declarations would
read as out-of-scope (uncoloured), giving the false impression that
DimFort doesn't care about them.

**What counts as unit-bearing.** The intrinsic-type tokens `real`,
`double precision`, and `double`. `integer`, `character`, `logical`,
and derived types are not unit-bearing and don't paint at all from
this rule — they carry no coverage signal.

**Detection.** Walk every `variable_declaration` node, check its
`intrinsic_type` child against the unit-bearing set, and check
sibling `comment` nodes on the declaration's last line for the
`@unit` marker. Declarations whose intrinsic type is unit-bearing
AND whose siblings carry no `@unit` comment paint yellow.

## 4. Three rendering layers

Two visible layers in v1, plus a mode-controlled escalation.

### 4.1 Gutter signs

Coloured dot in the editor's left margin, one per line. This is the
universal pattern for test-coverage tools.

| Companion | Mechanism | Notes |
| --- | --- | --- |
| VSCode | `TextEditorDecorationType` with `gutterIconPath` | Custom SVGs per tier |
| Nvim | `vim.fn.sign_define` + `vim.fn.sign_place` | Built-in `signcolumn` |
| Emacs | `fringe` bitmaps + overlays | Built-in `left-fringe` |

All three companions support gutter signs natively. No plugin
dependencies.

### 4.2 Line background tint

Subtle background colour wash on the whole line. Opt-in via mode
`background`. Heavier visual weight.

| Companion | Mechanism |
| --- | --- |
| VSCode | `TextEditorDecorationType` with `backgroundColor` (rgba with low alpha) |
| Nvim | extmark with `line_hl_group` |
| Emacs | overlay with `face :background` |

All three companions support per-line background colour natively.

### 4.3 Scrollbar markers — explicitly out of scope

VSCode supports `overviewRulerColor` natively, giving an at-a-glance
file-overview navigation strip. Nvim and Emacs do not:

- Nvim has no built-in scrollbar; the common pattern is via the
  `nvim-scrollbar` plugin or `statuscolumn` workarounds.
- Emacs's `scroll-bar-mode` is rendered by the GTK / Cocoa toolkit
  and does not expose per-line colouring.

Adding plugin dependencies for a marginal navigation gain would
expand the companion surface area for unclear benefit; the LSP's
existing Problems panel and `:lopen`-style commands already serve the
"navigate to issues" need.

Decision: gutter + tint only, uniformly across the three companions.
Scrollbar markers are a parked extension; they would land in a future
patch only if a strong user signal demands them.

## 5. Mode setting

A single setting key per companion controls the visualisation level:

```
dimfort.coverage.mode: "disabled" | "gutter" | "background"
```

| Mode | Renders | Use case |
| --- | --- | --- |
| **disabled** | Nothing. Companion suppresses the LSP request and the rendering pass entirely. | User who wants the diagnostic stream only |
| **gutter** | Gutter signs in the four tiers, in the left-margin column. | User who wants at-a-glance status without tinting the code body |
| **background** | Low-alpha background tint behind each in-scope line, in the four tiers. | User who prefers the heavier visual weight that paints behind the text |

**Gutter and background are mutually exclusive** (validated during
the VSCompanion smoke 2026-06-07). The earlier shape — `verbose =
gutter + background` — was reconsidered: both layers encode the same
per-line tier, so showing them together is redundant rather than
informative. The honest framing is "pick the visual encoding you
prefer," and the mutually-exclusive shape makes the cycle command
transitions cleaner (each step clears the previous mode's
decorations).

Three modes is enough granularity. Two would conflate "no decoration"
with "I want one of the visual encodings"; four would over-specify.

Default: `disabled` in v1. Users opt in explicitly. Rationale: an
opt-in feature on first ship lets early adopters validate the
taxonomy and the visual choice before the default flips. The default
can be promoted to `gutter` once the feature stabilises (separate
release decision; not in v1).

## 6. Gutter-clash resolution

The editor already paints native diagnostic icons in the same gutter
column it provides to our coverage layer:

- VSCode shows a red circle-X for `Error`, a yellow triangle for
  `Warning`, etc., automatically from `publishDiagnostics`.
- Nvim's `signcolumn` shows similar via the `LspDiagnosticSignError`
  / `LspDiagnosticSignWarn` / etc. signs registered by the LSP client.
- Emacs's flycheck-style display does the same.

If we also paint a red coverage sign on a line with a hard fire, the
gutter would render two red icons (or one would overwrite the other,
depending on priority resolution). Visual noise without information
gain.

**Resolution (provisional):** the coverage layer paints **green** and
**blue** only on lines without a native diagnostic icon. Lines with a
yellow / red diagnostic skip the coverage gutter — the native icon
already communicates that level of attention. The coverage layer's
actual value-add is the **positive signal**: green for "this line is
verified fine," which the native diagnostic stream cannot express.

**Status update (2026-06-06, validated during VSCode smoke):** this
provisional was reversed. The "step aside on native diagnostic icon"
rule assumed editors paint diagnostic icons in the gutter column —
but VSCode does not by default: diagnostics surface as squiggles in
the editor text, entries in the overview ruler, and rows in the
Problems panel, while the gutter column itself stays bare unless an
extension paints there. Skipping yellow / red coverage dots therefore
leaves those tiers with no per-line gutter indicator at all, which
reads as "the coverage layer only flags positives."

**Validated rule:** the coverage layer paints **all four tiers** in
the gutter. This:

- Carries a per-line indicator the user can read at a glance for
  every tier — green / yellow / red / blue.
- Coexists with the inline squiggles (squiggles are in the text,
  the coverage dot is in the gutter column — no competition).
- Matches the test-coverage convention familiar from
  Coverage Gutters in VSCode and similar tools.

The companion implementations should follow the same rule: paint
every tier in the gutter, unless the target editor *does* show
diagnostic icons in the gutter by default. The Nvim and Emacs
implementations should verify against their own defaults during their
own smoke walks; the default position is "paint all tiers" unless
proven otherwise.

Background mode is different: the line tint sits behind the text,
not in the gutter column, so it never competes with the editor's
native diagnostic surface (squiggles in the text, icons in editors
that paint them in the gutter).

Concretely:

```
gutter mode, line with H001:        [coverage red ●] foo = bar * baz
gutter mode, line with U005:        [coverage yellow ●] qux = quux
gutter mode, line verified OK:      [coverage green ●] x = y * z   ! @unit{m}
gutter mode, line with P001:        [coverage blue ●] some_unparseable_stmt
gutter mode, line out of scope:     [no icon] do i = 1, n

background mode replaces every gutter dot with a low-alpha tint
behind the text on the same line.
```

## 7. Wire format

A new LSP method, paralleling `dimfort/panelInfo` / `dimfort/interactions`:

### 7.1 Request

```
method:  "dimfort/lineStatus"
params:  { "uri": "file:///path/to/file.f90" }
```

### 7.2 Response

```jsonc
{
  "uri": "file:///path/to/file.f90",
  "version": 12,                    // textDocument version this status reflects
  "lines": [
    { "line": 12, "status": "green" },
    { "line": 13, "status": "yellow" },
    { "line": 14, "status": "green" },
    { "line": 17, "status": "red" },
    { "line": 23, "status": "blue" }
  ]
}
```

Lines not appearing in the array are out-of-scope (no decoration).

### 7.3 Edge cases

- **File not in workset.** Response with empty `lines` array, current
  `version`. Companions render no decoration; uncoloured = absence of
  data is consistent with the no-decoration baseline.
- **File parse failure (no AST).** Same as not-in-workset: empty
  `lines`. The companion may surface a status-bar note ("DimFort
  could not parse this file") via a separate channel.
- **Partial parse failure (`P001` regions interleaved with parsed
  regions).** Per-line: parsed lines get their `green` / `yellow` /
  `red` per the taxonomy; `P001`-region lines get `blue`.
- **`@unit_assume` accepted lines.** The line carries an asserted
  unit; it counts as `green` for coverage purposes. The 🔵 in the
  panel marker scheme refers to per-row overlay (see `markers.md`
  §4.6); coverage `blue` is reserved for `P001`. They do not collide
  because `@unit_assume` carries no `P001`.

### 7.4 Lifecycle

- Companions request on `didOpen` and re-request on `didChange` (with
  debounce, matching the existing checker debounce) and `didSave`.
- Server responds from the last cached `WorksetResult` (`_last_result`
  in `state.py`, same source the markers system reads — see
  `markers.md` §4 caveat 1).
- Companions cache the response keyed by `(uri, version)` and replay
  decoration when the user toggles the visualisation mode on, without
  another round-trip.

## 8. Coverage statistics

The same data feeds a second feature: aggregate counts. Two surfaces:

### 8.1 CLI: `dimfort coverage <paths>`

New CLI subcommand alongside `check`, `interactions`, `lsp`. Output
similar to test-coverage tooling:

```
$ dimfort coverage src/

File                                       OK    Warn   Fire  Unparsed   Out  Coverage
src/dynamics/dyn3d_common.f90              412     8     0        0     78      98.1%
src/physics/large_scale_clouds.f90         234    45     3        2    102      83.5%
src/physics/turb_mod.f90                     0    87    11        0    120       0.0%
...
Workset total                            12847   612    47       18   4231      95.1%

Coverage = OK / (OK + Warn + Fire). Unparsed and out-of-scope
lines are both excluded from the denominator: unparsed regions
are a tool limitation (no annotation can move them), out-of-scope
lines aren't checkable. The percentage measures "how close to
homogeneous is the checkable, parseable surface" — a fully
annotated workset reaches 100% even when P001 regions exist.
Unparsed shows in the per-row column so a large P001 area is
still visible; it just doesn't drag the headline number.
```

The denominator deliberately excludes out-of-scope lines: a 500-line
file with 100 lines of comments / blank / control flow has 400
checkable lines. The coverage percentage measures how many of the 400
DimFort verified, not 400/500. Unparsed regions are excluded for an
analogous reason: a P001 block is not annotatable, so counting it
against the user would conflate annotation effort with parser
coverage — two different concerns.

Flags (v1):

- `--json` — emit machine-readable JSON for downstream consumption.
- `--by-module` — group by module instead of per-file.
- `--summary` — workset total only, no per-file rows.

CI-gate flags (`--threshold N`, per-file thresholds, etc.) are
deferred past v1. The `--json` output is enough for downstream tools
to implement their own gating; the question of what the canonical
CI-gate UX should be can be revisited once there is real demand.

### 8.2 LSP: `dimfort/coverageStats`

```
method:  "dimfort/coverageStats"
params:  { "uri"?: string }      // optional; omit for workspace-wide
response:
{
  "scope": "file" | "workspace",
  "uri":   "file://..."?,         // present if scope=file
  "files": [
    {
      "uri": "file://...",
      "ok": 412,
      "warn": 8,
      "fire": 0,
      "unparsed": 0,
      "out": 78,
      "coverage_pct": 98.1
    },
    ...
  ],
  "total": { "ok": 12847, "warn": 612, "fire": 47, "unparsed": 18, "out": 4231, "coverage_pct": 95.1 }
}
```

See §8.3 for the companion-side rendering: an always-visible bar
segment in the side panel plus an on-demand report buffer.

### 8.3 Companion stats UI

The wire data from §8.2 drives two surfaces inside each companion:
an always-visible bar segment in the side panel, and an on-demand
report buffer / view.

#### 8.3.1 Panel bottom bar

The side panel already has a bottom bar; the coverage feature
adds a single line.

**Default (0.2.4 ship form)** — File segment only:

    File: 78% (🟡 18 🔴 2)

Cheap (one file's projection, refreshed on every
diagnostic-change signal). Collapses to `File: —` when no `.f90`
file is active. Always on; no opt-in needed.

**Full form** (opt-in via `dimfort.coverage.workspace_stats =
manual | automatic` — see §13.2 for the rationale):

    File: 78% (🟡 18 🔴 2)  ·  WS: 73% (🟡 412 🔴 38)

The `WS:` segment aggregates over **every Fortran file in the
workspace**, not just the active file's transitive `use`-closure.
This is a deliberate choice: a user expecting "workspace
coverage" expects a stable project-level number, not one that
shifts as they switch tabs. The server runs a dedicated
`check_files` over all indexed files for this scope; the `File:`
segment continues to serve from the per-active-file cache
produced by the normal diagnostic flow.

When the WS segment is opted in and has no data yet (cold start,
or pre-`?` placeholder in `manual` mode), the renderer shows
either `WS: ?` (manual, clickable) or `WS: …` (automatic /
manual mid-compute), never `WS: 0%` — distinguishable from a
real 0% workspace, which carries at least one warn or fire.

**No diagnostic-event counts in the bar.** Editor chrome already
shows workspace W/E totals (VSCode status bar by default; Nvim
and Emacs via `vim.diagnostic` / flycheck / flymake when
configured — recommended in the user docs). Showing them again
inside the panel would force a glyph-family disambiguation
between "state of these lines" (coverage tiers) and "count of
fires" (W/E events), which measure different things: yellow
lines outnumber W diagnostics (propagation, the §3.4 declaration
rule), and red lines under-count E diagnostics (multiple fires
can share a line). Circles in the bar always mean "lines in this
tier"; events live in the editor's native chrome.

Clicking either segment opens the report (§8.3.2).

#### 8.3.2 Coverage report buffer

A single buffer covering both scopes. Layout:

    Workspace coverage: 73.4%   (🟢 1284  🟡 412  🔴 38  · 14 unparsed)

    File                                       Coverage    🟢     🟡    🔴   Unparsed
    src/phylmd/cv_routines.f90                    12.4%    11     74     3          0
    src/dyn3d/leapfrog.f90                        81.0%   213     48     2          0
    src/physics/condsurf.f90                      98.2%   164      3     0          0
    …

- **Sort**: by coverage % ascending (worst first → actionable).
  A v2 sort toggle (by path, by tier count) is parked.
- **Paths**: workspace-relative; the server emits `file://` URIs,
  the client strips to a relative form.
- **Activate row**: click / `<CR>` jumps to the file's first
  non-green line; falls back to line 1 when the whole file is
  green.
- **Refresh**: manual via `r` (or editor-idiomatic equivalent);
  the buffer also refreshes automatically while open on the
  same `DiagnosticChanged` / `onDidChangeDiagnostics` signal
  that drives the paint and the bar.
- **Unparsed** is a per-row column but doesn't enter the headline
  percentage (matches the §8.1 formula).

#### 8.3.3 Refresh model

File-scope and workspace-scope refresh on different cadences,
because workspace aggregation is expensive (one tree-sitter walk
per workset file under `state.ts_handler_lock`; on real-world
Fortran codebases of 1000+ files the un-cached cost is in the
tens to hundreds of milliseconds).

**File-scope** refreshes on every editor diagnostic-change signal
— cheap, one file:

- **VSCode**: `vscode.languages.onDidChangeDiagnostics`.
- **Nvim**: `DiagnosticChanged` autocmd.
- **Emacs**: `after-change-functions` with 0.5 s debounce +
  `after-save-hook`.

**Workspace-scope** sits behind an additional **2 s companion-side
debounce** on the same trigger. Without it, active typing produces
a fresh `WorksetResult` every ~400 ms (the server's `didChange`
debounce), and each refresh would re-walk the entire workset. The
2 s debounce caps aggregation at roughly one call every two seconds
during active editing, idle bursts excluded.

To make the staleness visible, the WS segment renders in a **muted
foreground** between a diagnostic-change signal and the arrival of
the corresponding `dimfort/coverageStats` response. Companion-side
state: `ws_stale: bool` flag set on every `DiagnosticChanged`,
cleared on stats-response. Visual: VSCode
`descriptionForeground`; Nvim a `Comment`-derived highlight group;
Emacs the `shadow` face.

File-scope does not need a stale marker — it moves in lock-step
with the squiggles the user can already see.

**Future optimisations (parked).** The 2 s debounce + identity-
keyed cache is the conservative first cut. Two follow-ups are
on the table if real-world profiling shows it's still too heavy
in steady state:

1. **Per-file tree-identity caching server-side.** Replace the
   whole-`WorksetResult` cache key with per-file
   `(id(tree), id(per_file_diagnostics))`. When the user edits
   one file, only that file's projection re-walks; unchanged
   files hit the cache. Steady-state cost becomes
   O(changed files) rather than O(workset). Conditional on the
   multifile checker reusing tree objects for unchanged files
   across re-checks (likely, given the existing content-hash
   cache infrastructure).
2. **`workspace_stats` user-facing tri-state**:
   `disabled | manual | automatic`. `manual` shows `WS: ?` with
   a click / command to compute on demand; `disabled` shows
   `WS: —` always. File-scope is always live regardless. Gives
   users on enormous codebases an explicit escape hatch even if
   the per-file cache is in place.

Neither is implemented in v1; profile first, then choose.

#### 8.3.4 Commands

Each companion exposes:

- `DimFort: Show Coverage Report` / `:DimFortCoverageReport` /
  `M-x dimfort-coverage-report` — opens the report buffer.
- (Existing) `DimFort: Cycle Coverage Visualisation` — unchanged.

The bottom-bar segments are clickable shortcuts to the report;
the command surface is for users who close the panel or prefer
keyboard-driven access.

## 9. Companion implementation notes

The per-companion work is mechanical — each companion already has the
infrastructure for decoration. Sketch shapes:

### 9.1 VSCode

Two decoration-type sets per tier: a gutter-only set and a tint-only
set. They are mutually exclusive — `gutter` mode applies the gutter
set; `background` mode applies the tint set; neither runs in
`disabled` mode. ``TextEditorDecorationType`` is immutable
post-creation, so building two sets at construction is what lets the
provider switch between modes cleanly.

```typescript
const gutterDecorations = {
  green: vscode.window.createTextEditorDecorationType({
    gutterIconPath: ctx.asAbsolutePath("media/coverage-green.svg"),
    gutterIconSize: "contain",
  }),
  /* ... yellow / red / blue ... */
};
const tintDecorations = {
  green: vscode.window.createTextEditorDecorationType({
    backgroundColor: "rgba(40, 167, 69, 0.10)",
    isWholeLine: true,
  }),
  /* ... yellow / red / blue ... */
};

async function refreshCoverage(uri: vscode.Uri): Promise<void> {
  if (mode === "disabled") return;
  const { lines } = await client.sendRequest("dimfort/lineStatus", { uri: uri.toString() });
  const buckets = bucketByStatus(lines);
  const editor = vscode.window.activeTextEditor;
  const active = mode === "gutter" ? gutterDecorations : tintDecorations;
  const inactive = mode === "gutter" ? tintDecorations : gutterDecorations;
  for (const tier of ["green", "yellow", "red", "blue"]) {
    editor.setDecorations(active[tier], buckets[tier]);
    editor.setDecorations(inactive[tier], []);  // clear the other mode
  }
}
```

### 9.2 Nvim

```lua
-- Sign definitions (gutter mode).
vim.fn.sign_define("DimfortCoverGreen", { text = "●", texthl = "DimfortCoverGreen" })
-- ... yellow, red, blue ...

-- Per-line background (background mode).
local ns = vim.api.nvim_create_namespace("DimfortCoverage")

local function refresh_coverage(bufnr, uri)
  if mode == "disabled" then return end
  client.request("dimfort/lineStatus", { uri = uri }, function(err, result)
    if err then return end
    -- Clear both layers so a mode switch removes the previous one.
    vim.api.nvim_buf_clear_namespace(bufnr, ns, 0, -1)
    vim.fn.sign_unplace("DimfortCoverage", { buffer = bufnr })
    for _, entry in ipairs(result.lines) do
      if mode == "gutter" then
        vim.fn.sign_place(0, "DimfortCoverage", "DimfortCover" .. capitalize(entry.status),
                          bufnr, { lnum = entry.line })
      elseif mode == "background" then
        vim.api.nvim_buf_set_extmark(bufnr, ns, entry.line - 1, 0, {
          line_hl_group = "DimfortCoverBg" .. capitalize(entry.status),
        })
      end
    end
  end)
end
```

### 9.3 Emacs

```elisp
(defvar dimfort-coverage--overlays nil)

(defun dimfort-coverage-refresh (uri)
  (when (not (eq dimfort-coverage-mode 'disabled))
    (lsp-request-async "dimfort/lineStatus"
                       `(:uri ,uri)
                       (lambda (result)
                         ;; Clear so a mode switch removes the previous layer.
                         (dolist (ov dimfort-coverage--overlays) (delete-overlay ov))
                         (setq dimfort-coverage--overlays nil)
                         (dolist (entry (gethash "lines" result))
                           (let* ((line (gethash "line" entry))
                                  (status (gethash "status" entry))
                                  (face (intern (format "dimfort-coverage-%s" status))))
                             (cond
                              ((eq dimfort-coverage-mode 'gutter)
                               ;; Fringe (gutter mode).
                               (overlay-put (make-overlay ...) 'before-string
                                            (propertize " " 'display
                                                        `((left-fringe dimfort-cover-bitmap ,face)))))
                              ((eq dimfort-coverage-mode 'background)
                               ;; Background tint (background mode).
                               (overlay-put (make-overlay ...) 'face
                                            `(:background ,(face-background face)))))))))))
```

## 10. Server-side implementation

Server-side work re-uses the per-line data the checker already produces.

### 10.1 Data source

For each file in the last `WorksetResult`:

- The checker emits a `list[Diagnostic]`; each diagnostic carries
  `start.line`, `end.line`, and `code`.
- The checker's tree-walker visits expression nodes; for each, the
  walk knows whether the node resolved to a `Unit` (green-eligible)
  or didn't (out-of-scope unless a diagnostic owns the line).

### 10.2 Per-line projection

For each file:

1. Initialise an empty per-line status map.
2. Walk the diagnostics: for each in the tier-mapped set (full
   tier→code mapping in §3), assign its tier to every line in
   `start.line ..= end.line`, taking the worst tier on collision.
3. Walk the tree for ``comment`` nodes carrying an ``@unit``
   annotation marker; paint every spanned line green (if not
   already painted by step 2). This catches every annotated
   declaration regardless of scope — previous implementations
   read `attachments.var_units_span`, which is keyed
   first-seen-wins on the variable NAME and therefore misses
   same-name declarations across scopes (e.g. a polymorphic
   `x` declared in every routine of a module). The comment-
   walk-based approach is robust against name collisions.
4. Walk the tree for ``variable_declaration`` nodes of unit-bearing
   intrinsic types (`real`, `double precision`); paint every line
   spanned by such a declaration yellow when no sibling ``comment``
   on the declaration's last line carries the ``@unit`` marker.
   This is the resolution-axis 🟡 rule from §3.4 — a declared-but-
   never-used real variable fires no `U005` but is still
   unannotated, and the panel surfaces yellow for it; the coverage
   layer matches.
5. Build the **unannotated name set** from the file's `U005`
   diagnostics by extracting the quoted variable name from each
   message (`'name' is used in a unit-checked expression...`). This
   is the set of names whose use sites should propagate yellow per
   §3.3.
6. Walk expression-bearing statement nodes. For each statement,
   classify by descendant identifier text against the annotated
   set and the unannotated set:
   - If any descendant matches an unannotated name → paint every
     spanned line yellow (worst-wins against an already-green or
     uncoloured line; red / blue / yellow from step 2 stand).
   - Else if any descendant matches an annotated name → paint
     every spanned line green (only on lines still uncoloured).
7. Lines not painted by any step stay out-of-scope (omitted from
   the response).

This is one extra pass over data already in memory; no re-check.

### 10.3 Handler placement

A new `lsp/coverage.py` module, paralleling `lsp/panel.py` /
`lsp/interactions.py`. The `server.py` spine adds a `@server.feature
("dimfort/lineStatus")` registration and delegates to
`coverage.resolve`. The handler does not need `state.ts_handler_lock`
— it reads from the cached `WorksetResult` only, like `panel.resolve`.

A second handler for `dimfort/coverageStats` aggregates over the same
projection.

### 10.4 CLI placement

A new `_run_coverage(args)` in `cli.py`, paralleling `_run_check` and
`_run_interactions`. Re-uses `check_files` for the analysis pipeline;
adds a counting + formatting pass over the result. The CLI dispatch in
`main()` gains a third subcommand branch.

### 10.5 Stats cache

`dimfort/coverageStats` aggregates over every file in the workset
and is hit by each companion on every diagnostic-change signal.
The aggregation is not free — each file's projection involves a
tree-sitter walk under `state.ts_handler_lock`, and on
larger real-world Fortran codebases the un-cached cost is in
the tens-to-hundreds of milliseconds.

The handler caches its full response keyed by
`id(state.last_result)`. `WorksetResult` is immutable per check
cycle and replaced on each new result, so the cache invalidates
naturally on result swap. First call after a check pays the
walk; subsequent calls — from any companion, at any scope — are
O(1).

The per-file `dimfort/lineStatus` handler does not need this
cache: it's already scoped to one file and is cheap.

## 11. Decisions resolved during spec review

The following points were raised during the spec review and locked in
for v1:

1. **Default mode at first ship: `disabled`.** Users opt in
   explicitly. Promotion to `gutter` deferred to a future release.
2. **Coverage-percentage semantics: per-line.** Per-AST-node is
   not in v1; could land as a future `--ast-nodes` flag if demand
   emerges.
3. **CI gate not in v1.** No `--threshold` flag, no per-file
   threshold flags. The `--json` output is sufficient for downstream
   tools to implement their own gating; canonical CI-gate UX is a
   future question.
4. **Gutter-clash resolution: paint all four tiers** (validated
   during the VSCode smoke 2026-06-06; see §6). VSCode does not
   paint diagnostic icons in the gutter by default, so the earlier
   "step aside on yellow / red" rule left those tiers with no
   per-line gutter indicator. The companion paints every tier;
   the Nvim and Emacs implementations should verify against their
   own defaults during their own smoke walks.
5. **Refresh trigger: `onDidChangeDiagnostics`** (validated during
   the VSCode smoke 2026-06-06). The companion listens for the
   editor's diagnostic-change signal rather than running its own
   post-edit timer; this keeps the coverage layer in lock-step
   with the squiggles and avoids any race against the server's
   internal debounce. The Nvim and Emacs implementations should
   use their equivalent (`LspDiagnosticsChanged` on Nvim,
   `flymake-diagnostic-functions` / `eglot--diagnostics-changed`
   on Emacs).

## 12. Open questions (remaining)

These are open for review and may shift before further companion
work:

1. **Promoting default to `gutter` later.** When the feature
   stabilises (post-0.2.4 user feedback), the default could flip
   from `disabled` to `gutter`. The flip is a one-line companion
   change but is visible to every user. Decision deferred to the
   release that flips it.
2. **`dimfort/lineStatus` revalidation cost.** For a 5000-line file
   with frequent edits, the per-line projection runs once per
   `didChange` debounce. Cheap, but worth measuring on a
   representative large workset before shipping to confirm.

## 13. Migration

Originally scoped as a single 0.2.4 release; revised after the
in-editor smoke walk on a real Fortran codebase (1900-file
workset, 50 s cold `check_files` per WS refresh) surfaced a
performance ceiling outside the coverage handler's control. The
release plan now spans three minor versions:

### 13.1 What landed in 0.2.4 already (paint + CLI + per-file LSP)

Shipped pre-feature in DimFort 0.2.4 main:

- `core/coverage.py` per-line projection.
- `lsp/coverage.py` `resolve` (`dimfort/lineStatus`) handler +
  per-file `stats` (`dimfort/coverageStats` with a `uri`).
- `dimfort coverage <paths>` CLI subcommand.
- Per-file projection cache keyed by `WorksetResult` identity.
- §8.1 formula refinement (`ok / (ok + warn + fire) × 100`).
- Three companion paint integrations: VSCompanion, Nvim, Emacs.

### 13.2 0.2.4: the stats bar (current target)

Server side:

- `lsp/coverage.py` workspace-scope branch: `dimfort
  /coverageStats` with no `uri` returns a workspace-wide aggregate
  rather than the per-active-file workset.
- Architecture: the workspace check runs on a daemon thread; the
  stats handler returns the last-cached aggregate instantly + a
  `wsStale` flag. Background refresh fires on dirty marks from
  `server.py`'s `didChange` / `didSave` handlers, behind an idle
  debounce so active typing doesn't trigger constant refreshes.
- Defensive cache (dedicated `CacheStore` in tempdir, independent
  of the user's `cache_mode`) so the cached-side cost stays low.

Companion side (one PR per editor, VSCode first):

- Side-panel bottom-bar segment: `File: <pct>% (🟡 N 🔴 M)` is
  the default shipped surface. The full `· WS: …` extension
  exists but is opt-in (per §8.3.1).
- New companion setting `dimfort.coverage.workspace_stats`:
  `disabled | manual | automatic`. **Shipping default:
  `disabled`** — the WS segment is omitted from the bar entirely
  (no separator, no placeholder). Rationale: in-editor smoke
  testing on a larger real-world Fortran codebase confirmed that
  the background workspace check holds the LSP `check_lock` for
  tens of seconds at a time, which freezes per-file diagnostic
  checks (squiggles + panel updates) for the duration. The
  async architecture moves the check off the request thread —
  but as long as `check_files` is slow, `check_lock` contention
  makes the editor feel unresponsive every time WS refreshes.
  The default ships off until 0.2.5's multifile cache makes the
  underlying check cheap enough.
  - **`manual`** (opt-in): WS shows `?` with a click / palette
    command to compute on demand. User accepts the freeze cost
    in exchange for the data.
  - **`automatic`** (opt-in): bar wires to the server's async
    refresh cycle. Same cost characteristics; recommended only
    on small worksets where the freeze is sub-second.
- Coverage report buffer (per §8.3.2) — single-buffer with
  workspace header + per-file rows, click-to-jump. Same opt-in
  story: visible / functional only when `workspace_stats` is
  enabled.

Spec moves nothing; this section accumulates entries as pieces
ship.

When 0.2.5's multifile cache lands, the companion default flips
from `disabled` to (probably) `automatic`. That's a one-line
companion change — no re-architecture needed; the
infrastructure built for 0.2.4 already supports all three
modes.

### 13.3 0.2.5: multifile cache (deep optimisation)

DimFort-wide infrastructure work that benefits the active-file
LSP loop, `dimfort.checkWorkspace`, AND the WS coverage bar
simultaneously. Captured in its own design doc:
[multifile-cache.md](multifile-cache.md). Headline targets:
load phase from 17.84 s → ~10 ms per edit, index phase from
3.51 s → ~50 ms.

On the coverage side, 0.2.5 flips the companion default from
`manual` to `automatic` and may shorten the server-side idle
debounce. No re-architecture; one-line default changes.

### 13.4 0.2.6+: other planned 0.2.4 items deferred

Panel sort order, LaTeX / siunitx symbol-table export,
backward-traced H004 diagnostic, infer-unit quick-fix. Each
listed in the corresponding parked-idea memory entry. None
depend on the coverage work; they were pushed back to make room
for the bar's architectural surprises (and the 0.2.5 cache
investment that follows).

## 14. Out of scope for this design

- **Tracking coverage over time.** Coverage history (commit-over-
  commit deltas) would require persistent storage. Out of scope.
- **CI integration beyond `--threshold`.** A GitHub Action /
  reviewdog plugin / etc. would consume the `--json` output but is
  not part of DimFort itself.
- **HTML report generation.** Test-coverage tools often emit static
  HTML; DimFort would defer to external consumers of the `--json`
  output if that ever becomes a demand.
- **Coverage of `@unit_assume`-accepted lines distinction.** The
  spec counts them as green (verified, just verified via an
  assertion). A future enhancement could split them into a
  green-but-assumed sub-bucket; not in v1.
- **Per-symbol coverage.** "How many symbols carry `@unit{}` vs not"
  is a different question, served by the `interactions` subcommand
  and (in the future) `dimfort audit`. Coverage is per-line.
