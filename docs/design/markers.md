# Marker derivation — design spec

Status: **implemented 2026-05-25** on the `scale-phase2` branch (markers
are now diagnostic-driven; the old re-derivation helpers are removed).
Drafted after an audit of the panel/hover marker code (see "The problem"
below); model + decisions (§6) settled, then built.

This document is the spec. Code follows the doc. If something here turns
out wrong during implementation, **update this doc first**, then the code.

## Scope and relationship to the other docs

The 🟢/🟡/🔴 marker is one concern split across three docs — this one
**centralises the derivation**; the other two stay authoritative for what
they already own:

- **[hover-ui.md](../hover-ui.md)** — *presentation*: which layouts fire,
  the glyph legend, where the marker sits in each row. Owns the **look**.
- **[panel-info.md](panel-info.md)** — *wire contract*: the
  `marker: "ok" | "warn" | "error"` field on `ExpressionNode`, and that it
  is pre-aggregated server-side. Owns the **protocol**.
- **markers.md** (this doc) — *derivation*: how the server computes a
  node's marker in the first place. Owns the **logic**.

`hover-ui.md` already states the guiding intent (its assignment-marker
note): the marker uses "the same source of truth the diagnostic checker
and the side panel use, so the hover and the Problems panel never
disagree." **That sentence is the whole design** — this doc makes it
literally true for *every* marker, not just the dimensional assignment
verdict it holds for today.


## 1. The problem

There are **two independent implementations of "is there a unit problem
here, and how severe":**

1. **Emission** — `ts_checker.check()` walks the tree, applies the
   dimension rules (`combine`/`power`) + `S001` (scale) + `S002` (offset),
   and produces `Diagnostic`s with positions and (post-override)
   severities. These are the squiggles and the Problems panel.
2. **Markers** — `server.py` (`_node_trace_mark`, `_homogeneity_short_
   marker`, `_verdict_marker`, `_scale_marker_emoji`, the
   `_build_expression_tree` aggregation, …) **re-walks** the tree and
   **re-derives** a 🟢/🟡/🔴 per node, trying to mirror (1).

The duplication is the root cause of a recurring bug class (catalogued in
the 2026-05-25 marker audit):

- **Drift on new checks.** Each check added to emission (S001, S002, and
  *soft-units next*) must be re-implemented in the marker layer, at every
  surface, with the right severity and propagation. S002 emission shipped
  with **no** marker support — every offset site shows a green circle
  under a yellow squiggle. (Audit finding 1.)
- **Re-encoding subtle rules.** The site-dependent offset algebra
  (`degC + dt` is legal at `+` but a mismatch at an assignment/relational)
  has to be duplicated in the marker code, where the shared
  `_homogeneity_short_marker` serves both a `+` hover and a relational
  hover and would false-positive. (Audit finding 2.)
- **Severity re-lookups.** Marker code re-calls `effective_severity(...)`
  with a hard-coded code, instead of inheriting the severity the
  diagnostic already carries. (Audit finding 3.)
- **Markers without diagnostics.** Relational/`max`/`min` are *not* S001/
  S002 emission sites, yet the relational hover overlays a scale marker —
  a coloured circle with no squiggle. (Audit finding 4.)

The Phase-1 marker work was a string of patches to keep the re-derivation
in sync (panel-root propagation, short-hover folding). That is the
treadmill this doc gets off.

**Not in scope / explicitly sound.** The hover/panel also display each
node's *resolved unit* (`play : kg/(m·s²)`), its rule ID (`R5.6`), the
tree structure, and autocast info. That is the unit-algebra *trace* and is
legitimately recomputed — you must resolve an expression to show its unit.
This doc changes **only the marker-severity derivation**, not the trace.


## 2. The model

A node's marker combines **two axes**, then aggregates over children:

```
base(node)      = 🟡 if the node's unit is unresolved (unannotated leaf,
                  unsupported intrinsic, partial resolution)
                  else 🟢
diag(node)      = worst severity of a UNIT-CONSISTENCY diagnostic owning
                  node (§3), mapped: error→🔴, warning→🟡, info→🟢
self(node)      = worst_of(base(node), diag(node))
marker(node)    = worst_of(self(node), *[marker(c) for c in children])
```

`worst_of` is the existing `🔴 > 🟡 > 🟢` order.

**Which diagnostics drive markers — the consistency family only
(decided 2026-05-25).** A marker means *"is the unit algebra consistent
here"*, so `diag` reads only the **unit-consistency family**:

```
{ H001, H002, H003, H004,   # dimension homogeneity (assignment / operand
                            #   / intrinsic-arg / call-arg mismatch)
  S001, S002,               # scale (factor) / affine offset
  S003 }                    # invalid @unit_affine_conversion directive
```

