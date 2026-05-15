# AST-only mode — design notes

**Status:** branch `ast-only`. Phase 0 spike landed; Phase 1+ TBD.

## Motivation

The current checker walks LFortran's **ASR** (resolved/typed tree). ASR
gives us type info, resolved `use`-imports, intrinsic dispatch, and
expression typing for free. But ASR fails on a handful of F77 idioms
embedded in F90 source — most prominently the `COMMON`+`PUBLIC`
forward-reference pattern in two LMDZ files. See
[scratch/f77-survey/README.md](../../scratch/f77-survey/README.md) for the
full audit: 4 idioms are "AST passes, ASR fails."

In an AST-only checker, those files become first-class supported, the
`lfortran -c` Phase 1 disappears entirely, and we halve LFortran
subprocess calls per check. Cost: we re-implement the semantic
resolution ASR was doing for us, by hand, against AST nodes.

## Scope of "AST-only"

Use **LFortran's AST** as the single source of truth. Do not invoke
`--show-asr` and do not run `lfortran -c`. Do not parse Fortran
ourselves — we still ride LFortran for tokenisation, parse-tree
construction, and source-position tracking.

What we re-implement:
1. Per-file symbol table from `Declaration` nodes.
2. Cross-file `use`-chain resolution by walking each module's AST.
3. Expression typing/unit propagation by walking AST `BinOp` etc.
4. Intrinsic dispatch (extend the existing `collect_intrinsic_names`).
5. Derived-type member resolution.

What we deliberately do not re-implement:
- Operator overloads. Rare in scientific code; falls through.
- Numeric kind tracking (we ignore kind for unit purposes).
- Generic interfaces.

## Phasing

| Phase | Goal | Status |
|---|---|---|
| 0 | Spike: single-file H001 from AST, no ASR involved. Prove the pattern. | landed |
| 1 | Per-file resolver covers H001 + H002 + H003 + H004 (within one file). | landed |
| 2 | Cross-file `use`-chain symbol resolution. | landed |
| 3 | Derived types, intrinsics beyond the six categories, casts, array sections. | TBD |
| 4 | Selectable backend (`[checker] backend = "ast" \| "asr"`) wired through CLI + LSP. | TBD |
| 5 | Default flipped to AST; Phase 1 (lfortran -c) removed from `check_files`. | TBD |

## Risks worth tracking

- **Implicit kind promotion.** `Cast` nodes don't exist in AST. For pure unit checking this is fine — units are kind-agnostic.
- **Forward references.** AST gives no resolution. We need a two-pass approach: gather all declarations first, then walk expressions.
- **Operator overloads.** ASR resolves `+` to user-defined operators when applicable. We will not; flag as a known gap.
- **`use, only:` with renames.** AST has the rename text; we apply it by hand when threading symbols across files.
- **Silent degradation.** Worst failure mode: a resolver returns `None`/unknown for an expression we should have checked, no diagnostic fires. Need explicit "I-don't-know-this-node" warnings during development.

## Phase 0 deliverable

A function `dimfort.core.ast_checker.check(ast, var_units, file=...)` that
walks an AST + an already-attached `var_units` dict and produces
`Diagnostic` objects for H001 (assignment mismatch) and H002 (add/sub
mismatch). Tested end-to-end on `tests/fixtures/smoke_check.f90`:
must produce the same H001 the ASR-based checker produces.

Out of scope for Phase 0: cross-file, intrinsics, casts, derived
types, anything beyond `Name | Num | BinOp(+,-,*,/) | Assignment`.
