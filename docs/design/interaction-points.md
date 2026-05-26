# Interaction points & conflicting-constraint detection — design spec

Status: **in progress** on the `interaction-points` branch (v1 = CLI query +
conflict diagnostic; no editor UI yet).

This document is the spec. Code follows the doc. If something here turns out
wrong during implementation, **update this doc first**, then the code.

## The problem

DimFort tells you whether a *statement* is dimensionally homogeneous. It does
not tell you, for a given *variable*, what every site that touches it implies
about its unit. Yet that cross-site view is exactly what a human does when
auditing a suspicious unit — and it is where the hardest real bugs live.

Concrete motivation (LMDZ findings #017/#019/#020, see
`../../../LMDZ_ICEPHASE_TRACE.md`):

- `dzfice` (a derivative passed `ICEFRAC_LSCP → lscp`) is used at `lscp:669`
  in a way that requires it to be `{1}`, and at `lscp:857` in a way that
  requires `{1/K}`. **The two use-sites contradict each other.** Neither line
  fires in isolation when `dzfice` is unannotated — the contradiction is
  invisible to a per-statement checker. Finding it took a manual trace of
  every read/write across two routines in two files.
- `invtau_phaserelax` is pinned to `{1/s}` purely by the use-site
  `invtau_phaserelax + invtau_e`.

The unit of a variable is fixed by its **use-site interactions**, not by its
declaration. This feature surfaces those interactions and flags when they
disagree.

## What it does (v1)

A new **on-demand** CLI command:

```
dimfort interactions <symbol> [paths...] [--file F] [--scope ROUTINE] [--scale]
```

For `<symbol>` (case-insensitive, Fortran semantics), across the resolved
workset, it collects every **interaction point** — a read or write of the
symbol in a unit-checked expression — and classifies the **constraint** each
places on the symbol's unit:

| Kind (internal) | User-facing label | Meaning | Example |
|---|---|---|---|
| `declares` | **Declaration** | the `@unit{...}` annotation on the declaration | `real :: x !< @unit{m/s}` |
| `contributes` | **Write** | variable on the LHS of `=`; the RHS unit flows into it | `x = a*b` ⇒ `unit(a*b)` |
| `requires` | **Read** | a read whose context fixes the symbol's unit (an equality constraint) | `x + y` ⇒ `unit(y)` |
| `uses` | **Undetermined read** | a read for which no required unit was determined — either none exists (`z = x*w`) or one exists but a coefficient was un-annotated so it couldn't be derived | `z = x*w` |

User-facing labels are deliberately **structural** (what the site *is*), not
directional ("contributes"/"requires" forced a viewpoint that read ambiguously
— a read can equally be seen as the variable *contributing* its unit outward or
the context *requiring* a unit of it). The internal kind names keep the
directional vocabulary since they describe what the analyzer computed; only the
display layer (`interactions.KIND_DISPLAY`) is structural. The command prints the
symbol grouped by kind, each site with `file:line`, the resolved unit (or `?`
when unknown), and the source slice.