(`S003`, the verified-conversion error, joined the set when Phase 2c
shipped — a bad directive shows 🔴; a *valid* one emits no diagnostic and so
stays 🟢, exactly as the diagnostic-driven model intends.)

Deliberately **excluded**: `H010` and the `D1.x` rule markers (implicit
literal-cast — a *smell*; the units are made-consistent by the cast, not
inconsistent), and the `U0xx` family (annotation quality / info). They
still get squiggles; they just don't colour a circle, because a green
circle there is *correct* ("the algebra is consistent"). This keeps
markers meaning what they've always meant — adding S002, not turning
every LMDZ implicit-cast yellow. (The declaration row is the one place an
annotation-quality code drives a marker — `U002` "unparseable" → 🔴 — but
that is the *resolution* axis for a declaration, not an expression; §2.1.)
If H010/U0xx-in-the-panel is ever wanted, it's a deliberate later toggle,
not a side effect of this refactor.

**Single source of truth = the diagnostic stream** (plus the resolution
axis, which is *not* a diagnostic — an unannotated leaf is unknown, not
wrong). Everything else falls out:

- **New check → free markers.** S002, soft-units: emit a diagnostic, the
  marker follows. No marker code touched. (Dissolves finding 1.)
- **Subtle rules live once.** The site-dependent offset algebra is decided
  in emission; markers reflect whatever was emitted. (Dissolves finding 2:
  `degC + dt` emits nothing at `+`, so no marker; a relational `degC < tk`
  emits S002 *iff* we make relational an emission site — so the marker and
  squiggle agree either way.)
- **Severity is inherited.** `diag(node)` reads the diagnostic's own
  severity, already overridden by `finalize_diagnostics`. No re-lookup.
  (Dissolves finding 3.)
- **No orphan markers.** A marker can only be 🟡/🔴 from `diag` if a
  diagnostic exists there. (Dissolves finding 4 *by construction*: with no
  relational diagnostic, the relational marker is simply dimension-only —
  consistent. Emitting at relational/`max`/`min` is a separate future
  enhancement, not a prerequisite; see §6.1.)


## 3. Derivation, precisely

- **Resolution axis (`base`).** Reuse the existing resolve: a node with a
  `Unit`/wrapper is 🟢; `None` (unannotated, unsupported, partial) is 🟡.
  This is the *only* thing the marker still computes from the tree.
- **Diagnostic axis (`diag`).** Look up the file's **consistency-family**
  diagnostics (§2: `{H001,H002,H003,H004,S001,S002}`; see §4 caveat 1)
  that *own* the node (§4 caveat 2, tightest-enclosing); take the worst
  severity. Mapping: `Severity.ERROR → 🔴`, `WARNING → 🟡`,
  `INFO`/`HINT → 🟢` (never escalates). Non-family codes (`H010`, `D1.x`,
  `U0xx`) are skipped here. `off` diagnostics never exist (dropped in
  `finalize_diagnostics`), so suppression is free.
- **Aggregation.** `worst_of(self, children)` — the worst-of-children
  rule `panel-info.md` already promises and `_aggregate_marker` already
  implements; it stays, now fed by the unified `self`.

The assignment row keeps showing **no unit column** (it is a statement);
only its marker is meaningful — unchanged from `hover-ui.md`.


## 2.1 What gets a marker — the marker-bearing set

Today this is *implicit* — a node has a marker if some hover/panel handler
happens to render it — which is why coverage feels uneven (e.g. assignments
marked in one path, not another). The centralised rule:

> **A node is marker-bearing iff it is rendered as a row.** Every rendered
> row gets a marker from the §2 model; nothing else does. There is no
> fourth case.

The rendered rows, and how the model applies to each:

| Rendered row | `base` (resolution axis) | `diag` (covering diagnostics) |
|---|---|---|
| expression node (ident, literal, math, call) | 🟢 if its unit resolves, else 🟡 | H00x / S00x / D1.x owning this node |
| **assignment statement** | always rendered; no unit of its own → `base` = 🟢 | its homogeneity diagnostic (H001 / S001 / S002) |
| relational / IF / WHERE / DO-bound condition | as the expression it wraps | its operands' diagnostic |
| call-arg pairing row | 🟢 if actual resolves, else 🟡 | the arg-mismatch diagnostic |
| **declaration (scope-var) row** | 🟢 annotated / 🟡 unannotated / 🔴 unparseable | the unparseable case *is* a `U002` diagnostic |

**Two unifications fall out:**
- **Every assignment is marker-bearing, uniformly** — whether or not its
  RHS resolves (unresolved RHS → 🟡 via `base`-of-children, mismatch → 🔴
  via `diag`, clean → 🟢). No assignment is silently marker-less.
