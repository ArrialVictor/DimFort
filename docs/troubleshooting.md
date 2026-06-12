# Troubleshooting

Common problems and how to resolve them. Open an issue if your
case isn't here.

## Install

### `pipx: command not found`

Install pipx first:

```bash
brew install pipx && pipx ensurepath          # macOS (Homebrew)
sudo apt install pipx && pipx ensurepath      # Debian / Ubuntu
python3 -m pip install --user pipx && pipx ensurepath
```

Restart the shell or `source ~/.bashrc` / `~/.zshrc` after
`ensurepath`.

### `error: externally-managed-environment`

PEP 668 stops `pip install` from writing to system Python. Use
`pipx` (the recommended path in [quickstart/install.md](quickstart/install.md))
or install into a virtualenv. Avoid
`pip install --break-system-packages`.

### `dimfort: command not found` after install

The install location is not on your `PATH`. With pipx, run
`pipx ensurepath` and restart your shell. With a virtualenv,
activate it (`source .venv/bin/activate`) or invoke DimFort by
its absolute path.

## Editor companion

### Companion doesn't start the LSP

Check that the editor can launch the binary:

```bash
which dimfort
dimfort --version
```

In VSCode, set `dimfort.executable` to the absolute path printed
by `which dimfort`. In Neovim / Emacs / Helix, ensure your editor
inherits a `PATH` that includes that location — `launchd`-started
GUI editors on macOS often don't pick up `~/.zshrc` updates.

### No diagnostics light up

Open the language-server log (each editor exposes this differently
— "Output → DimFort" in VSCode, `:LspLog` in Neovim, `*lsp-log*`
buffer in Emacs). Common causes:

- The file isn't recognized as Fortran. Check the editor's filetype
  detection.
- The workspace folder is unset. The server runs the workspace
  index on `initialize` from `workspace_folders`; if none are
  passed, only the open file is checked.
- The `dimfort.toml` is malformed. The server logs a warning and
  continues with defaults — confirm it parses with `python3 -c 'import tomllib; tomllib.load(open("dimfort.toml", "rb"))'`.

## Diagnostics

### Every file fires `P001`

P001 marks regions the parser couldn't read — common on legacy
F77 idioms or `.F90` files whose `module` / `use` constructs are
gated by `#ifdef`. Either:

- Configure CPP support so the gated regions become visible:

  ```toml
  [parser]
  cpp_defines   = ["WITH_NETCDF=1"]
  include_paths = ["include"]
  ```

- Or silence P001 per-project if you accept the unparsed regions:

  ```toml
  [diagnostics]
  P001 = "off"
  ```

See [design/shipped/unparsed-regions.md](design/shipped/unparsed-regions.md)
for the full story.

### Flood of `U005` "unannotated variable" warnings on first run

This is expected on a codebase that has not been annotated yet —
U005 fires on every variable used in a unit-relevant position
that lacks `@unit{}`. Approach options:

- Demote U005 to `"info"` while you ramp up annotation:

  ```toml
  [diagnostics]
  U005 = "info"
  ```

- Annotate the high-leverage constants modules first; the
  signatures propagate to every caller.
- Use `dimfort interactions <var>` to see every site that touches
  a single variable before you commit to a unit.

### `H004` on a call to a function you can't annotate

The called function lives in a vendored library or external
module. Add its parent module to `[workset] external_modules`:

```toml
[workset]
external_modules = ["netcdf", "mpi", "vendor_legacy"]
```

The call now resolves to "unit unknown" and stops firing H004.

### `U002` "could not parse unit string"

The captured text isn't a valid unit expression. Common causes:

- Digit-suffix exponents: `m2` should be `m^2`. From 0.2.2 the
  message carries a `did you mean m^2?` suggestion and the editor
  exposes a one-click Quick Fix.
- Stray whitespace inside the directive: `@unit{ m / s }` is fine,
  but `@unit{m / s }*2` is not — the trailing `*2` is part of the
  capture.
- A unit name the catalog doesn't know. Define it in an extension
  units file and point `[units] file` at it — see
  [`dimfort.toml` reference](reference/dimfort-toml.md#units).

## Performance

### LSP feels sluggish on a large workspace

Two knobs:

- Cap the per-check workset:

  ```toml
  [workset]
  max_size = 80
  ```

  (Or pass `maxWorksetSize` via the editor's LSP
  `initializationOptions`.) DimFort then pins the active file's
  direct dependencies and drops topo-last entries.

- Enable the content-hash cache so unchanged files skip the
  check phase:

  ```jsonc
  // VSCode settings.json or any LSP client's init options
  { "cacheMode": "read-write" }
  ```

  Or on the CLI: `dimfort check --cache read-write`.

  Cache contents live at `.dimfort-cache/` under the first input
  path; add that to `.gitignore`. See
  [usage.md § Content-hash cache](usage.md#content-hash-cache).

### CI runs are slower than I expect

The content-hash cache delivers most of its benefit on warm
re-runs. On a clean CI checkout the cache directory is empty
every time, so the cache write overhead is dead weight — leave
the cache off in CI.

```bash
dimfort check src/        # implicit --cache off
```

## Annotation discipline

### When to use `@unit_assume{}`

Only for **irreducible** unit assertions — an RHS unit DimFort
cannot derive on its own (e.g. a base raised to a non-rational
power, an empirical fit with no closed-form dimensional
analysis). Every use carries a mandatory reason and fires `U020`
as an audit note.

If the unit *is* derivable but DimFort doesn't see it (e.g. a
literal silently carries a unit), express it as a typed
`PARAMETER` instead, not as an assume.

### Cross-file annotations not picked up

DimFort aggregates signatures across files passed to a single
`check` invocation. If you check files one at a time, cross-file
`use` resolution doesn't fire. Pass the whole workset:

```bash
dimfort check src/                       # walks the directory
dimfort check src/a.f90 src/b.f90        # explicit list
```

The LSP always sees the full workspace once the initial index
build finishes.

## Still stuck?

Open an issue at <https://github.com/ArrialVictor/DimFort/issues>
with: the DimFort version (`dimfort --version`), a minimal source
file that reproduces the problem, your `dimfort.toml` if any,
and the exact command + output.
