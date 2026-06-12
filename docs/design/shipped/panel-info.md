# Side panel — wire spec and rendering reference

The DimFort side panel is a persistent cursor-following view of the
unit information at the cursor. It shipped in 0.2.0 and is on by
default in every companion. This doc is its **wire spec** for the
LSP messages the server emits, and its **canonical rendering
reference** for the companions. Per-editor styling (filter UX,
keybindings, etc.) lives in the companion MANUAL_QA notes — not here.

The VSCode companion (`DimFort-VSCompanion/src/panel.ts`) is the
authoritative reference renderer when this doc and the code disagree.

## Section order

The shipped panel renders **six** collapsible sections, in this order:

```
▾ EXPRESSION       — unit-algebra tree for the expression at the cursor
▾ DIAGNOSTICS      — diagnostics on the cursor line
▾ INTERACTIONS     — cross-site unit constraints for the symbol at the cursor
▾ ACTIONS          — code actions available at the cursor
▾ SCOPE            — declarations of every enclosing scope, outermost first
▾ IMPORTS          — symbols brought into the cursor's scope by `use` clauses
```

Plus a flat **footer** with the whole-file diagnostic counts.

Each section header is uppercase and prefixed with a fold marker
(`▾` open, `▸` closed). Companions persist fold state per-section.
Sections render a `(none)` placeholder when empty rather than
disappearing, so the section list is stable as the cursor moves.

EXPRESSION and DIAGNOSTICS come from the `dimfort/panelInfo`
response. INTERACTIONS comes from a separate `dimfort/interactions`
request. ACTIONS is companion-side: it mirrors the LSP
`textDocument/codeAction` result for the cursor range, filtered to
DimFort's own actions. SCOPE and IMPORTS come from `dimfort/panelInfo`.

## LSP endpoints

### `dimfort/panelInfo`

```
request:   "dimfort/panelInfo"
params:    { uri: DocumentUri, position: Position }
response:  PanelInfo | null
```

