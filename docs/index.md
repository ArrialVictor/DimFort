# DimFort documentation

DimFort is a static unit-consistency checker for Fortran. You
annotate declarations with the physical dimension they should
carry — `@unit{m/s}`, `@unit{kg*m^2/s^2}`, … — and DimFort verifies
that assignments, arithmetic, intrinsics, and procedure calls all
line up. It ships as a CLI (`dimfort check`) and a language-server
(`dimfort lsp`) with companions for VSCode, Neovim, and Emacs.

Status: **beta**. Usable on real-world Fortran today; the
`@unit{}` format, diagnostic codes, and LSP protocol may still
shift between `0.x` releases.

## Get started

- [Install](quickstart/install.md) — pipx, virtualenv, or from source.
- [Your first check](quickstart/first-check.md) — run DimFort on the
  bundled `demos/tour.f90` and read the output.
- [Bringing DimFort to an existing codebase](quickstart/bringing-to-existing-codebase.md)
  — configure DimFort to read your project's inline-comment
  unit conventions (`! [m/s]`, `! [m^2: empirical]`, …) without
  rewriting every declaration.
- [Troubleshooting](troubleshooting.md) — install, editor, diagnostics,
  performance.

## Reference

- [Annotations](reference/annotations.md) — `@unit{...}` grammar,
  placement rules, continuation lines, `@unit_assume` /
  `@unit_affine_conversion`, derived-type fields.
- [Polymorphism (`'a`, `'b`, …)](reference/polymorphism.md) —
  generic functions over arbitrary units; H020/H021/H022/H023.
- [CLI](reference/cli.md) — `check` / `interactions` / `lsp` flags
  and exit codes.
- [Diagnostic codes](reference/diagnostic-codes.md) — every code
  (H, U, S, X, P) with severity and trigger.
- [Unit algebra](reference/unit-algebra.md) — the rule taxonomy
  (`R1.1`–`R7.1`) and D-class diagnostic mapping.
- [Intrinsics](reference/intrinsics.md) — Fortran intrinsics whose
  unit semantics DimFort knows.
- [`.dimfort.toml`](reference/dimfort-toml.md) — every config key.
- [Project units file](reference/units-file.md) — extend the
  shipped SI catalog with domain-specific units (`hPa`, `bar`,
  `percent`, …).

## Editor integration

- [Side panel](editor-integration/side-panel.md) — what the
  cursor-following panel shows across all three companions.
- [Hover UI](editor-integration/hover-ui.md) — render rules and
  marker vocabulary.
- [LSP protocol](editor-integration/lsp-protocol.md) — wire
  contract for editor integrators: `initializationOptions`,
  custom requests, debouncing, workspace commands.
- [Editor commands](editor-integration/commands.md) — cross-companion
  reference table mapping every user-facing command across the three
  companions. Anti-drift artifact: per-companion renames visibly
  desync a row here.

## Design

The `design/` tree is contributor and future-feature documentation;
end users don't need it.

- `design/shipped/` — specs for shipped features (markers, scale,
  symbolic exponents, panel-info, content-hash cache, interaction
  points, unparsed regions, unit-comment delimiters, polymorphic
  units, coverage visualization, multifile cache).
- `design/future/` — proposals not yet built: `audit` command,
  rewrite rules.
- `design/contributor/` — internals for people editing DimFort
  itself (core architecture, LSP architecture).

## Releases

- [Changelog](https://github.com/ArrialVictor/DimFort/blob/main/CHANGELOG.md)
- [Release process](release-process.md) — maintainer-only: tag,
  PyPI publish, GitHub release.
