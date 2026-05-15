# Changelog

All notable changes to DimFort are documented here. Format inspired by [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]
- Initial project scaffold: `src/` layout, pyproject, CLI stub, LSP stub, CI workflow.

### Branch `ast-only`

- **Phase 0 (spike, 2026-05-15)** — minimal AST-only checker landing as `core.ast_checker.check`. Walks LFortran's AST (no ASR involvement, no `lfortran -c`) and emits H001 + H002 for `Name` / `Num` / `BinOp(+,-,*,/)` / `Assignment` node combinations. Demonstrated end-to-end on `tests/fixtures/smoke_check.f90`: H001 fires on the dimensionally-wrong assignment, not on the clean one. Design notes in `docs/ast-only-design.md`; rest of the H/U series, cross-file `use`-chain resolution, intrinsics, derived types, casts, and array sections are TBD across Phases 1–5.