`--file` / `--scope` disambiguate a name reused across files / routines. With
no filter, every scope that declares-or-uses the name is reported (each scope
analysed independently — no cross-scope unit bleed, per finding #018).

### The conflict diagnostic — `X001`

The payoff. After collecting the constraints, the symbol is **over-constrained**
when two sites disagree on its dimension:

- two `requires` sites with different dimensions, or
- a `contributes` (producer) unit whose dimension differs from a `requires`
  (consumer) unit, or
- a `declares` unit whose dimension differs from any `requires`/`contributes`.

Each such pair emits **`X001`** (ERROR), e.g.

```
lmdz_lscp_main.f90:669: error: X001 conflicting unit claims for 'dzfice':
  read here claims 1, but declaration at lmdz_lscp_main.f90:264 claims 1/K
```

`X001` is *only* produced by the `interactions` command (it is not part of the
`check` pass). It fires **even when the symbol is unannotated** — that is the
whole point: the contradiction is a property of the use-sites, independent of
whether anyone wrote `@unit{}`. Dimension mismatch only in v1; scale (`factor`)
disagreements are reported as conflicts only under `--scale` (mirrors S001's
opt-in), so dimension-only stays first-class.

## The constraint model

For a read occurrence node `n` (an `identifier` whose text is the symbol),
`required_unit_of(n)` is the unit the *position* of `n` is forced to have by
its context. It is a recursion **up** the AST that propagates a known target
unit **down** through arithmetic — the mechanical version of the manual trace:

- **assignment RHS** (`lhs = … n …`): anchor = `unit(lhs)` (the declared LHS
  unit). The whole RHS must equal it.
- **call argument** (`foo(…, n, …)` / `call foo(…, n, …)`): anchor =
  `signature(foo).arg_units[i]` for the argument position `i` that contains `n`
  (only when the callee has a known signature — array indexing has none).
- **additive** parent (`a + b`, `a - b`): if a target propagated from above is
  known, `n`'s term must equal it; else the unit is pinned by *any other term in
  the enclosing `+`/`-` chain that resolves* — a bare literal (`1.`) anchors the
  whole sum to `{1}`, so a single unknown inside a sibling term can't blind us.
  (This pins `invtau_phaserelax` from `+ invtau_e`, and the `dzfice` term in a
  `1. + … - coeff*dzfice` denominator from the `1.` literal.) Only the term
  containing `n` is skipped, to avoid the circular "`n` requires its own unit."
- **multiplicative** parent (`a * b`, `a / b`): solve through, *only if* the
  enclosing target is known and the sibling factor resolves:
  - `n * s = R` ⇒ `n = R / s`
  - `n / s = R` ⇒ `n = R * s`   (n numerator)
  - `s / n = R` ⇒ `n = s / R`   (n denominator)
  (This is what pins `dzfice` from `zqsi*dzfice` inside the `:669` sum, and
  from `(ΔL/cp)*…*dzfice` inside the `:857` sum.)
- **parenthesised / unary**: transparent — recurse to the parent.
- **`**` (power), or anything else**: `None` (no equality constraint; the
  exponent must be dimensionless but that is a separate, existing check).

A write occurrence (assignment LHS) is a **contributes**: the contributed unit
comes from `_assignment_homogeneity` (the checker's single source of truth), so
the autocast rule R4.4 applies — a *pure-literal* RHS (`x = 0.0`) is
unit-agnostic, adopts the declared LHS unit, and makes **no independent claim**
(so it can never manufacture a conflict, exactly as `dimfort check` stays
silent). A real computed RHS keeps its own resolved unit.

All unit resolution reuses `ts_checker._resolve` and the unit algebra
(`Unit.__mul__`/`__truediv__`, `combine`, `compare`) — no new dimensional logic.
A constraint resolving to `None` (unknown) is reported as `?` and never
participates in conflict detection (unknown ≠ conflict — no false positives).

## Architecture

- `core/interactions.py` (new): the engine. Public entry
  `collect_interactions(workset: WorksetResult, symbol: str, *, file=None,
  scope=None, scale=False) -> SymbolReport`. Builds a per-file `_Ctx` via the
  extracted `ts_checker._build_ctx`, walks each tree for occurrences, classifies
  each, and runs conflict detection. Returns a structured report (dataclasses:
  `InteractionPoint`, `Conflict`, `SymbolReport`) — CLI-agnostic so the LSP/panel
  can consume it later.
- `ts_checker._build_ctx` (extracted this branch): single source of truth for
  `_Ctx` construction, shared by `check` and `collect_interactions`.
- `cli.py`: `interactions` subcommand + `_run_interactions` (formatting only).
- `diagnostics.py`: register `X001` in `CODES`.

## LSP endpoint

`dimfort/interactions` (custom request) — resolves the identifier under the
cursor (or an explicit `symbol` param), runs `collect_interactions` over the
cached workset (`_last_result`), and returns the serialised report
(`{symbol, points[], conflicts[], hasConflict}`). Mirrors `dimfort/panelInfo`'s
cursor model. This is what the editor companions consume for an interactions
panel tab.

## Explicitly out of scope for v1

- **VS Code panel tab** (and the other companions' equivalents). The LSP
  endpoint above makes it a thin client change, but the UI itself is deferred
  to a companion branch.
- **Whole-workset "audit every symbol" sweep.** v1 is one symbol per
  invocation; an always-on `X001` over the whole tree risks noise + perf cost
  and needs the U005-cross-file machinery first (see memory
  `project_u005_cross_file`).
- **Derived-type field members** (`o%x`) as the queried symbol. Reads of a
  scalar variable only in v1.
- **Solving through intrinsics** other than the transparent passthroughs
  `_resolve` already handles.

## Testing

`tests/unit/test_interactions.py`, inline-Fortran fixtures (house style):
producer/consumer/sibling/call-arg constraints; the additive-term-with-
coefficient shape (the `dzfice` `{1}` vs `{1/K}` conflict); `invtau + invtau_e`
shape; unknown-stays-unknown (no false conflict); scope disambiguation; and a
clean symbol with agreeing constraints (no `X001`).
