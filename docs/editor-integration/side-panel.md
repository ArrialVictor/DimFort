# The DimFort side panel

The three editor companions — VSCode, Neovim, Emacs — render the same
cursor-following side panel. This page is the canonical description
of what it shows and how to read it; each companion README documents
its own toggle commands, settings keys, and dock-side / width
controls.

The wire-format spec behind the panel (`dimfort/panelInfo` request +
response) is in [design/panel-info.md](../design/shipped/panel-info.md).

## What's in the panel

Six stacked sections, top to bottom. Section ordering and per-section
content are identical across editors.

### 1. Expression

The unit-algebra tree for the expression under the cursor: each node
labelled with its resolved unit, the unit-algebra rule that produced
it (`R3.1`, `R5.6`, …), and a 🟢 / 🟡 / 🔴 marker.

The same content as the **detailed** hover, but it stays visible
while you edit — useful for debugging a mismatch or walking through
code with someone.

### 2. Diagnostics

DimFort diagnostics whose range covers the cursor's line. Each row
carries a 🔴 / 🟡 / 🔵 severity circle (error / warning / info), the
code, and the message. Click or press the editor's "follow" key on a
row to jump to its anchor.

### 3. Interactions

Cross-site unit analysis for the symbol under the cursor — the same
result as the `dimfort interactions` CLI query. Layout:

- the `X001` conflicting-claims finding, if any;
- then four groups — **Declaration**, **Write**, **Read**,
  **Undetermined** — each row showing the site's location, the unit
  it implies, and a one-line source snippet.

Rows navigate cross-file.

### 4. Actions

The code actions available at the cursor (e.g. insert an `@unit{}`
skeleton on an undeclared variable, extract an `H010`-`D1.5` numeric
literal to a typed `PARAMETER`, apply a `U002` "did you mean …?"
rewrite). Activating a row applies the action through the LSP.

### 5. Scope

Every variable declared in any *enclosing* scope, stacked
outermost-first and indented by nesting (a module's declarations,
then a contained subroutine's locals). Per-variable marker:

- 🟢 annotated and parseable,
- 🟡 unannotated (used somewhere in a unit-relevant position),
- 🔴 annotation present but unparseable (`U002`).

Annotation gaps stand out at a glance. Supports a name / unit filter
— invocation is editor-specific.

### 6. Imports

Every symbol a `use` clause brings into the current scope, grouped by
source module under a `from <module>` header. Variables render as
`name : unit`; functions render as `name(argunits)` so the call
signature is visible inline (e.g. `force(kg)`).

Rows navigate cross-file to where the imported symbol — and its
`@unit{}` — is declared. Supports a name / unit / module filter —
invocation is editor-specific.

## Footer

The footer pins the file-wide H- and U-diagnostic counts so the
panel doubles as a per-file health indicator.

## Marker vocabulary

The 🟢 / 🟡 / 🔴 / 🔵 circles appear in every section and follow the
single source of truth shared with the hover and the Problems panel.
The derivation rules live in
[design/markers.md](../design/shipped/markers.md); the per-surface
presentation rules live in [hover-ui.md](hover-ui.md).

Quick gloss:

- 🟢 OK / annotated / derivable.
- 🟡 unannotated, or implicit cast accepted with a warning (`H010`).
- 🔴 dimensional error.
- 🔵 informational — `P001` unparsed regions, `U020` `@unit_assume`
  audit notes, severity-remapped codes set to `info`.

## Per-editor controls

| | VSCode | Neovim | Emacs |
|---|---|---|---|
| **Toggle** | `DimFort: Show Side Panel` (palette) | `:DimFortTogglePanel` | `M-x dimfort-panel-toggle` |
| **On / off setting** | `dimfort.panel.enabled` | `panel_enabled` | `dimfort-panel-enabled` |
| **Scope filter** | filter box in section | `:DimFortScopeFilter <query>` | `M-x dimfort-scope-filter` |
| **Imports filter** | filter box in section | `:DimFortImportsFilter <query>` | `M-x dimfort-imports-filter` |

Each companion README documents the full set of editor-specific
commands and settings.
