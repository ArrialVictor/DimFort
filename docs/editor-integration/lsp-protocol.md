# Language server

DimFort ships an LSP server built on [pygls](https://github.com/openlawlibrary/pygls).
Start it over stdio with:

```bash
dimfort lsp
```

This page documents the wire-protocol contract that editor companions
build against. The user-facing hover rendering rules live in
[hover-ui.md](hover-ui.md); the side-panel request/response payload
lives in [design/panel-info.md](../design/shipped/panel-info.md).

## What's wired up

### Diagnostics

Published via `textDocument/publishDiagnostics`. Triggers:

| Event | Behaviour |
|---|---|
| `textDocument/didOpen` | Immediate re-check of the opened file. |
| `textDocument/didChange` | Re-check with a **400 ms** debounce; superseded keystrokes drop their pending check, so a fast typist does not queue work. |
| `textDocument/didSave` | Immediate re-check, with the on-disk text reloaded. |
| `textDocument/didClose` | Republishes the last cached diagnostics for the file so closing a tab does not clear squiggles from the Problems panel. |

The server is **workspace-aware**: on `initialize` it captures
workspace folders, builds a Fortran source index, and runs the pipeline
across every file under them. Cross-file diagnostics (`use mod_other`,
H004 on a call into another file) light up in the editor exactly as
they do on the CLI.

### Hover

`textDocument/hover` resolves to a single rendering driven by the
tri-state `hover` initialization option:

| Mode | Render |
|---|---|
| `"disabled"` | Hover returns no content; the side panel becomes the unit surface. |
| `"short"` *(default)* | One-line summary — the root node and its immediate children only. |
| `"detailed"` | Full unit-algebra tree under the cursor, with per-node markers and expected-unit annotations. |

The same mode applies uniformly to every hover surface: use-statement
module names, function / subroutine definition headers, derived-type
member access, call expressions, bare identifiers, numeric literals,
and enclosing-expression contexts. Layout, marker glyphs, and the
conflict-resolution rules are specified in
[hover-ui.md](hover-ui.md).

The legacy `traceHoverEnabled` boolean (pre-tri-state clients) is
still accepted and mapped onto `hover`: `true → "detailed"`, otherwise
`"short"`. Modern clients should set `hover` directly.

### Side panel (`dimfort/panelInfo`)

Custom request returning everything the editor needs to render the
"DimFort" side panel at a given cursor position: enclosing scopes
with their typed variables, current imports with their resolved units,
the expression tree under the cursor, line-scoped diagnostics, and
file-wide H/U counts. The user-facing description of what the panel
shows is in [editor-integration/side-panel.md](side-panel.md).

Method: `dimfort/panelInfo`

Request:

```jsonc
{
  "textDocument": { "uri": "file:///…/foo.f90" },
  "position":     { "line": 0, "character": 0 }   // both 0-based
}
```

Response (top-level shape — full field reference in
[design/panel-info.md](../design/shipped/panel-info.md)):

```jsonc
{
  "expression":            { /* AST under cursor, may be null */ },
  "scopes":                [ /* outermost-first; each carries vars */ ],
  "imports":               [ /* use-clause symbols active at cursor */ ],
  "diagnostics":           [ /* diagnostics whose range covers this line */ ],
  "fileDiagnosticCounts":  { "error": 0, "warning": 0 }
}
```

### Cross-site analysis (`dimfort/interactions`)

Custom request answering "for the symbol under the cursor, what does
every site that touches it imply about its unit, and do those
implications agree?". Used by editor panels to surface
over-constrained variables that no single statement reveals.

Method: `dimfort/interactions`

Request:

```jsonc
{
  "textDocument": { "uri": "file:///…/foo.f90" },
  "position":     { "line": 0, "character": 0 },
  "symbol":       "<optional explicit symbol name>",
  "scale":        false
}
```

Response:

```jsonc
{
  "symbol":      "u_zonal",
  "points":      [ { "file": "…", "line": 12, "kind": "read", "unit": "m/s", "snippet": "…" } ],
  "conflicts":   [ { "code": "X001", "message": "…", "file": "…", "line": 12, "site": {…}, "reference": {…} } ],
  "hasConflict": false
}
```

Spec: [design/interaction-points.md](../design/shipped/interaction-points.md).

### Per-line coverage tiers (`dimfort/lineStatus`)

Custom request returning a per-line coverage tier for one file
— used by editor companions to paint a gutter / background
decoration showing which lines DimFort verified, which need
attention, which fired a hard error, and which sit in an
unparsed region.

Method: `dimfort/lineStatus`

Request:

```jsonc
{ "uri": "file:///…/foo.f90" }
```

Response:

```jsonc
{
  "uri":   "file:///…/foo.f90",
  "lines": [
    { "line": 12, "status": "green"  },
    { "line": 13, "status": "yellow" },
    { "line": 14, "status": "red"    }
  ]
}
```

Lines omitted from the response are out-of-scope (no
decoration). Status values: `green` (verified-OK), `yellow`
(needs attention — `U005`, `H010`, `S001`, `S002`, or
propagation), `red` (hard fire — `H001`-`H004`, `H020`-`H023`,
`S003`, `U002`), `blue` (`P001` unparsed region).

### Coverage stats (`dimfort/coverageStats`)

Custom request returning per-file or workspace-wide coverage
aggregates: tier counts plus a percentage. Used by editor
panels to surface a stats bar / report buffer alongside the
per-line decoration.

Method: `dimfort/coverageStats`

Request (file scope):

```jsonc
{ "uri": "file:///…/foo.f90" }
```

Request (workspace scope — aggregate over every file in the
workspace index):

```jsonc
{}
```

Optional `force_refresh: true` on workspace-scope bypasses the
server-side idle debounce; used by companions exposing an
explicit on-demand refresh.

Response:

```jsonc
{
  "scope": "file",                        // or "workspace"
  "uri":   "file:///…/foo.f90",           // present when scope=file
  "files": [
    { "uri": "file:///…/foo.f90",
      "ok": 164, "warn": 3, "fire": 0,
      "unparsed": 0, "out": 78,
      "coverage_pct": 98.2 }
  ],
  "total": { "ok": 164, "warn": 3, "fire": 0,
             "unparsed": 0, "out": 78,
             "coverage_pct": 98.2 },
  "ws_stale": false                       // workspace scope only
}
```

`coverage_pct` is `ok / (ok + warn + fire) * 100` —
unparsed and out-of-scope lines are excluded from the
denominator. `ws_stale` (workspace scope only) is `true` when
the cached aggregate is out of date or a background refresh is
in flight; companions render the bar segment muted while stale.

Workspace-scope checks run on a daemon thread inside the
server with a built-in idle debounce; calling this method
during active editing does not block the request thread and is
not a synchronous re-check.

Spec: [design/shipped/coverage-visualization.md](../design/shipped/coverage-visualization.md).

### Inlay hints, definition, code actions, completion

- **Inlay hints** — `[unit]` ghost text at variable uses, calls, and
  member accesses. Toggle: `inlayHintsEnabled`.
- **Go-to-definition** — resolves identifiers and call-callees to
  their declaration sites, cross-file. Toggle: `gotoDefinitionEnabled`.
- **Code actions** — insert `@unit{}` skeletons on undeclared
  variables; extract H010-D1.5 numeric literals to a typed
  `PARAMETER`; apply the U002 "did you mean …?" rewrite when the
  diagnostic carries a `suggested_rewrite` payload. Toggle:
  `codeActionsEnabled`.
- **Completion** — unit-name completion inside `@unit{…}` directives.
  Toggle: `completionEnabled`.

### Workspace command

`workspace/executeCommand` with command `dimfort/checkWorkspace`
re-runs the full workspace check on demand (per-file checks are
otherwise event-driven). On completion the server emits a toast with
the file count, H/U totals, wall-clock time, and cache hit / miss /
dirty stats when the cache is active.

## `initializationOptions` reference

Every key is optional. Unset keys fall back to the default below or to
the matching `.dimfort.toml` setting where one exists.

| Key | Type | Default | Effect |
|---|---|---|---|
| `hover` | `"disabled"` \| `"short"` \| `"detailed"` | `"short"` | Tri-state hover verbosity. See [hover-ui.md](hover-ui.md). |
| `inlayHintsEnabled` | boolean | `true` | Toggle inlay hints. |
| `completionEnabled` | boolean | `true` | Toggle unit-name completion inside `@unit{…}`. |
| `codeActionsEnabled` | boolean | `true` | Toggle code actions. |
| `gotoDefinitionEnabled` | boolean | `true` | Toggle go-to-definition. |
| `scaleMode` | boolean | from `.dimfort.toml` (`false` if unset) | Opt in to multiplicative-scale checking (S001 / S002 / S003) and the scale-aware unit display. |
| `maxWorksetSize` | integer | `40` | Cap the per-check workset; on large workspaces, files are pinned to the active file's direct dependencies and topo-last entries are dropped to keep the LSP responsive. |
| `externalModules` | string[] | (merges with config) | Extend the known-external module list (intrinsics + common libraries) beyond `.dimfort.toml`. Lowercased. |
| `cacheMode` | `"off"` \| `"read-only"` \| `"read-write"` | `"off"` | Content-hash cache mode. See [usage.md § Content-hash cache](../usage.md#content-hash-cache). |
| `cacheDir` | string (absolute path) | `.dimfort-cache/` under the first workspace folder | Override the cache directory when `cacheMode` is not `"off"`. |
| `traceHoverEnabled` | boolean | — | **Legacy.** `true` → `hover = "detailed"`. Ignored if `hover` is set. |

Example:

```jsonc
{
  "hover":         "detailed",
  "scaleMode":     true,
  "cacheMode":     "read-write",
  "maxWorksetSize": 80
}
```

## Limitations

- **In-memory edits to file A trigger a check of every file in its
  workset.** The pipeline is fast on small projects; large worksets
  are capped at `maxWorksetSize` so a deep entry point in a large
  codebase stays responsive.
- **`.F90` preprocessing** runs the system `cpp` (one subprocess per
  file). A warm content-hash cache (`cacheMode: "read-write"`) skips
  the check phase on unchanged files and is the recommended setting
  on large workspaces.

## Editor setup

### VSCode

Use the [DimFort-VSCompanion](https://github.com/ArrialVictor/DimFort-VSCompanion)
extension from the VS Code Marketplace
(`arrialvictor.dimfort-vscode`) or the Open VSX Registry
(`dimfort.dimfort-vscode`). Set the `dimfort.executable` setting to
your DimFort install (typically the `dimfort` binary inside a
virtualenv or pipx environment).

### Neovim (built-in LSP)

```lua
vim.lsp.config.dimfort = {
  cmd = { "dimfort", "lsp" },
  filetypes = { "fortran" },
  init_options = {
    hover     = "short",
    cacheMode = "read-write",
  },
}
vim.lsp.enable("dimfort")
```

The [DimFort-NvimCompanion](https://github.com/ArrialVictor/DimFort-NvimCompanion)
plugin adds the side panel, palette commands, and inlay-hint styling
on top of this baseline.

### Helix

In `~/.config/helix/languages.toml`:

```toml
[language-server.dimfort]
command = "dimfort"
args    = ["lsp"]

[language-server.dimfort.config]
hover     = "short"
cacheMode = "read-write"

[[language]]
name             = "fortran"
language-servers = ["dimfort"]
```

### Emacs (lsp-mode)

```elisp
(with-eval-after-load 'lsp-mode
  (add-to-list 'lsp-language-id-configuration '(f90-mode . "fortran"))
  (lsp-register-client
   (make-lsp-client :new-connection (lsp-stdio-connection '("dimfort" "lsp"))
                    :activation-fn (lsp-activate-on "fortran")
                    :server-id 'dimfort)))
```

The [DimFort-EmacsCompanion](https://github.com/ArrialVictor/DimFort-EmacsCompanion)
package adds the side panel and palette commands on top.
