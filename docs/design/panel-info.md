# Side-panel info endpoint — design notes

Status: **draft / no implementation yet**. Doc-first per the
algebra-extension precedent. Branches:
- `DimFort`            → `panel-info-endpoint`
- `DimFort-NvimCompanion` → `split-view-prototype`

This document is a working spec to settle the data model and the
panel layout before any code lands. The features that survive a
two-session usage trial in Neovim graduate to Emacs and VSCode; the
ones that don't, get cut.


## Motivation

The Detailed-hover layout already renders a full unit-algebra tree
for the expression under the cursor, but it dismisses the moment the
cursor moves. Two workflows want a *persistent* view:

1. **Code archaeology** — opening an annotated codebase you didn't
   write (LMDZ-class) and surveying which variables carry which
   units. A flat per-routine table is much faster than `K`-hovering
   every declaration.
2. **Pair / talk-through** — two people on one screen reading
   annotations together. A side panel makes the unit-of-everything
   visible without choreographing key presses.

Both want **"open it to explore, close it when editing"** rather than
always-on. Default off; toggleable; persistent across cursor moves;
debounced cursor-follow updates.


## Scope

In:

- Two stacked sections in a single side panel:
  1. **Expression** — the unit-algebra tree for the expression
     under the cursor (same content as Detailed hover).
  2. **Scope variables** — the declarations of every enclosing scope
     (subroutine / function / module / program), stacked outermost
     first, each with its unit (or `unannotated` marker). A cursor in
     a module-contained subroutine shows the module's declarations
     *and* the subroutine's locals as separate sections.
- Nvim-first prototype. Emacs port second. VSCode last.
- Settings to toggle visibility and layout (both / expression-only
  / routine-only).

Out (for v1, revisit later):

- Cross-file workspace-wide views (e.g. all modules' constants).
- Editing from the panel (e.g. click to add `@unit{}`).
- Per-row diagnostics navigation (clicking a row jumps to the line).
- Sort / filter / search controls.

Keep v1 read-only and information-dense. Polish only if usage proves
the panel earns its screen real estate.


## LSP endpoint

A single custom request:

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

  // The full chain of enclosing scopes, OUTERMOST first
  // (e.g. [module, subroutine] for a cursor inside a module-contained
  // subroutine). Each carries its declarations. Empty when the cursor
  // is at bare file level. The panel stacks one section per entry.
  scopes: ScopeSection[];

  // Innermost scope, surfaced for single-section consumers. Identical
  // to scopes[scopes.length - 1] (or null when scopes is empty). The
  // routine / routineVars fields are further back-compat aliases.
  scope: { name: string;
           kind: "subroutine" | "function" | "module" | "program" } | null;
  scopeVars: ScopeVar[];
  routine: { name: string; kind: string } | null;
  routineVars: ScopeVar[];
}

interface ScopeSection {
  name: string;
  kind: "subroutine" | "function" | "module" | "program";
  vars: ScopeVar[];
}

interface ExpressionNode {
  // Human-readable label (the source slice, lightly normalised).
  label: string;
  // Resolved unit string, or null if unresolved / not applicable
  // (e.g. an assignment statement, which has no unit of its own —
  // renderers omit the unit column for such nodes).
  unit: string | null;
  // 🟢 ok, 🟡 warning/unresolved, 🔴 mismatch.
  marker: "ok" | "warn" | "error";
  // Rule ID that produced this node's unit (e.g. "R5.6"), if any.
  ruleId: string | null;
  // Sub-expressions whose units feed into this one.
  children: ExpressionNode[];
}

