# Unparsed regions — the P001 "no guarantees here" marker

## Motivation

DimFort's value is a *guarantee*: where it puts no squiggle, the units are
consistent. There is one place that quietly breaks the promise — a region the
parser couldn't make sense of. Tree-sitter is error-tolerant: on a construct it
can't parse it emits an `ERROR` (or `missing`) node and recovers, so the rest of
the file still checks. But the checker simply *skips* those regions — no
resolution, no diagnostics — and the user can't tell "checked and clean" from
"never looked at it."

`P001` closes that gap: an **informational** marker on each unparsed region,
meaning *"DimFort could not parse this — it makes no unit claim about these
lines."* Honest by construction.

## Behaviour

- **Code `P001`, severity `INFO`.** In an LSP client an `Information` diagnostic
  renders as a faint blue underline (+ a Problems-panel entry) — distinct from
  the red/yellow of real H/S violations, which is exactly the "this is FYI, not
  a violation" signal we want. No companion changes are needed: the marker rides
  the existing `textDocument/publishDiagnostics` stream, so VSCode / Neovim /
  Emacs all render it from day one.
- **One diagnostic per contiguous unparsed region.** Tree-sitter can emit many
  nested `ERROR`/`missing` nodes for one bad construct; we coalesce their line
  spans into contiguous regions and emit a single `P001` per region, so a file
  with one unparseable statement gets one marker, not twenty.
- **Message**: `could not parse this region — DimFort makes no unit guarantee
  here`.
- **Scope**: emitted only for files that *parsed with errors*. A file that fails
  to load entirely is already reported as `U007` (load failure); `P001` is for
  the partial case (tree-sitter recovered but left `ERROR` regions).

## Where it's emitted

Inside `ts_checker.check`, alongside the H/S/U diagnostics, by walking
`ts_parser.error_nodes(tree)`. Emitting here (rather than in `multifile`) means
`P001` automatically gets:

- **Severity overrides** — `check` runs every diagnostic through
  `finalize_diagnostics`, so a project can downgrade, bump, or silence it.
- **cpp line-map remapping** — `multifile` remaps `check`'s diagnostics back to
  source coordinates, so the marker lands on the right line even for `.F90`
  files that went through the preprocessor.

## Suppressing it

`P001` is **on by default** (the honesty is the point). To silence it project-
wide, use the existing severity-override mechanism in `.dimfort.toml`:

```toml
[diagnostics]
P001 = "off"
```

This matters because **DimFort targets F90+ by design** (see the modernizer for
F77 → F90). A codebase with hand-rolled F77 idioms will light up with `P001`
markers — which is accurate (DimFort genuinely can't check those lines), but a
team that knows its F77 files and isn't ready to modernize can turn the marker
off, or bump it to `warning`/`error` to *enforce* parseability in CI.

## Not a unit marker

`P001` is informational, not a unit-resolution verdict, so it carries **no**
🟢/🟡/🔴 unit marker (those are reserved for the H/S consistency axis, per
[`markers.md`](markers.md)). It surfaces only as a squiggle and in the panel's
Diagnostics section.

## Out of scope (for now)

- A dedicated decoration style in the companions (a true coloured underline
  independent of LSP severity). The `Information` severity is good enough and
  free; revisit only if users want it visually distinct from other info-level
  diagnostics.
- Per-line opt-out directives (`! dimfort: allow-unparsed`). The project-wide
  `off` switch covers the known-F77 case; per-line granularity can come later if
  asked for.
