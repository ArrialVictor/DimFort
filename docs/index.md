# DimFort

Static unit-consistency checker for Fortran, with LSP integration.

- [Annotations](reference/annotations.md) — `@unit{…}` syntax, placement rules, continuation lines, Doxygen integration.
- [Usage](usage.md)
- [LSP](editor-integration/lsp-protocol.md) — user-facing features; internals in [LSP architecture](design/contributor/lsp-architecture.md).
- [Releases](release-process.md)

Internals (for contributors): [core architecture](design/contributor/core-architecture.md) — the check pipeline (parse → annotate → attach → check) and the `core/` module map; [LSP architecture](design/contributor/lsp-architecture.md) — the editor layer on top.

> Status: beta. CLI, LSP, and diagnostic pipeline are working end-to-end on tree-sitter; the `@unit{}` format, diagnostic codes, and LSP protocol may still shift between `0.x` releases. See [Usage](usage.md) for the current feature list.
