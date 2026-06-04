# Install

DimFort is published on PyPI as `dimfort` and requires Python 3.11
or later. Pick the path that matches how you want to use it.

## Recommended: pipx

[`pipx`](https://pipx.pypa.io/) installs DimFort into its own
isolated virtualenv and exposes the `dimfort` command on your `PATH`.
This is the right default for everyday use — CLI + language server
both work, and upgrades stay clean.

```bash
pipx install 'dimfort[lsp]'
dimfort --version
```

The `[lsp]` extra pulls in `pygls`. Omit it if you only want the
CLI and never the language server.

## In a project virtualenv

If your project already has a virtualenv (e.g. you write Fortran
alongside Python tooling), install DimFort there:

```bash
source .venv/bin/activate
pip install 'dimfort[lsp]'
```

Point your editor companion at the resulting `dimfort` binary
(usually `.venv/bin/dimfort`) so it picks up the right copy.

## From source

For contributing or pinning to a development branch:

```bash
git clone https://github.com/ArrialVictor/DimFort.git
cd DimFort
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,lsp]"
```

## Troubleshooting

### `pipx: command not found`

```bash
brew install pipx && pipx ensurepath          # macOS (Homebrew)
sudo apt install pipx && pipx ensurepath      # Debian/Ubuntu
python3 -m pip install --user pipx && pipx ensurepath
```

Restart your shell or `source ~/.bashrc` (or `~/.zshrc`) after
`ensurepath`.

### `error: externally-managed-environment` (macOS Homebrew Python)

PEP 668 stops `pip install` from touching Homebrew's site-packages.
Use `pipx` (the recommended install path above) or install into a
virtual environment — never `pip install --break-system-packages`.

### Editor companion can't find DimFort

The editor needs to know which `dimfort` binary to launch. In
VSCode set `dimfort.executable` to the absolute path; in Neovim /
Emacs the launch command (`cmd = { "dimfort", "lsp" }` and
similar) must resolve through your shell's `PATH`. Run
`which dimfort` to confirm the binary you expect is the one being
picked up.

## Next steps

- [First check](first-check.md) — run DimFort on a sample file and
  read the output.
- [Bringing DimFort to an existing codebase](bringing-to-existing-codebase.md)
  — adopt it on a project that already documents units in inline
  comments.
- [Editor integration](../editor-integration/) — wire DimFort into
  VSCode, Neovim, Emacs, or Helix.
