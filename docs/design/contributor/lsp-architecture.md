# Language server — internal architecture

How the `dimfort lsp` server is organised internally. For the *user-facing*
feature list (what hover/inlay/diagnostics do) see [`docs/lsp.md`](../../editor-integration/lsp-protocol.md);
this document is for people editing the server itself.

The server speaks LSP over stdio (via [pygls](https://github.com/openlawlibrary/pygls))
and lives under `src/dimfort/lsp/`. It started as one ~3900-line `server.py` and
was split into focused modules around a single `LanguageServer` instance;
`server.py` is now ~1230 lines of lifecycle + publish + feature registration.

## Module map

### The spine — `server.py`

Owns the pygls `LanguageServer` instance and every `@server.feature(...)`
registration, plus the parts that are inherently central:

- **Lifecycle**: `initialize` / `initialized` (workspace-folder capture, config
  load, background index build) and the document-sync handlers (`did_open`,
  `did_save`, `did_close`, `did_change`, with the debounce).
- **Diagnostic publish side**: `_publish_for_uri`, `_ensure_uri_loaded`,
  `_refresh_inlay_hints` — the write path that runs the pipeline and pushes
  `publishDiagnostics`.
- **Feature toggles**: `_FeatureToggles` / `_features` (set from
  `initializationOptions`).
- **Misc**: `_notify`, the `dimfort.checkWorkspace` command, `run_stdio`.

Each `@server.feature` handler in `server.py` is a **thin wrapper**: it does the
feature-flag check, calls `_ensure_uri_loaded` if needed, acquires the
tree-sitter lock if it traverses the cached tree, then delegates to a logic
function in a feature module.

### Shared layers

| Module | Owns |
| --- | --- |
| `state.py` | The `state` singleton: every lock + mutable global (last check result, workspace index, debounce versions, open-URI map, project config, cache, scale mode). The concurrency contract is documented at the top of this file. |
| `tree_access.py` | URI↔path conversion (`_uri_to_path`, `_uri_for_path`) and workset lookups (`_trees_for` → cached parsed tree; `_build_ts_ctx` → a `ts_checker._Ctx` pre-loaded with the workset's unit tables). |
| `tree_nav.py` | Pure tree-sitter navigation / node inspection: the identifier / enclosing-scope / expression-root under a cursor, node→LSP-range mapping, one-line node previews. No state, no checker ctx. |
| `decl_scan.py` | Source-side declaration scanning — from the live buffer (`_scan_declarations_for_uri`) or disk (`_last_scan_declarations`). |
| `expr_tree.py` | The diagnostic-driven **marker model** (`_self_marker` / `_node_marker` / `_diags_for_ctx`, per [`markers.md`](../shipped/markers.md)) and the panel builders (`_build_expression_tree`, `_build_scope_vars`). Shared by both the panel and the hover surfaces. |
| `hover_render.py` | Pure markdown rendering for hover (unit pretty-printing, single-symbol / signature / module summaries). |
| `markers.py` | 🟢/🟡/🔴 marker token mapping + worst-of aggregation. |
| `ts_helpers.py` | Parser-shape-specific tree-sitter queries (walk calls / identifiers / use-statements / definitions, etc.). |

### Feature handlers (one module each)

Registered in `server.py`, logic delegated here:

| Module | Feature |
| --- | --- |
| `completion.py` | `textDocument/completion` inside `@unit{…}`. |
| `definition.py` | `textDocument/definition` (go-to-declaration, cross-file). |
| `inlay.py` | `textDocument/inlayHint` (`[unit]` ghost text). |
| `interactions.py` | `dimfort/interactions` (cross-site unit analysis + X001 conflicts). |
| `code_action.py` | `textDocument/codeAction` — Add `@unit{}`, Extract literal to PARAMETER. |
| `panel.py` | `dimfort/panelInfo` (cursor-following side-panel payload). |
| `hover.py` | `textDocument/hover` — unit resolution + the short/detailed renderers, call hover (root row `name(args) : ret` + per-actual-argument children, `(expected …)` on mismatch; shares the side panel's expression-tree renderer), pure-signature hover on definition headers, and the unit-algebra trace tree. The `_hover` wrapper reads the verbosity toggle and threads it in as `hover_mode` (the module never imports `server`). |

## Three load-bearing patterns

1. **Singleton state.** All shared mutable state lives on one `state` object in
   `state.py`. Read/write it as attributes (`state.last_result = x`,
   `with state.check_lock:`) — there is no module-level `global`, and because
   every module imports the *same* instance there is no stale-reference hazard.
   Import: `from dimfort.lsp.state import state`.

2. **Handler delegation.** A feature handler's `@server.feature` wrapper stays in
   `server.py`; its logic moves to a feature module that exposes a plain function
   (e.g. `definition.resolve(params)`). Feature modules import only `lsprotocol`,
   `dimfort.core.*`, and the shared layers above — **never `server`** — so there
   is no import cycle.

3. **Lock discipline (`state.ts_handler_lock`).** The tree-sitter C bindings are
   not thread-safe for concurrent traversal of one tree, and pygls runs sync
   handlers on a worker pool (a Cmd-hover fires hover + definition together). So:
   - Handlers that traverse the **cached shared** tree — **hover, definition,
     inlay** — MUST hold `state.ts_handler_lock` (acquired in their `server.py`
     wrapper).
   - Handlers that parse a **fresh** tree per request — **interactions, panel** —
     and **code-action** do **not** hold it. Preserve this when editing them;
     don't add a lock.
   It is one shared lock; any module importing `state` gets the same object. A
   locking regression is a silent native crash with no Python traceback, so
   **pytest cannot catch it** — manually smoke-test lock-touching handlers in an
   editor (fire hover + definition together).

## Dependency direction

```
server.py ──▶ feature handlers ──▶ shared layers ──▶ dimfort.core.*
    │                                   ▲
    └───────────────────────────────────┘  (server.py also uses shared layers)
```

No module under `lsp/` imports `server`. Shared layers may import each other in
one direction only (e.g. `expr_tree` → `tree_nav`/`markers`/`state`;
`tree_access` → `state`).

## Testing note

The test suite imports internals from the module that actually defines them
(e.g. `from dimfort.lsp.tree_nav import _identifier_at`,
`from dimfort.lsp.hover import _resolve_hover`). The earlier transitional
re-export layer in `server.py` (which re-imported moved symbols so old test
imports kept working) has been removed; only symbols `server.py` genuinely owns
(`_initialize`, `_to_lsp_diagnostic`, `_cap_workset`, `_features`, the feature
handlers, `state`) are imported from it.

## Status

The split is complete (branch `refactor-lsp-split`). `server.py` is the spine —
lifecycle, diagnostic publish, feature registration, and the `dimfort.checkWorkspace`
command — and every feature's logic lives in its own module.
