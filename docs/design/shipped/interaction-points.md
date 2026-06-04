# Interaction points & conflicting-constraint detection — design spec

DimFort's `check` pass answers "is this *statement* dimensionally homogeneous?".
The **interactions** feature answers a different question: "for a given
*variable*, what does every site that touches it imply about its unit, and do
those implications agree?". That cross-site view is what a human does when
auditing a suspicious unit in real-world Fortran codebases, and it is where the
hardest bugs live — a unit contradiction that no single statement reveals.

This document is the spec for both the on-demand CLI and the LSP request the
editor panels consume.

## The problem

The unit of a variable is fixed by its **use-site interactions**, not by its
declaration. A scalar can be written once and read in two spots; if those reads
imply incompatible dimensions, the variable is over-constrained — but neither
read fires on its own, because per-statement homogeneity is satisfied locally.
Surfacing this requires looking at every site for the symbol at once, deriving
the unit each site *requires* of it, and comparing.

## The constraint model

For a read occurrence of the queried symbol (an `identifier` node whose text
equals the symbol, case-insensitively), the **required unit** is the unit the
*position* of that occurrence is forced to have by its context. It is computed
by walking **up** the AST, propagating a known target unit **down** through
arithmetic — the mechanical version of the manual trace.

- **Assignment RHS** (`lhs = … n …`): the anchor is `unit(lhs)` (the declared
  LHS unit). The whole RHS must equal it.
- **Call argument** (`foo(…, n, …)` / `call foo(…, n, …)`): the anchor is the
  callee signature's `arg_units[i]` for argument position `i`, but only when a
  signature is known (array indexing has none).
- **Additive parent** (`a + b`, `a - b`): if a target unit propagated from
  above is known, the term containing `n` must equal it. Otherwise the unit is
  pinned by *any other term in the enclosing `+`/`-` chain that resolves* — a
  bare literal (`1.`) anchors the whole sum to `{1}`, so a single unknown
  inside one sibling term can't blind us. Every term inside `n`'s own subtree
  is skipped, to avoid the circular "`n` requires its own unit."
- **Multiplicative parent** (`a * b`, `a / b`): solve through, *only if* the
  enclosing target is known and the sibling factor resolves:
  - `n * s = R` ⇒ `n = R / s`
  - `n / s = R` ⇒ `n = R * s`   (n numerator)
  - `s / n = R` ⇒ `n = s / R`   (n denominator)
- **Parenthesised / unary**: transparent — recurse to the parent.
- **`**` (power) or anything else**: `None` (no equality constraint; the
  exponent must be dimensionless but that is a separate, existing check).

A **write** occurrence (assignment LHS) is a `contributes`: the contributed
unit comes from `assignment_homogeneity` (the checker's single source of
truth), so the autocast rule R4.4 applies — a *pure-literal* RHS (`x = 0.0`)
is unit-agnostic, adopts the declared LHS unit, and makes **no independent
claim** (so it can never manufacture a conflict, exactly as `dimfort check`
stays silent). A real computed RHS keeps its own resolved unit.

All unit resolution reuses `resolve_unit` and the unit algebra
(`Unit.__mul__` / `__truediv__`, `compare`) — no new dimensional logic. A
constraint resolving to `None` is reported as `?` and never participates in
conflict detection (unknown ≠ conflict — no false positives).

## Interaction kinds

Each occurrence is classified into one of four kinds. The internal `kind`
strings keep the directional vocabulary the analyzer computes in; the
**user-facing labels are structural** (what the site *is*), because the
directional names ("contributes" / "requires") read ambiguously — a read can
equally be seen as the variable contributing its unit outward or the context
requiring a unit of it. The display layer (`KIND_DISPLAY`) is the only
vocabulary any user surface — CLI, panel, X001 message — should speak.

| `kind` (internal) | Display label    | Meaning                                                                             |
|-------------------|------------------|-------------------------------------------------------------------------------------|
| `declares`        | **Declaration**  | the `@unit{...}` annotation on the declaration                                      |
| `contributes`     | **Write**        | variable on the LHS of `=`; the RHS unit flows into it                              |
| `requires`        | **Read**         | a read whose context fixes the symbol's unit (an equality constraint)               |
| `uses`            | **Undetermined** | a read whose context places no equality constraint, or one whose sibling is unknown |

Only `declares`, `contributes`, and `requires` pin the variable's unit;
`uses` does not, and is excluded from conflict detection.