interface ScopeVar {
  name: string;
  // The annotated unit text as written, or null for unannotated
  // declarations. For kind "error" this is the raw (unparseable) text.
  unit: string | null;
  // The base-SI normalized form (factor included), e.g. "hPa" →
  // "100×kg/(m×s²)", so scale factors and derived-unit expansions are
  // visible. Equals `unit` for base-SI annotations; null when the
  // annotation doesn't parse or is absent. Renderers show
  // `unit = unitNormalized` only when the two differ.
  unitNormalized: string | null;
  // 1-based line number of the declaration.
  line: number;
  // 🟢 annotated (valid unit), 🟡 unannotated (no @unit{}),
  // 🔴 error (has @unit{} but it failed to parse — the U002 set).
  kind: "annotated" | "unannotated" | "error";
}
```

The enclosing scope is the innermost ``subroutine`` / ``function`` /
``module`` / ``program`` node. For routine scopes the declarations are
matched by ``DeclarationSite.scope`` (the routine name); for module /
program scopes, top-level declarations (``scope is None``) are matched
by line span so nested routines' locals are excluded.

The `marker` field on `ExpressionNode` carries the worst-of-children
aggregation already done by the Detailed-hover renderer. Clients
don't need to re-derive it.

Both fields (`expression`, `routineVars`) are optional in the response
— the server returns `null` for whichever doesn't apply. Clients hide
the corresponding section.


## Update cadence

The client triggers `dimfort/panelInfo` on cursor moves, debounced.
Recommended debounce: **200 ms**. Implementations should:

- Cancel an in-flight request if the cursor moves before the response
  arrives.
- Skip the request entirely if the cursor is on a blank line or
  inside a comment (cheap pre-filter to avoid round-tripping for nothing).

The server is stateless w.r.t. this endpoint — it computes from the
last cached `WorksetResult`. No subscription model in v1.


## Panel layout

ASCII mock-up — the panel sits as a vertical split on the right
(Nvim default; configurable).

```
┌─ driver.f90 ────────────────────────┬─ DimFort panel ─────────────┐
│  1  subroutine driver               │ Expression                  │
│  2    use constants_mod, only: ...  │                             │
│  3    use physics_mod,   only: ...  │   bogus = c_sound * t       │
│  4                                  │   ├─ c_sound : m/s       🟢 │
│  5    real :: t          !< @unit{s}│   ├─ t       : s         🟢 │
│  6    real :: d          !< @unit{m}│   ├─ * (R1.1): m         🟢 │
│  7    real :: v          !< @unit{m │   └─ ◂ kg ≠ m            🔴 │
│  8    real :: bogus      !< @unit{kg│                          H001│
│  9    real :: t_celsius             │                             │
│ 10                                  │ Routine: driver             │
│ 11    t = 2.0                       │  line  name        unit     │
│ 12    d = fall_distance(t)          │     5  t           s        │
│ 13    d = sound_travel(t)           │     6  d           m        │
│ 14    v = c_sound + 5.0             │     7  v           m/s      │
│ 15                                  │     8  bogus       kg       │
│ 16    bogus = c_sound * t           │     9  t_celsius   (none) 🟡│
│ 17    t_celsius = t - 273.15        │                             │
│ 18  end subroutine                  │                             │
└─────────────────────────────────────┴─────────────────────────────┘
```

Highlights:

- Cursor on `bogus = c_sound * t` (line 16) → expression section
  shows the tree with markers; routine section lists every
  declaration in `driver`.
- Cursor on a declaration line → expression section is empty
  (header still visible, body shows "no expression at cursor");
  routine section unchanged.
- Cursor on a blank line / comment → both sections show the
  last cached content (we don't blank the panel on every
  whitespace cursor move).

Sizing:

- Default width: 35% of editor column count, clamped to [40, 80] cols.
- Configurable via setting; user can `:vertical resize N` to override
  in Nvim.

### Rendered example

The real panel (Neovim, the reference renderer) on the `qa.f90` scene
from the companion `MANUAL_QA.md`. Cursor on the `=` in
`q = 0.5 * rho * v * v` — a deep, all-🟢 multiplication tree over the
stacked `Module` / `Function` scope:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../img/panel-nvim-hero_dark.png">
  <img width="640" src="../img/panel-nvim-hero_light.png" alt="DimFort side panel — unit-algebra tree for q = 0.5 * rho * v * v with the stacked module/function scope">
</picture>

Cursor on the `=` in `bogus = c_sound * t` — a `kg ≠ m` mismatch, the
assignment root marked 🔴:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../img/panel-nvim-mismatch_dark.png">
  <img width="640" src="../img/panel-nvim-mismatch_light.png" alt="DimFort side panel — a kg ≠ m homogeneity violation, the assignment root marked red">
</picture>


## Settings (per editor)

| Key (Nvim Lua)            | Type     | Default   | Effect                                |
|---------------------------|----------|-----------|---------------------------------------|
| `panel_enabled`           | boolean  | `false`   | Open the panel on attach              |
| `panel_layout`            | string   | `"both"`  | `"both"` / `"expression"` / `"routine"` |
| `panel_position`          | string   | `"right"` | `"right"` / `"left"` / `"bottom"`     |
| `panel_width_fraction`    | number   | `0.35`    | Fraction of editor width              |
| `panel_debounce_ms`       | number   | `200`     | Cursor-follow debounce                |

Each editor mirrors these under its native config namespace
(`dimfort.panel.*` in VSCode, `dimfort-panel-*` in Emacs).

Commands (Nvim):

- `:DimFortTogglePanel` — open / close.
- `:DimFortPanelLayout {both|expression|routine}` — switch layout.
- `:DimFortPanelRefresh` — force re-request, useful for debugging.


## Implementation plan

Branches stay independent until both are wired:

### Server (`panel-info-endpoint`)

1. Add `dimfort/panelInfo` request handler in `lsp/server.py`.
2. Reuse existing infrastructure:
   - `_trees_for(uri)` to get the parsed tree.
   - `_smallest_enclosing_routine(...)` to find the routine context.
   - The existing trace-collecting code path that builds the
     Detailed-hover tree — refactor to also return a structured form,
     not just the rendered text.
   - `_last_result.attachments[path].var_units` for the routine vars
     list; merge with `_last_scan_declarations(path)` to include
     unannotated declarations.
3. Unit tests: deterministic input file → expected `PanelInfo`
   payload. Cover (a) cursor on expression, (b) cursor on
   declaration, (c) cursor in comment, (d) cursor in module
   (no enclosing routine).

### Nvim client (`split-view-prototype`)

1. New module `lua/dimfort/panel.lua`:
   - `M.open(opts)` — create / show the panel window + buffer pair.
   - `M.close()` — close window, keep buffers.
   - `M.toggle()` — bind to `:DimFortTogglePanel`.
   - `M.refresh()` — fire a `dimfort/panelInfo` request, render the
     response.
2. Cursor-follow autocmd group `DimFortPanel`:
   - `CursorMoved` / `CursorMovedI` → debounced refresh.
   - `BufLeave` of source buffer → blank the panel (preserve last
     content but mark stale).
3. Render functions in pure Lua, no external deps:
   - `render_expression(node, indent)` — recursive, produces lines.
   - `render_routine_vars(vars, header)` — table layout.
   - Use `nvim_buf_set_extmark` for the 🟢 / 🟡 / 🔴 markers (avoid
     baking them into the text so colorschemes can re-style).


## Open questions

1. **What to show when the cursor is on a USE clause line?** The
   imported names could form a "what's coming in" mini-table. Defer.
2. **Module-level cursor**: routine section becomes "Module: NAME";
   list is the module's exported decls. Trivial extension.
3. **Cross-file derived-type fields**: when the cursor is on `b%v`
   and `b` is a `type(point)`, do we show the type's field table?
   Probably yes, as a second routine-vars-style section. Defer to v2.
4. **What's the "stale" marker on the panel content** when the server
   is mid-request? Recommended: dim the panel text via a highlight
   group; un-dim on response. Skip if it looks jittery in practice.
5. **Should the panel be per-window or global?** v1: global, one
   panel for the whole Nvim session. v2: optional per-window if users
   actually open multiple Fortran files side-by-side.


## Two-session graduation test

Per the working-style note: build the prototype, use it during the
real LMDZ annotation cycle for two sessions, then decide. The
specific signals to look for:

- Pro: I open the panel during code archaeology and keep it open.
- Pro: I find unannotated variables I would have missed.
- Con: I open it once, ignore it, close it.
- Con: It eats screen real estate I'd rather have for the code.
- Con: The expression-tree section is busy / hard to read for non-trivial expressions.

Convert these signals into a concrete next step:

| Signal | Action |
|---|---|
| Both sections heavily used | Port to Emacs, design VSCode webview |
| Only routine section used | Cut expression section, simplify panel |
| Only expression section used | Cut routine section, simplify panel |
| Neither used | Delete branches, keep design doc for future reference |
