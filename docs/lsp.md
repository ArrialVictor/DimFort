# Language server

DimFort ships an LSP server built on [pygls](https://github.com/openlawlibrary/pygls).
Start it with:

```bash
dimfort lsp
```

It speaks LSP over stdio, the wire format every common editor expects.

## What's wired up

- **Diagnostics**:
  - On `textDocument/didOpen` and `didSave` — immediate re-check.
  - On `textDocument/didChange` — re-check with a 400 ms debounce, so
    unsaved buffer edits flow through the pipeline as you type.
  - On `textDocument/didClose` — the file's diagnostics from the most
    recent workspace check are republished, so closing a file doesn't
    silently clear its squiggles from the Problems panel.
- **Workspace-aware**. The server captures workspace folders on
  `initialize` and runs the pipeline over **every** Fortran source it
  finds under them. Cross-file behaviour (`use mod_other`, H004 on a
  call to a function defined in another file) lights up correctly in
  the editor exactly as it does on the command line. A `DimFort: Check
  Whole Workspace` command on the command palette re-runs the full
  workspace check on demand.
- **Hover** (`textDocument/hover`). Point at a variable name (either
  its declaration or a use site) and the editor shows `**🟢 DimFort**`
  with `**name** : <unit>` below it (or `**🟡 DimFort**` and "no unit
  annotation" if the variable was declared without one). Derived-type
  member accesses (`b%v`) produce `**particle%v** : m/s`. Hovering on
  a Fortran intrinsic callee (`exp`, `log`, `sqrt`, `sin`, `sum`, …)
  shows the call's resolved unit and the full source text of the
  call.
- **Full-unit-trace hover** (opt-in). Set `traceHoverEnabled: true`
  in `initializationOptions` (in VSCode: run `DimFort: Toggle Full
  Unit Trace in Hover`). With it on, any hover inside an assignment
  replaces the compact view with an ASCII tree of the RHS — each
  node carries its resolved unit and the unit-algebra rule that
  produced it (`R3.1`, `R5.6`, …). The header reads
  `🟢 / 🔴 / 🟡 DimFort` for OK / mismatch / unresolved respectively.
- **Inlay hints**, **go-to-definition**, **code lens**, **code actions**
  (insert `!< @unit{}` skeletons; extract H010-D1.5 literals to a
  named PARAMETER), and **unit-name completion** are all live; each
  is toggleable through its respective `DimFort: Toggle …` palette
  command or VSCode setting.

## Limitations

- **In-memory edits to file A trigger a check of every file** in its
  workset. The pipeline is fast on small projects; large worksets are
  capped at `maxWorksetSize` files (default 40, configurable via
  `initializationOptions`) so a deep LMDZ-scale entry point stays
  responsive.
- **`.F90` preprocessing** uses the system `cpp` (one subprocess per
  file). On a 2400-file workspace this dominates wall time — the
  workspace check takes ~80s vs ~7s for pure parse. Tracked as a perf
  task.

## Editor setup

### VSCode

Use the companion extension at [DimFort-VSCompanion](https://github.com/ArrialVictor/DimFort-VSCompanion).
The README in that folder walks through the F5 dev-host workflow.
Point the `dimfort.executable` setting at your DimFort install
(typically a virtualenv).

### Neovim (built-in LSP)

```lua
vim.lsp.config.dimfort = {
  cmd = { "dimfort", "lsp" },
  filetypes = { "fortran" },
}
vim.lsp.enable("dimfort")
```

### Helix

In `~/.config/helix/languages.toml`:

```toml
[language-server.dimfort]
command = "dimfort"
args = ["lsp"]

[[language]]
name = "fortran"
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