> **Historical note.** The `uses` group was previously labelled "Undetermined
> read"; it is now just **Undetermined** across the CLI, all panel renderers,
> and the X001 message text. The internal `kind` value is unchanged.

## The conflict diagnostic — `X001`

The payoff. After classification, the symbol is **over-constrained** when two
*constraining* sites (`declares` / `contributes` / `requires`) in the same
`(file, scope)` group disagree on the variable's unit. Each such pair emits
**`X001`** (severity `ERROR`, registered in `core/symbols.py`).

`_is_conflict` is **scale-mode-aware**:

- Default (`scale=False`): a `dim_mismatch` from `compare()` is a conflict.
  `scale_mismatch` is **not** — dimension-only stays first-class.
- Opt-in (`scale=True`): `scale_mismatch` *also* counts. Mirrors S001's
  opt-in (see [scale.md](scale.md)).

The diagnostic span is a point at the offending site's `(line, column)`.
Repeated identical claims on the same source line collapse (a symbol used
twice in `a*x - b*x` yields a single X001, not two). The message is built from
the structural labels:

```
file:line: error: X001 conflicting unit claims for 'dzfice':
  read here claims 1, but declaration at file:264 claims 1/K
```

X001 fires **even when the symbol is unannotated** — that is the whole point:
the contradiction is a property of the use-sites, independent of whether
anyone wrote `@unit{}`.

Same-named variables in different routines are *different variables*, so
conflict detection never crosses a scope boundary. The grouping key in
`_detect_conflicts` is `(file, scope)`, where `scope` is the lower-cased
enclosing routine name (or `None` at module level).

X001 lights up 🔴 in panels — see [markers.md](markers.md) for the marker
vocabulary. It is **not** emitted by the regular `check` pass; only the
`interactions` flow surfaces it.

## CLI

```
dimfort interactions <symbol> [paths...] [--file F] [--scope ROUTINE]
                              [--scale] [--no-color]
```

- `<symbol>` is case-insensitive (Fortran semantics).
- `paths` are the Fortran source files / directories to search.
- `--file F` restricts to occurrences in files whose name or path-suffix
  matches `F`.
- `--scope ROUTINE` restricts to occurrences in the routine with that name
  (case-insensitive). With no filter, every scope that declares-or-uses the
  name is reported (each scope analysed independently — no cross-scope unit
  bleed).
- `--scale` includes magnitude (`factor`) disagreements as conflicts, in
  addition to dimension mismatches.
- `--no-color` disables ANSI colour (also auto-disabled outside a TTY).

Output groups the symbol's sites in display order **Declaration → Write →
Read → Undetermined** (the same order as `KIND_DISPLAY`). Each row shows
`file:line [scope]`, the resolved unit, and the enclosing-statement snippet.
The Undetermined group **omits the unit column** — the group label already
says no unit was determined; a redundant `?` per row would be visual noise.

Empty groups are skipped on the CLI (the panel renders them as `(none)`).

Conflicts, if any, print after the groups as `⚠ X001 …` lines. Exit status:

- `0` — at least one site found, no conflict.
- `1` — at least one X001 conflict.
- `2` — bad invocation (no inputs, missing paths, …); `0` with a `no
  read/write … found` message when the symbol simply isn't present.

## LSP — `dimfort/interactions`

