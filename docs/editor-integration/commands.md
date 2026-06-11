# Editor commands — cross-companion reference

Single source of truth for the user-facing commands every DimFort companion exposes. Each row is one concept; the three columns are the equivalent command name on each companion.

The intent is **anti-drift**: any future rename in one companion visibly desyncs a row of this table. PR reviewers can check at a glance whether a rename was deliberately one-sided (platform convention) or accidentally one-sided (drift). Each companion repo's `README.md` carries its own per-companion command listing; this doc is the cross-cutting view.

A cell of `*(native UI)*` means the concept is reached via the platform's standard mechanism (e.g. VS Code's right-click → "Hide View") rather than a DimFort-registered command. A cell of `*(auto)*` means the behaviour happens automatically (no user trigger).

## Diagnostics + workspace

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Run workspace check | `DimFort: Check Whole Workspace` (`dimfort.checkWorkspace`) | `:DimFortCheckWorkspace` | `M-x dimfort-check-workspace` |
| Restart server | `DimFort: Restart Language Server` (`dimfort.restartLanguageServer`) | `:DimFortRestart` | `M-x dimfort-restart` |
| Print status | *(native UI: status bar)* | `:DimFortStatus` | `M-x dimfort-status` |

## Cache

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Cycle cache mode (off → read-only → read-write) | `DimFort: Cycle Content-Hash Cache (Off / Read-only / Read-write)` (`dimfort.cycleCache`) | `:DimFortCycleCache` | `M-x dimfort-cycle-cache` |
| Clear disk cache | `DimFort: Clear Content-Hash Cache` (`dimfort.clearCache`) | `:DimFortClearCache` | `M-x dimfort-clear-cache` |

## Feature toggles + cycles

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Toggle inlay hints | `dimfort.toggleInlayHints` | `:DimFortToggleInlayHints` | `M-x dimfort-toggle-inlay-hints` |
| Toggle completion | `dimfort.toggleCompletion` | `:DimFortToggleCompletion` | `M-x dimfort-toggle-completion` |
| Toggle code actions | `dimfort.toggleCodeActions` | `:DimFortToggleCodeActions` | `M-x dimfort-toggle-code-actions` |
| Toggle goto-definition | `dimfort.toggleGotoDefinition` | `:DimFortToggleGotoDefinition` | `M-x dimfort-toggle-goto-definition` |
| Cycle hover verbosity (disabled / short / detailed) | `dimfort.cycleHover` | `:DimFortCycleHover` | `M-x dimfort-cycle-hover` |
| Cycle scale checking (auto / on / off) | `dimfort.cycleScale` | `:DimFortCycleScale` | `M-x dimfort-cycle-scale` |
| Cycle coverage visualisation (disabled / gutter / background) | `dimfort.cycleCoverage` | `:DimFortCycleCoverage` | `M-x dimfort-cycle-coverage` |

## Panel — global

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Open / close panel | `dimfort.togglePanel` | `:DimFortTogglePanel` | `M-x dimfort-toggle-panel` |
| Force panel refresh | *(auto)* | `:DimFortPanelRefresh` | *(auto)* |

## Panel — per-section visibility (0.2.6)

Three independent toggles flipping `dimfort.show.{cursor,scope,imports}` (VS) / `panel_show_{cursor,scope,imports}` (Nvim) / `dimfort-show-{cursor,scope,imports}` (Emacs).

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Toggle Cursor section (Expression / Diagnostics / Interactions / Actions) | `dimfort.toggleCursor` | `:DimFortToggleCursor` | `M-x dimfort-toggle-cursor` |
| Toggle Scope section | `dimfort.toggleScope` | `:DimFortToggleScope` | `M-x dimfort-toggle-scope` |
| Toggle Imports section | `dimfort.toggleImports` | `:DimFortToggleImports` | `M-x dimfort-toggle-imports` |

## Panel — sort + unit display

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Cycle sort mode (line / alphabetic / status) | `dimfort.cycleSortMode` *(plus title-bar icon variants `.alpha`, `.status`)* | `:DimFortCycleSortMode` | `M-x dimfort-cycle-sort-mode` |
| Cycle unit display (canonical / input / both) | `dimfort.cycleUnitDisplay` *(plus title-bar icon variants `.canonical`, `.both`)* | `:DimFortCycleUnitDisplay` | `M-x dimfort-cycle-unit-display` |

## Panel — filters

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Filter Scope section | *(filter input in panel)* | `:DimFortScopeFilter [query]` | `M-x dimfort-scope-filter` |
| Filter Imports section | *(filter input in panel)* | `:DimFortImportsFilter [query]` | `M-x dimfort-imports-filter` |

## Coverage

| Concept | VSCompanion | NvimCompanion | EmacsCompanion |
|---|---|---|---|
| Coverage report buffer | *(status-bar tooltip — no command)* | `:DimFortCoverageReport` | `M-x dimfort-coverage-report` |

## Conventions across companions

- `toggle*` is reserved for 2-state booleans (on/off, shown/hidden).
- `cycle*` is reserved for 3-state-or-more enums (disabled/short/detailed, off/read-only/read-write, …).
- `check*` describes the workspace-wide unit check; coverage refresh is a side effect, not the verb. The verb that ships across all three companions is `check`.
- Per-section visibility (`toggleCursor`/`toggleScope`/`toggleImports`) is settings-backed — flipping persists per VS Code Settings / `setup{}` defaults / `defcustom` (via `customize-set-variable`).

## When to update this table

- Add a row whenever a new user-facing command lands in any companion.
- Drop a row only if **all three** companions remove the concept (e.g. a feature is deprecated upstream).
- If a rename happens, **update all three columns** in the same PR — the table is the contract.
- The per-companion `README.md` command listings should be kept in sync with their column here. The DimFort docs audit (see [pre-release docs audit checklist](../../../HANDOVER.md)) includes a cross-link check between this table and the per-repo READMEs.
