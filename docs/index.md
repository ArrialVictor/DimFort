# DimFort

Static unit-consistency checker for Fortran, with LSP integration.

- [Annotations](annotations.md) — `@unit{…}` syntax, placement rules, continuation lines, Doxygen integration.
- [Usage](usage.md)
- [LSP](lsp.md) — user-facing features; internals in [LSP architecture](design/lsp-architecture.md).
- [Releases](release.md)

Internals (for contributors): [core architecture](design/core-architecture.md) — the check pipeline (parse → annotate → attach → check) and the `core/` module map; [LSP architecture](design/lsp-architecture.md) — the editor layer on top.

> Status: pre-alpha. CLI, LSP, and diagnostic pipeline are working end-to-end on tree-sitter; see [Usage](usage.md) for the current feature list.