Custom request consumed by the panel section of the same name. The wire shape
is mirrored in
[panel-info.md § `dimfort/interactions`](panel-info.md#dimfortinteractions);
this section is the authoritative server-side contract.

```
request:   "dimfort/interactions"
params:    {
             textDocument?: { uri: DocumentUri },
             position?: Position,
             symbol?: string,
             scale?: boolean,
           }
response:  InteractionsReport | null
```

Resolution:

1. If `symbol` is given explicitly, it is used as-is.
2. Otherwise the handler resolves the identifier under `(uri, position)`. It
   parses a **fresh** tree from the live document for the lookup, so it does
   not need the shared tree-handler lock; it falls back to the cached tree
   only if the fresh parse fails.
3. If no symbol can be resolved (no cursor / not on an identifier / no
   cached workset), the response is `null`.

```typescript
interface InteractionsReport {
  symbol: string;
  points: InteractionPoint[];
  conflicts: InteractionConflict[];
  hasConflict: boolean;
}

interface InteractionPoint {
  file: string;                  // absolute path
  line: number;                  // 1-based
  column: number;                // 1-based
  scope: string | null;          // lower-cased routine name, or null at module level
  kind: "declares" | "contributes" | "requires" | "uses";
  unit: string;                  // rendered unit, or "?" when unknown
  snippet: string;               // enclosing statement, whitespace-collapsed
}

interface InteractionConflict {
  code: string;                  // "X001"
  message: string;
  file: string;
  line: number;                  // 1-based; matches site.line/column
  column: number;
  site: InteractionPoint;        // the disagreeing site
  reference: InteractionPoint;   // the earlier site it disagrees with
}
```

Points are sorted `(file, line, column)`. Conflicts iterate per
`(file, scope)` group, comparing each constraining site to the first one
seen in that group.

`scale` defaults to `false` on the wire. The companion panels pass through
whatever the user has configured; the server applies it both to the
underlying workset build and to `_is_conflict`.

## Panel rendering contract

All three companion panels (VS Code, Neovim, Emacs) render the
`InteractionsReport` identically:

1. **Symbol header** (the resolved name).
2. **Conflicts** first — a 🔴 row per `InteractionConflict`, clickable to
   `(site.file, site.line, site.column)`.
3. **The four kind groups**, always rendered in display order even when
   empty (so the section structure is stable across cursor moves). Empty
   groups show `(none)`.
4. **Site rows** under each group: `(basename:line  unit)` on one line,
   the dimmed snippet underneath. The **Undetermined** group omits the
   per-row unit cell.
5. **Cross-file navigation.** Clicking a site row jumps the editor to
   `(file, line, column)`. Unlike Scope / Diagnostics rows which always
   live in the active document, interaction sites can target a *different
   file*, so the click handler opens it first.
6. **Filter / scope.** The wire response covers the whole workset; the
   panel does no extra filtering. Use the CLI `--file` / `--scope` flags
   when a name is reused in different files or routines and a tighter
   view is wanted.

See [panel-info.md](panel-info.md) for how the Interactions section fits
into the broader panel layout (cursor-following debounce, fold state,
absence-glyph dimming).

## Architecture

- `core/interactions.py` — the engine. Public entry
  `collect_interactions(workset, symbol, *, file=None, scope=None,
  scale=False) -> SymbolReport`. Dataclasses: `InteractionPoint`,
  `Conflict`, `SymbolReport`. CLI-agnostic.
- `core/ts_checker._build_ctx` — shared `Ctx` construction, reused so the
  `check` pass and `collect_interactions` see the same scoped unit
  tables, signatures, and field units.
- `core/symbols.py` — `X001` is registered in `CODES` (severity `ERROR`,
  short text "conflicting unit claims across a symbol's use-sites").
- `lsp/interactions.py` — the LSP handler. Resolves the cursor symbol,
  serialises the report.
- `cli.py` — the `interactions` subcommand (parser + `_run_interactions`
  formatting).

A cheap byte-level gate skips files whose source doesn't mention the symbol
at all, so a whole-workset query (especially from the panel) doesn't build a
`Ctx` per file.

## Out of scope

- **Whole-workset "audit every symbol" sweep.** One symbol per invocation.
  An always-on X001 over the whole tree risks noise + perf cost and would
  need cross-file machinery to disambiguate at scale.
- **Derived-type field members** (`o%x`) as the queried symbol. Reads of a
  scalar variable only.
- **Solving through intrinsics** other than the transparent passthroughs
  `resolve_unit` already handles.

## Open questions

- **Whole-workset surface.** Whether to add a `dimfort/diagnostics`-style
  push of *every* X001 across the workset (today X001 is on-demand only),
  and whether that would interact badly with already-noisy unannotated
  codebases.
- **Provenance for `use` renames.** Whether the `InteractionPoint` payload
  should grow an optional `viaModule` / `originModule` pair so the panel
  can show "uses `dzfice` from `lscp_main` (imported as `dzice`)" when a
  symbol is touched via a renaming `use` clause. Today the kind/unit are
  reported but the rename source isn't.
- **Power operands.** `**` currently contributes no equality constraint
  on either side. The exponent must be dimensionless, but the base could
  in principle propagate a unit when the result unit and exponent are
  known — worth weighing against the false-positive risk.

## Testing

`tests/unit/test_interactions.py`, inline-Fortran fixtures (house style):
producer/consumer/sibling/call-arg constraints; the additive-term-with-
coefficient shape (the canonical `{1}` vs `{1/K}` conflict from the
benchmark workspace); the `invtau + invtau_e` shape; unknown-stays-unknown
(no false conflict); scope disambiguation; and a clean symbol with
agreeing constraints (no X001).