`null` if the position is outside any analysable region (blank line,
comment-only line, file the server hasn't indexed).

```typescript
interface PanelInfo {
  // The expression's unit-algebra tree, or null if the cursor isn't
  // inside an expression context (e.g. on a declaration line only).
  expression: ExpressionNode | null;

  // Full chain of enclosing scopes, OUTERMOST first
  // (e.g. [module, subroutine] for a cursor inside a module-contained
  // subroutine). Each carries its declarations. Empty when the cursor
  // is at bare file level. The panel stacks one section per entry.
  scopes: ScopeSection[];

  // Diagnostics on the cursor LINE — so the panel can show *why* a node
  // is marked without a hover / Problems trip. Scoped to the line (not
  // the whole file). Empty array when the line is clean; renderers
  // show a placeholder rather than hiding the section.
  diagnostics: PanelDiagnostic[];

  // Symbols brought into the cursor's scope by `use` clauses — usable
  // here but not declared in any enclosing scope. Scoped like Fortran
  // visibility: a module-level `use` shows for any cursor in the
  // module; a routine-level `use` only in that routine. A name
  // declared locally in an enclosing scope shadows the import and is
  // omitted.
  imports: ImportVar[];

  // Whole-file diagnostic counts, for the panel footer.
  fileDiagnosticCounts: { error: number; warning: number };
}

interface ExpressionNode {
  // Human-readable label (the source slice, lightly normalised).
  label: string;
  // Unit string — always present (never null). The server resolves all
  // three "no unit" cases to a concrete glyph:
  //   * "-" — structural-no-unit (assignment statement, relational
  //           expression, subroutine call — no unit by design).
  //   * "?" — unknown unit (unannotated identifier, unsupported
  //           intrinsic, partial resolution).
  //   * <formatted> — resolved unit (e.g. "kg·m⁻¹·s⁻²").
  // See design/markers.md §4.5.
  unit: string;
  // Three-tier severity (`ok`/`warn`/`error`) plus a fourth
  // **overlay** value `assumed` (companions render 🔵). `assumed`
  // does NOT participate in worst-of aggregation — it appears only
  // on the RHS row of an assumed assignment as a per-row overlay,
  // and ancestors never inherit it. Severity worst-of stays
  // `error > warn > ok`. See design/markers.md §4.6.
  marker: "ok" | "assumed" | "warn" | "error";
  // The formal unit this node is expected to satisfy, only set when
  // this node is a positional argument of a call whose callee
  // signature is known AND the resolved unit dimensionally differs
  // from the formal. Renderers append `(expected <expected>)` to the
  // row. When a node carries `expected`, the server demotes `marker`
  // from `ok` to `warn` (the 🟡-on-`expected` override — see
  // design/markers.md §4.4); a row with `expected: <unit>` therefore
  // never reads `marker: "ok"`.
  expected: string | null;
  // The mandatory reason supplied with
  // `@unit_assume{<unit> : <reason>}`, set on the **RHS row** of an
  // assumed assignment (NOT on the assignment row itself — the
  // directive's syntactic subject is the RHS expression). When set:
  //   * `unit` carries the *asserted* unit (not the computed `?`).
  //   * `marker` reads `"assumed"` (companions render 🔵), unless a
  //     diagnostic owning this node paints 🔴.
  //   * Renderers append `(assumed: <reason>)` to the row tail.
  // The assignment row stays `marker: "ok"` when the homogeneity
  // check passes. A declared-unit conflict still fires H001 on the
  // assignment and paints it `"error"` — the assumption never masks
  // a declared-unit conflict. See design/markers.md §4.6.
  assumed: string | null;
  // Sub-expressions whose units feed into this one.
  children: ExpressionNode[];
}

interface ScopeSection {
  name: string;
  kind: "subroutine" | "function" | "module" | "program";
  vars: ScopeVar[];
}

interface ScopeVar {
  name: string;
  // The annotated unit text as written, or null for unannotated
  // declarations. For kind "error" this is the raw (unparseable) text.
  unit: string | null;
  // Base-SI normalized form, e.g. "Pa" → "kg·m⁻¹·s⁻²". The factor and
  // affine offset are included **only when scale mode is on**:
  //   * scale-off:  "hPa" → "kg·m⁻¹·s⁻²"  (dim expansion only)
  //   * scale-on:   "hPa" → "100×kg·m⁻¹·s⁻²"  (factor visible)
  //   * always:     "degC" affine offset is included under scale-on
  // Equals `unit` for base-SI annotations; null when the annotation
  // doesn't parse or is absent. Same gate applies to the unit column
  // of ExpressionNode and to ImportVar.unitNormalized. Rationale: a
  // linter shouldn't display information it's actively ignoring.
  // Renderers show the second column only when it differs from `unit`.
  unitNormalized: string | null;
  // 1-based line number of the declaration.
  line: number;
  // 🟢 annotated (valid unit), 🟡 unannotated (no @unit{}),
  // 🔴 error (has @unit{} but it failed to parse — the U002 set).
  kind: "annotated" | "unannotated" | "error";
}

interface ImportVar {
  name: string;          // local name (after any `=>` rename)
  unit: string | null;   // var: the source @unit{}; procedure: its return unit; else null
  unitNormalized: string | null;  // base-SI form, scale-mode gated (as ScopeVar)
  // The module that *originally declared* the symbol (lower-cased).
  // For a directly-imported name this equals the module on the cursor's
  // `use` line. For a transitively re-exported name — e.g. `g0` declared
  // in `phys_base` and re-exported through `phys_constants` — `module`
  // names `phys_base` (so nav jumps to the real declaration), and
  // `viaModule` names the intermediate hop.
  module: string;
  // Present only for transitively re-exported names; names the module
  // on the cursor's actual `use` line. Renderers may show "from
  // phys_base (via phys_constants)" or similar. Absent for direct imports.
  viaModule?: string;
  // For a procedure: a function with a return unit (or any subroutine) is
  // "annotated"; a function lacking a return @unit{} is "unannotated".
  kind: "annotated" | "unannotated";
  // True for an imported function/subroutine. ``signature`` is the
  // parenthesised argument units (e.g. "(kg, m)", "()", "?" for an
  // un-annotated arg); absent for a variable.
  callable: boolean;
  signature?: string;
  // Navigation target: the imported symbol's DECLARATION in the source
  // module (cross-file), resolved via the workspace module exports.
  // Falls back to the `use` clause's own line in this file when the
  // source declaration can't be located; `file` is then absent.
  file?: string;
  line: number;
  column: number;
}

interface PanelDiagnostic {
  severity: "error" | "warning" | "info" | "hint";
  code: string;     // H001 / S002 / U005 / …
  message: string;
  // 1-based span, so a click can land on (and select) the exact range.
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
}
```

#### Scope construction

The enclosing scope is the innermost `subroutine` / `function` /
`module` / `program` node. For routine scopes the declarations are
matched by `DeclarationSite.scope` (the routine name); for module /
program scopes, top-level declarations (`scope is None`) are matched
by line span so nested routines' locals are excluded.

For **module and program scopes**, the server also emits a row per
procedure defined inside the scope's line span (functions and
subroutines visible by host association). These rows reuse the
`ScopeVar` shape so renderers don't need to special-case them:

- `name` is pre-formatted with the argument-unit signature, e.g.
  `gravity_at(m)` or `set_state(kg, K)`.
- `unit` is the return unit, or `-` for subroutines (structural-no-unit,
  same glyph used in call rows of EXPRESSION and IMPORTS).
- `kind` is `"annotated"` when the return is known *or* the procedure
  is a subroutine; `"unannotated"` for functions lacking a return
  `@unit{}`. Same marker mapping as `ImportVar`.

**Error-recovery fallback.** A single unparseable statement makes
tree-sitter wrap the whole enclosing routine in an `ERROR` node, so the
`subroutine` / `function` node disappears and the scope lookup finds
nothing. When that happens, the server reconstructs the enclosing
scopes line-based (`recover_scopes`): the routine *header* statement
survives inside the `ERROR`, so each scope's name + kind comes from
the surviving headers, and each scope's extent comes from pairing
headers with the closing `end` / `end <kind>` lines. Declarations are
then matched to the recovered scope that most tightly encloses them
by line span. The EXPRESSION section stays empty inside an unparsed
region (see the `has_error` guard in `_find_expression_root`).

### `dimfort/interactions`

The INTERACTIONS section is populated by a separate request, fired by
the panel alongside `dimfort/panelInfo` and on the same debounce.

```
request:   "dimfort/interactions"
params:    { uri: DocumentUri, position: Position }
response:  InteractionsReport | null
```

```typescript
interface InteractionsReport {
  symbol: string;
  points: InteractionPoint[];
  conflicts: InteractionConflict[];
  hasConflict: boolean;
}

interface InteractionPoint {
  file: string;
  line: number;
  column: number;
  scope: string | null;
  kind: "declares" | "contributes" | "requires" | "uses";
  unit: string;    // rendered unit, or "?" / "-" for absence
  snippet: string;
}

interface InteractionConflict {
  code: string;    // X001
  message: string;
  file: string;
  line: number;
  column: number;
  site: InteractionPoint;
  reference: InteractionPoint;
}
```

Conflicts render first as a 🔴 row, then the points are grouped by
`kind`:

| `kind`        | Group label    |
|---------------|----------------|
| `declares`    | Declaration    |
| `contributes` | Write          |
| `requires`    | Read           |
| `uses`        | Undetermined   |

All four groups are always rendered (with `(none)` when empty) so the
section structure is stable across cursor moves. The Undetermined
group omits the per-row unit cell — the group label already says no
derived unit was determined. See [interaction-points.md](interaction-points.md)
for the analysis semantics.

## Rendering conventions

These are part of the spec — companions match them so the panel
reads identically across editors.

- **Section headers** are uppercase, prefixed with a fold marker.
  Fold state persists per section.
- **Markers** use a coloured-circle vocabulary: 🟢 ok, 🟡 warn / unannotated,
  🔴 error, 🔵 assumed / info.
- **Absence glyphs** `?` (unknown) and `-` (structural-no-unit) are
  rendered dimmed so real units pop visually.
- **`unit` + `unitNormalized`** render as **two side-by-side cells**
  with a two-space gap. No arrow glyph, no separator — column spacing
  alone conveys the second cell. The normalized cell is shown only
  when `unitNormalized` differs from `unit` (so base-SI rows like
  `m → m` stay uncluttered).
- **Click behaviour.** Scope, Imports, Diagnostics, and Interactions
  rows are clickable: clicking jumps the editor's cursor to the
  referenced line (cross-file for imports + interactions). Actions
  rows are buttons that apply the underlying `CodeAction`.

## Update cadence

Companions trigger `dimfort/panelInfo` (and `dimfort/interactions`)
on cursor moves, debounced. Default: **200 ms**
(`panel_debounce_ms`). Companions should:

- Cancel an in-flight request if the cursor moves before the response
  arrives (drop the stale response by sequence number).
- Skip the request entirely if the cursor is on a blank line or
  inside a comment (cheap pre-filter to avoid round-tripping).
- Re-render on visibility change (panel hidden → shown).

The server is stateless w.r.t. these endpoints — they compute from the
last cached `WorksetResult`. No subscription model.

## Rendered example

Live screenshots of the panel on the companion `MANUAL_QA.md` scene
are tracked in the companion repos' READMEs; refer to those rather
than ASCII mock-ups, which drift faster than the panel does.

## Settings

Companion config keys mirror these across editors: Nvim Lua uses
`snake_case`, Emacs uses `dimfort-<key>` (kebab-case), VSCode uses
`dimfort.<key>` (dot-case). The defaults below are the unified UX
stance — panel on, hover at "short", inlay hints off (redundant with
the panel), cache on.

### Panel

| Key (Nvim Lua)         | Type     | Default   | Effect                                        |
|------------------------|----------|-----------|-----------------------------------------------|
| `panel_enabled`        | boolean  | `true`    | Open the panel on attach                      |
| `panel_layout`         | string   | `"both"`  | `"both"` / `"expression"` / `"routine"`       |
| `panel_position`       | string   | `"right"` | `"right"` / `"left"` / `"bottom"`             |
| `panel_width_fraction` | number   | `0.35`    | Fraction of editor width                      |
| `panel_width_cols`     | integer? | `nil`     | Explicit column count — overrides the fraction |
| `panel_debounce_ms`    | integer  | `200`     | Cursor-follow debounce                        |

### Server-side feature toggles

| Key                       | Type    | Default        | Effect                                                  |
|---------------------------|---------|----------------|---------------------------------------------------------|
| `inlay_hints_enabled`     | boolean | `false`        | `[unit]` ghost text at variable uses / calls            |
| `completion_enabled`      | boolean | `true`         | Unit-name completion inside `@unit{}`                   |
| `code_actions_enabled`    | boolean | `true`         | Add-`@unit{}` and extract-to-`PARAMETER` quick fixes    |
| `goto_definition_enabled` | boolean | `true`         | LSP go-to-definition                                    |
| `hover`                   | string  | `"short"`      | `"disabled"` / `"short"` / `"detailed"`                 |
| `scale_mode`              | string  | `"auto"`       | `"auto"` defers to `dimfort.toml`; `"on"`/`"off"` override |
| `cache_mode`              | string  | `"read-write"` | `"off"` / `"read-only"` / `"read-write"`                |

### Hover

| Key (Nvim only) | Type   | Default     | Effect                                                                                |
|-----------------|--------|-------------|---------------------------------------------------------------------------------------|
| `hover_border`  | string | `"rounded"` | Border style for hover floats — `"rounded"`/`"single"`/`"double"`/`"solid"`/`"shadow"`/`"none"` |

### Workspace

| Key                | Type     | Default                    | Effect                                                      |
|--------------------|----------|----------------------------|-------------------------------------------------------------|
| `external_modules` | string[] | `[]`                       | Extra module-export descriptors (e.g. for vendored deps)    |
| `filetypes`        | string[] | `["fortran"]`              | Buffers DimFort attaches to                                 |
| `root_markers`     | string[] | `["dimfort.toml", ".git"]` | Files marking the workspace root                            |
| `auto_attach`      | boolean  | `true`                     | Attach automatically via FileType / BufEnter                |

## Commands

The user-facing commands below use the Nvim names. Emacs binds
equivalents under `M-x dimfort-…`; VSCode under the
`DimFort:` command-palette prefix.

Panel:

- `:DimFortTogglePanel` — open / close.
- `:DimFortPanelLayout {both|expression|routine}` — switch layout.
- `:DimFortPanelRefresh` — force re-request.
- `:DimFortScopeFilter [query]` — filter the Scope section by name / unit
  (no argument clears).
- `:DimFortImportsFilter [query]` — filter the Imports section by name /
  unit / module (no argument clears).

Server / session:

- `:DimFortCheckWorkspace` — run the workspace-wide unit check.
- `:DimFortRestart` — restart the language server.
- `:DimFortStatus` — print current feature toggles and client state.

Feature toggles:

- `:DimFortCycleHover` — cycle hover verbosity (disabled / short / detailed).
- `:DimFortCycleScale` — cycle scale checking (auto / on / off).
- `:DimFortToggleCache` — toggle the content-hash cache between off and read-write.
- `:DimFortToggleInlayHints` — toggle inlay hints.
- `:DimFortToggleCompletion` — toggle unit-name completion.
- `:DimFortToggleCodeActions` — toggle code actions.
- `:DimFortToggleGotoDefinition` — toggle go-to-definition.

## Open questions

1. **Cursor on a `use` clause line** — surface a "what's coming in"
   mini-table for that specific clause, in place of (or alongside) the
   default Imports section? The Imports section already lists every
   use-imported name visible at the cursor, so this would be a
   re-grouping at most.

2. **Cross-file derived-type fields.** When the cursor is on `b%v`
   and `b` is a `type(point)`, the panel doesn't surface the type's
   fields anywhere — go-to-def is the only path. A nested
   `▾ TYPE: point (from <module>)` sub-section, listing fields with
   the same `ScopeVar` shape and click-to-jump cross-file, would
   mirror how Scope already stacks enclosing scopes. The server-side
   field lookup is the same one `dimfort/interactions` already does
   for plain symbols.

   Once types appear in the panel, Scope and Imports start mixing
   categories of thing (variables, procedures, types). A natural
   follow-up is to sub-categorise each section:

   ```
   ▾ SCOPE — push
      Variables
         b           type(point)   🟢
         kinetic     kg·m²·s⁻²     🟢
      Procedures
         helper(m)   s             🟢

   ▾ IMPORTS
      from geom
         Types
            point                  🟢
         Variables
            g0          m·s⁻²      🟢
         Procedures
            norm(m, m)  m          🟢
   ```

   Wire-shape unchanged — the server already tags each row's kind
   (module-procedure rows in Scope; `callable` flag in Imports);
   sub-bucketing is purely renderer-side. Convention: **empty
   buckets disappear within a scope** (a routine with no types
   shouldn't render a "Types: (none)" row), but the **top-level six
   sections stay stable** (still rendered with a `(none)` placeholder
   when empty) so the panel doesn't pop in/out structurally.

3. **Stale marker** when the server is mid-request: dim the panel
   text via a highlight group, un-dim on response? Currently the
   panel just keeps the last content; the 200 ms debounce + fast
   responses mean staleness has not proven visible in practice, and
   dim/un-dim on every keystroke risks being jittery. Revisit only
   if someone reports confusion.

4. **Per-window vs global panel.** Today one global panel per editor
   session, following `activeTextEditor`. A per-window panel would
   let users compare two files' Scope tables side-by-side
   (`:vsplit` in Nvim, split editor groups in VSCode). Tractable in
   Nvim and Emacs — both treat windows as first-class; the panel
   becomes a buffer pinned to a window via window-local state +
   per-window autocmds / hooks.

   **VSCode is the awkward one.** The panel today is a
   `WebviewView` registered for `viewType: "dimfort.panel"` and
   lives in the sidebar container — inherently singleton per
   workbench window, no API to spawn one per editor group. Two
   routes if we go per-window:

   - **Switch to `WebviewPanel`s** (editor-area tabs created with
     `window.createWebviewPanel`). One per editor group, the user
     can drag them between groups / split off / pop to a new window
     like any tab. Renderer is unchanged — same HTML/CSS/script,
     same `webview.postMessage` plumbing, same VSCode theme
     variables; only the container code (lifecycle, focus tracking,
     `WebviewPanelSerializer` for reload-restore) is new. Trade-off:
     panels live in the editor area, so they cost code real estate
     (vs. sidebar pixels that were going to the Explorer anyway),
     and they're closeable like any tab. Mitigation: an
     `editor/title` menu button — a small DimFort icon on each
     editor's title bar that opens a panel pinned to that editor's
     `ViewColumn`, mirroring how Markdown's "Open Preview to the
     Side" button works.

   - **Keep the singleton `WebviewView`, just smarter caching** —
     remember per-(window or editor-group) panel state and re-render
     from cache on focus return instead of re-querying. Not really
     "per-window," but it sharpens the impression of stability.

   Likely path if this lands: ship per-window in Nvim + Emacs;
   accept that VSCode either takes the `WebviewPanel` route (with
   the title-bar-button discoverability fix) or stays singleton.
   Worth documenting the divergence rather than forcing parity.