- **The scope-panel kind markers are the same model**, not a separate
  scheme: `annotated/unannotated/error` ≡ `🟢/🟡/🔴` = (annotation
  resolves) ∨ (a `U002` covers the declaration). So `_build_scope_vars`
  collapses into `marker_for` too.

**Not marker-bearing:** bare syntax — keywords, operator/punctuation
tokens, `then`/`do`/`call` — render no row and carry no marker (matches
`hover-ui.md`'s "no hover" cells).

**One deliberate exemption — the call-arg pairing rows.** The function/
subroutine call hover pairs each *formal* against its *actual* and marks
each row. These stay a **local per-arg `equal_dim` comparison**, not the
diagnostic-driven model, because `H004` is emitted on the *whole call*
(not per argument) and the checker emits *no* scale/offset diagnostic at
call-arg sites. So there is no per-arg diagnostic to read; a local
dimension check matches exactly what `H004` checks, while routing per-arg
markers through `compare()` would paint scale/offset rows with no backing
squiggle — the orphan-marker anti-pattern this design exists to kill. If
the checker ever emits per-arg (or scale/offset-at-call) diagnostics, this
surface folds into the model then.


## 4. Caveats (the non-obvious parts)

1. **Diagnostics must be available at hover/panel time.** They are
   computed by `check_files` for publishing but not currently kept for the
   on-demand handlers. Cache the last-computed `list[Diagnostic]` per URI
   (keyed like the publish path) and let hover/panel query it. The hover
   already re-resolves units; this adds a range lookup, not a second
   check. (If a doc is dirty/unpublished, fall back to a fresh check or to
   the resolution axis only — decide in §6.)
2. **Range → node mapping — assign to the *tightest-enclosing* node, not
   range-contains.** Diagnostics carry spans; markers are per-node. The
   naive "diagnostic range contains node → mark node" *over-paints*: an
   H002 spanning `a + b` would also paint `a` and `b` red, but they
   resolve fine on their own. Rule: **each diagnostic belongs to the
   single smallest AST node enclosing its range** (H002 → the `a+b` node;
   H001, which spans `lhs = rhs`, → the `assignment` node). `diag(node)`
   = the diagnostics *assigned to* `node`; the upward direction is then
   handled by worst-of-children aggregation, **never downward**. Worked:
   ```
   0.5 * (a+b) * c  🔴   (propagated up)
   ├── a + b        🔴   (H002 owns this node)
   │   ├── a        🟢   (not owned by the H002)
   │   └── b        🟢
   └── c            🟡   (unresolved, no diagnostic)
   ```
   Pin this with a test matrix mirroring the current per-node markers so
   the refactor is provably behaviour-preserving.
3. **Severity → glyph.** Only error/warning escalate. INFO (e.g. a future
   autocast-info, U020 `@unit_assume`) stays 🟢 so the panel isn't noisy.
4. **🟡-on-`expected` override (call-arg rows).** A call-argument node
   whose actual unit dimensionally differs from the callee's formal
   carries an `expected` annotation in both the trace hover and the
   panel payload. When that row would otherwise paint 🟢 — its own
   expression resolved cleanly and no diagnostic owns it — the marker
   demotes to 🟡. Rationale: the expression *is* clean here, but its
   consumer (the call signature) disagrees; flagging silently with
   `(expected …)` plus an unchanged 🟢 reads as "all fine here", which
   contradicts the `🔴` painted on the enclosing call by H004. The
   demotion is bounded — it only acts on rows that already paint 🟢
   AND carry an `expected` annotation — so it never overrides a
   diagnostic-owned 🔴 or a 🟡 from resolution. The hover-side rule
   lives in `_render_ast_tree` (`if extra_str and mark == "🟢": mark = "🟡"`);
   the panel-side rule lives in `_build_expression_tree` (`if expected
   and marker == "ok": marker = "warn"`). Both sites pull from the
   same call-signature lookup, so they can't disagree.
5. **Three glyphs for "no unit" — one meaning each.**
   - `?` — **unknown unit**. Reserved for nodes that *could* have a
     unit but don't yet: unannotated identifier, unsupported intrinsic
     return, partial resolution. Paints 🟡 on the resolution axis.
   - `-` — **no unit by structure**. Reserved for nodes that have no
     unit by design: assignment statements, relational expressions,
     subroutine calls (no return value). The
     `_NO_UNIT_NODE_TYPES` set in `expr_tree.py` is authoritative.
     Resolution-axis base is 🟢 (a clean assignment / subroutine call
     is not "unresolved"); markers come from the diagnostic axis +
     children. Surfaced identically by hover (`_render_ast_tree`) and
     panel (`_build_expression_tree`).
   - `(none)` — **empty section / sub-section header** only (e.g.
     `Scope: (none)`, `Imports: (none)`, "no declarations" body).
     Never used inside a tree row or for an individual variable; for
     unannotated declarations in scope/import sections, companions
     render `?`.
6. **🔵 — accepted via `@unit_assume`.** A statement-level
   `@unit_assume{<unit> : <reason>}` directive asserts a unit that
   the algebra can't derive (typically a non-rational exponent on a
   dimensioned base — empirical fits like Tetens, Magnus, Buck,
   Brandes2007). The checker emits a U020 INFO acknowledging the
   assumption; the row paints **🔵** and surfaces the mandatory
   reason as `(assumed: <reason>)` on the row tail (same column as
   `(expected …)`). The semantics:
   - **🔵 sits between 🟢 and 🟡** in the worst-of aggregation order
     (`error > warn > assumed > ok`). A 🔵 child propagates 🔵 to its
     parent unless a 🟡/🔴 sibling beats it; siblings on their own
     merits aren't suppressed.
   - **The directive short-circuits worst-of-children at the
     assumed node itself** (the assignment_statement carrying the
     `@unit_assume`). The whole point of the directive is "trust me
     on the unit; don't worry about the inside" — child markers
     (which often show 🟡 from unresolved leaves like `(-0.922)`)
     are not propagated up through that row. The assumption owns
     that row's verdict.
   - **Ownership is line-based**, not span-based: the U020
     diagnostic position sits at the `@unit_assume` token in the
     trailing comment, which is *outside* the assignment's
     tree-sitter span. The ownership rule matches a U020 against
     the smallest `assignment_statement` on the same line. Only
     `assignment_statement` nodes can own a U020 — the directive is
     statement-level.
   - **🔵 doesn't compete with 🟡/🔴.** If a consistency-family
     diagnostic also owns the node (e.g., the assumed unit
     disagrees with a *declared* LHS unit and H001 fires), worst-of
     paints 🔴/🟡 instead. The assumption never masks a declared-
     unit conflict.

