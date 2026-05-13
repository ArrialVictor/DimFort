# Language server

DimFort ships an LSP server built on [pygls](https://github.com/openlawlibrary/pygls).
Start it with:

```bash
dimfort lsp
```

It speaks LSP over stdio, the wire format every common editor expects.

## What's wired up

- **Diagnostics** on `textDocument/didOpen` and `textDocument/didSave`.
  The full pipeline runs (scan → attach → check); results are published
  as LSP `Diagnostic`s with our familiar codes (H001–H004, U001–U010,
  U002).
- Diagnostics are cleared on `textDocument/didClose`.

## What isn't yet

- **No `didChange` reactivity.** The server re-checks on save only.
  Cheap to add when we want it.
- **No hover.** Showing the resolved unit of a variable or expression
  is the next obvious feature.
- **No workspace-wide scan.** Each open file is treated as a one-file
  workset. Cross-file H004 / module-resolved access does not yet flow
  through the LSP — coming when we add workspace folder traversal.

## Editor setup

### VSCode

Use the companion extension at `Homogeneity/vscode-extension/`.
The README in that folder walks through the F5 dev-host workflow.
Point the `dimfort.executable` setting at your DimFort install
(typically a virtualenv).

### Neovim (built-in LSP)

No extension needed — register the server directly:

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