## 5. Reconciliation with the existing docs

- **panel-info.md** — wire contract is **unchanged** (`marker: ok|warn|
  error`, pre-aggregated). Only the server-side derivation changes, so the
  3 companions are untouched. The "worst-of-children" sentence stays true.
- **hover-ui.md** — presentation is unchanged; its marker *semantics* were
  synced when this landed: the per-row 🟢/🟡/🔴 list is now diagnostic-
  driven (consistency family incl. `S001`/`S002`), the "source of truth"
  note points at the file's diagnostics, and the relational examples show
  🟡 (relational is not an emission site, for dimension *or* scale — e.g.
  `p > 0.0` no longer re-derives a 🔴).
- **scale.md** — unaffected; its S001/S002 *emission* is the source these
  markers now read. The forward-compat note there (soft-units is a
  severity/provenance layer over the same diagnostics) is exactly why
  diagnostic-driven markers pay off: soft-units markers come for free.


## 6. Decisions (resolved 2026-05-25)

1. **Finding 4 — relational/`max`/`min`: decoupled.** The refactor *alone*
   removes the inconsistency: with no relational diagnostic emitted, the
   relational hover shows a 🟡 marker (the orphan overlay disappears) —
   consistent by construction, no extra work. Note the build revealed
   relational is unemitted for **dimension too**, not just scale (`p >
   0.0` previously re-derived a 🔴 with no squiggle). *Emitting* H00x /
   S001 / S002 at relational + `max`/`min` is genuinely useful (comparing
   across dimensions / scales / frames is a real bug) but is a **separate
   future emission enhancement**, not a prerequisite for this refactor.
2. **Dirty-buffer fallback: read the cache (recompute later if needed).**
   As built, the markers read the last cached `WorksetResult`
   (`_last_result`, keyed by file), which the publish path refreshes on
   every change — so the dirty window is one debounced keystroke. A
   recompute-on-miss is the same single-file check the publish path runs;
   left as a refinement since the cache is current in practice.
3. **Granularity: tightest-enclosing ownership + a behaviour-preserving
   test matrix** (see §4 caveat 2). Diagnostics are assigned to their
   smallest enclosing node; aggregation handles upward propagation.


## 7. Migration

1. Add the per-URI diagnostic cache + a `marker_for(node)` that implements
   §2/§3 (resolution axis ∨ diagnostic-range lookup, then aggregate).
2. Re-express `_node_trace_mark` / `_homogeneity_short_marker` /
   `_verdict_marker` / `_scale_marker_emoji` over `marker_for` — or delete
   them where `marker_for` subsumes them. Keep `_build_expression_tree`'s
   aggregation (now trivial) and the wire tokens.
3. Regression: the existing marker tests (`test_panel_info.py`,
   `test_lsp_server.py`) must stay green; add the §6.3 matrix. Dimension
   and S001 markers must be byte-identical; S002 markers appear for free.
4. Update the two `hover-ui.md` paragraphs (§5).
