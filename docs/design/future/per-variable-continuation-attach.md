# Per-variable continuation-line attachment

**Status:** **0.2.7 design pass complete; implementation pending.**
Drafted 2026-06-15 from the late-cycle method-triangulation finding
that surfaced an attach-rule issue larger in empirical payoff than
any single lexer-flag work. File stays in `future/` until
implementation lands and is promoted to `shipped/`.

Sibling notes:
- [`permissive-unit-lexer.md`](permissive-unit-lexer.md) — the
  lexer flags ship in the same release; without this attach-rule
  change, one corpus's adoption caps at ~60 % regardless of lexer
  perfection.
- [`../shipped/unit-comment-markers.md`](../shipped/unit-comment-markers.md)
  — the orthogonal extraction-side companion.

## 1. Problem

Real climate codebases declare related variables on consecutive
continuation lines of a single declaration, with the **per-variable
unit annotation on each line**:

```fortran
REAL(wp), POINTER, CONTIGUOUS :: &
  &   acdnc(:,:,:),        & !! cloud droplet number concentration  [1/m**3]
  &   cape    (:,:),       & !! convective available energy         [J/kg]
  &   cloud_num(:,:),      & !! 2D cloud droplet number concentration [1/m**3]
```

Each `!!` annotation describes the variable on its own physical
line — `[1/m**3]` for `acdnc`, `[J/kg]` for `cape`, `[1/m**3]` for
`cloud_num`. The lexer reads each unit cleanly. The current attach
step throws all of them away.

### 1.1 Current behaviour (`src/dimfort/core/attach.py:16-30`)

Today the attach rules are:
- **POST** (`!<`, `!`, `!!` per project config) on any line in
  `[decl.line_start, decl.line_end]` attaches **to all of
  `decl.names`**.
- **PRE** (`!>`, `!!`) walks forward through a contiguous PRE block;
  the annotation attaches to the declaration whose `line_start`
  equals `block_end + 1`.
- **U010** fires when a POST annotation sits on an *intermediate*
  physical line of a `&`-continued declaration (strictly between
  `line_start` and `line_end`). The annotation is **rejected**.
- **U006** is the parallel reject for the `!!`-as-POST style.

The U006/U010 reject exists because today's data model carries
only `(line_start, line_end, names)` — no per-name positions. An
attach-all rule on an intermediate line would associate
`[1/m**3]` with `acdnc`, `cape`, AND `cloud_num` (wrong); refusing
is the conservative choice given the missing data.

### 1.2 Empirical payoff (6-corpus method-triangulation, 2026-06-14)

Counts below are full-corpus sweeps, not cherry-picked modules.
Each corpus is named anonymously; the aggregate base is **~7,300
files / ~3.95 MLoC across 6 distinct convention lineages**.

| corpus | files | LoC (k) | strict matches | intermediate-cont hits | % of corpus rejected |
|---|---:|---:|---:|---:|---:|
| Corpus A | ~2,095 | ~793 | 1,806 | 50 | 2.8 % |
| Corpus B | 95 | 150 | 293 | 2 | 0.7 % |
| Corpus C | 701 | 369 | 896 | 2 | 0.2 % |
| Corpus D | 1,366 | 480 | 2,593 | 59 | 2.3 % |
| Corpus E | 1,154 | 890 | 3,567 | 217 | 6.1 % |
| **Corpus F** | **1,889** | **1,270** | **3,626** | **1,407** | **38.8 %** |
| **aggregate** | **~7,300** | **~3,952** | **12,781** | **1,737** | — |

Verification: `dimfort check` run on top-offending file per corpus
matched the per-line predictions within ~1 %. Without this rule
change Corpus F adoption is capped at ~60 %; with it, the lexer
flags' empirical payoff lands intact. ~1,700 net annotations
across the 6 corpora become attachable (vs ~120 lost today).

**Convention-lineage diversity.** Corpus B's smallest file count
(95) reflects a focused single-component codebase, not a thin
sample — its 150 kLoC is dense biogeochem code with per-variable
unit annotations throughout. Corpus F's high rejection rate is a
property of its convention (per-line annotation under multi-line
continuation), not a sampling artefact: rerunning the measurement
on any random F90 subset of Corpus F reproduces the ratio within
~1 %. The cross-corpus spread (0.2 % to 38.8 %) is the conventions
talking, not measurement noise.

## 2. The new rule

> An annotation on physical line N attaches to the variables whose
> declaration tokens *end* on line N.

Concretely:

```fortran
REAL :: foo, bar, &   !! [m/s]   ← attaches to foo AND bar
        baz,      &   !! [K]     ← attaches to baz
        qux           !! [Pa]    ← attaches to qux (today's behaviour preserved)
```

The rule is local, deterministic, and matches the observed author
convention. It generalizes today's "POST on last line attaches to
all" to "POST on any line attaches to names ending on that line";
on a single-line decl the two rules coincide (all names end on the
same line).

### 2.1 What changes in the canonical attach behaviour

| input shape | today | new rule |
|---|---|---|
| Single-line single-name (`REAL :: x  !! [m]`) | attach `x` | attach `x` (unchanged) |
| Single-line multi-name (`REAL :: x, y  !! [m]`) | attach `x`, `y` | attach `x`, `y` (unchanged) |
| Continuation, last line only (`!! [m]` on final line) | attach all `names` | attach names whose end falls on the last line — for a typical decl that's the trailing tail |
| Continuation, per-line annotations (the finding case) | reject (U006/U010) | per-line: each `!! [X]` attaches to names ending on its line |
| Continuation, annotation on non-last intermediate line, others unannotated | reject (U006/U010) | attach to names ending on that line; the unannotated names land as U005 and surface a permanent migration-detection diagnostic (§6) |

### 2.2 Why per-name end-line and not start-line

A continuation-line declaration can carry multi-line dimension
bounds:

```fortran
REAL :: foo(SIZE_A, &
            SIZE_B), bar(:,:), &   !! [m/s]
        baz                       !! [K]
```

Here `foo` *starts* on line 1 but its declaration tokens *end* on
line 2 (after the closing `)` of its bounds). Attaching `!! [m/s]`
to "names starting on line 2" would give `bar, baz` (wrong —
`baz` is on line 3); "names ending on line 2" gives `foo, bar`
(correct). The end-line anchor matches the author's mental model:
the annotation describes the variable whose declaration just
finished writing itself.

## 3. Backward compatibility — hard switch

The new rule changes the meaning of every today-rejected
continuation-line annotation, and changes the variables that the
"POST on last line" case attaches to in some shapes (last-line
attachment now restricts to names ending on the last line, not
all names of the decl). This is a breaking change.

**Decision: hard switch.** The cost basis:
- Beta release line, ~zero known external users with persisted
  annotation files.
- The cost of a heuristic ("DimFort sometimes interprets this
  differently based on neighbours") would be permanent — every
  future user has to learn the rule plus its exceptions.
- The cost of a hard switch is bounded — the project's internal
  annotation files get a sweep, CHANGELOG carries a migration
  note, and a permanent migration-detection diagnostic (§6)
  catches the pattern at any future moment.

**Migration aids shipped alongside:**
- `docs/troubleshooting/continuation-attach-migration.md` — before/
  after examples drawn from the empirical corpora (anonymized).
- Permanent migration-detection diagnostic (§6) — runs forever, so
  any project that finds DimFort after 0.2.7 gets the same
  hand-holding the 0.2.7 migrators get.
- CHANGELOG entry under "Breaking changes" with a one-line
  pointer to the migration page.

There is **no opt-out flag**. A flag would convert a momentary
break into a persistent fork in the user's mental model — the
"slow drift" failure mode that the lexer-flag work explicitly
designs *for* (independent flags) but the attach rule explicitly
designs *against* (single deterministic rule).

## 4. Data model — per-name spans

### 4.1 Extend `DeclarationSite`

`src/dimfort/core/annotations.py:707` currently carries:

```python
@dataclass(frozen=True)
class DeclarationSite:
    line_start: int            # 1-based: line of type-spec
    line_end: int              # 1-based: last physical line of the statement
    names: tuple[str, ...]     # variable names declared, in source order
```

Add a parallel field with per-name span info:

```python
@dataclass(frozen=True)
class NameSpan:
    name: str
    start_line: int   # 1-based, first source-token char
    start_col: int    # 1-based
    end_line: int     # 1-based, last source-token char
    end_col: int      # 1-based

@dataclass(frozen=True)
class DeclarationSite:
    line_start: int
    line_end: int
    names: tuple[str, ...]                # unchanged — many call sites read this
    name_spans: tuple[NameSpan, ...]      # NEW: parallel to names; order matches
```

`names` stays for cheap iteration on call sites that don't care
about positions; `name_spans` is the source of truth for the new
attach rule.

### 4.2 Why richer than a minimum

A minimum implementation could pass a `name_lines: dict[str, int]`
mapping each name to its end-line. That solves the immediate
attach problem. The richer per-name span is preferred because:
- The scanner already does the boundary detection work to compute
  end-line (paren-aware, comma-split) — adding start_col / end_col
  costs nothing extra.
- 0.2.8's inference work will want column info for code-action
  insertion points, per-name hovers, position-aware completion,
  and per-name diagnostic squiggles (the current LSP renders the
  whole multi-line decl with a single underline).
- A single richer pass at scan time costs less than two thin
  passes a release apart.

`NameSpan` is frozen / value-typed so it composes into
`DeclarationSite`'s existing immutability story.

### 4.3 Scanner change — bound-aware tokenization

Name-boundary detection MUST respect parentheses, not just `&`.
Counter-example:

```fortran
REAL :: foo(SIZE_A, &
            SIZE_B), &
        bar
```

A naive `&`-split would think `foo`'s declaration ends on line 1
(at the first `&`); it actually ends on line 2 (after `)`). The
scanner needs real tokenization with a paren-depth counter:

```
state machine over the decl-statement byte stream:
  - track paren depth (push on '(', pop on ')')
  - track quote state (string-literal escapes)
  - a name's span ends when we hit the next comma at depth 0
    (or the end of the statement)
```

This is straightforward (tree-sitter handles paren/quote tracking
natively in the existing scanner), but it replaces what was a
single-line regex assumption. The implementation should land as a
new helper `_split_decl_names` in `annotations.py`, called from
`_ts_decl_names` (currently at `annotations.py:1080`).

## 5. PRE on multi-line declarations — conditional refuse

PRE annotations (`!>`, `!!`) sitting above a multi-line declaration
present a theoretical ambiguity the new rule alone cannot resolve:

```fortran
!! [m/s]
REAL :: foo, bar, &
        baz
```

Under the new rule, the annotation's *next-line target* is `foo`
and `bar` (names ending on the decl's first line) — but the
author almost certainly intended all three. Under the old rule, the
annotation would have attached to all three.

### 5.1 Empirical finding (2026-06-15 survey)

A focused survey across the six measured corpora counted
PRE-comment-blocks-above-multi-line-declarations and classified a
representative sample for author intent:

| measurement | result |
|---|---|
| Total PRE-on-multi-line-decl sites (6 corpora union) | 224 |
| Sample classified for author intent | ~80 sites |
| Of which were actual unit annotations | **0** |

The 80 sampled PRE blocks decomposed entirely into non-unit
content: section markers (`!!! 1D`, `!! ---`), doc headers
(`!! History :`), developer change-log notes (`!! Arsene
18-02-2014 ...`), routine descriptions, commented-out code.

The theoretical ambiguity the disposition was originally designed
to flag does not materialize empirically. Authors writing unit
annotations on multi-variable continuation declarations
**universally use POST per-line** (the pattern driving the 1,407
Corpus F U006/U010 sites in §1.2). PRE blocks above multi-line
decls are uniformly non-unit content across the surveyed corpora.

### 5.2 Disposition — refuse only when PRE block is a unit annotation

- **Single-line multi-name decl + PRE**: unchanged from today (PRE
  attaches to all of `names`). The decl is unambiguous on one
  line; the PRE intent is clear.
- **Multi-line decl + PRE comment block containing NO unit
  annotation**: ignored (today's behavior; non-unit comments are
  not DimFort's concern). This covers the entire empirical surface
  (224 sites across 6 corpora, all non-unit content).
- **Multi-line decl + PRE comment block containing a unit
  annotation**: **refuse with a clear diagnostic** (U024).

The third case empirically never fires (0 of ~80 sampled sites)
but the diagnostic stays as a **safety net**: if a future author
does write a PRE unit annotation above a multi-line decl, U024
catches the ambiguity instead of letting the new rule silently
resolve to first-line names only.

Implementation note: `attach.py` already detects whether a PRE
comment block contains a unit annotation (that's what extracts
`@unit{}` content from PRE blocks today). The U024 gate reuses
that existing detection — no new pipeline stage required.

### 5.3 Diagnostic body

> *"PRE annotation on a multi-line declaration is ambiguous under
> the per-line attach rule. Move to inline POST annotations on each
> continuation line:*
> ```fortran
> REAL :: foo, bar, &   !! [m/s]
>         baz           !! [m/s]
> ```
> *Or collapse the declaration to a single line."*

The message **shows the code shape** that resolves it. Concrete
examples in diagnostic text consistently outperform abstract
explanations.

The diagnostic gets a new code. Suggested `U024` (next free in the
U-series; final number assigned at implementation time).

### 5.4 Why not refuse unconditionally

The simpler disposition "refuse U024 on every PRE block above a
multi-line decl regardless of content" would emit 224 spurious
diagnostics across the surveyed corpora — falling on doc headers,
section markers, and change logs that users don't expect to be
unit-relevant. Low migration cost in aggregate, but noisy. The
conditional-refuse disposition stays precise (0 empirical fires)
while keeping the same safety-net guarantee against future
ambiguity. Reuses existing PRE-content detection, so the
implementation cost differential is small.

## 6. Permanent migration-detection diagnostic

The most common migration footgun:

```fortran
! Author wrote ONE annotation thinking it would attach to all:
REAL :: foo, bar, &   !! [m/s]
        baz, qux
```

Under the new rule, `!! [m/s]` attaches to `foo, bar`; `baz, qux`
are unannotated (U005). The author's mental model says "I annotated
the whole declaration"; reality says "you annotated half of it".

**Diagnostic** (new INFO-level code, suggested `U025` — final
number at implementation time): fires when ALL of:

1. The declaration has continuation lines.
2. An annotation sits on a non-last continuation line.
3. Subsequent continuation lines have no annotation AND their
   names are U005.

Message body:

> *"This annotation attaches to names on its line under the
> per-line attach rule. Variables on later continuation lines
> (`<X>`, `<Y>`, `<Z>`) are unannotated; if you intended to cover
> them, add per-line annotations on each line."*

The diagnostic stays in the codebase **permanently** — it serves
the 0.2.7 migration sweep AND every future author who hits the
footgun for the first time. The migration step itself is just:

```
dimfort check --only=U025
```

No separate migration script. No version-gated machinery to remove
later. The diagnostic IS the migration tool, and it remains useful
forever.

### 6.1 Severity choice

INFO, not warning, not error:
- Not error: the code is correct as written (no soundness break);
  the author may genuinely want partial annotation.
- Not warning: warnings carry the implication "fix or suppress";
  here the right action is often "do nothing" (the partial
  annotation was intentional).
- INFO: render as a faint underline (per LSP convention,
  suppressible per editor), surface in the panel diagnostics tab,
  count separately in coverage. The user can sort it into action
  or dismissal without pressure.

## 7. U006 + U010 retirement

The new rule eliminates the failure modes that U006 and U010
reported. Disposition:
- Mark both **retired in 0.2.7** in
  `docs/reference/diagnostic-codes.md`.
- Append a one-line link to the migration troubleshooting page.
- Numbers stay reserved (don't reuse).

Reducing the diagnostic surface reduces user misunderstanding.
Two codes for a single underlying limitation, where the limitation
itself is gone, is dead weight.

## 8. Test plan

The test corpus extends today's attach tests with the cases below.
Every test runs against tree-sitter (the production path) and
against a fixture that exercises the paren-aware tokenizer.

| # | shape | expected attach | expected diagnostics |
|---:|---|---|---|
| 1 | Single-line single-name `REAL :: x  !! [m]` | `x: m` | — |
| 2 | Single-line multi-name `REAL :: x, y  !! [m]` | `x: m`, `y: m` | — |
| 3 | Continuation, annotation on first line only (the hard-switch regression case) | names ending on first line | U025 for later-line names |
| 4 | Continuation, annotation on every line (clear per-line intent) | per-line; each annotation to its line's names | — |
| 5 | Continuation, annotation on some lines, others U005 | per-line for annotated lines | U005 for missing; U025 if pattern matches |
| 6 | Continuation, annotation on last line only (today's well-supported path, preserved) | names ending on last line | — |
| 7 | `!`-as-POST and `!!`-as-POST styles under new rule | per-line per project config | — |
| 8 | `&` inside array bounds (paren-aware boundary) | the variable whose bounds span the `&` ends at the `)`, not at the `&` | — |
| 9 | Type spec with explicit bounds (`REAL, DIMENSION(:,:) :: ...`) | per-line per name | — |
| 10 | Per-name array bounds with continuation (`REAL :: foo(:,:), bar(:)`) split across lines | per-name end-line drives attach | — |
| 11 | PRE on single-line multi-name decl | all `names` (preserved) | — |
| 12 | PRE unit annotation on multi-line decl (synthetic — empirically nonexistent) | rejected | U024 |
| 13 | PRE comment block above multi-line decl with NO unit annotation (doc header, section marker, change log) | ignored — comment is not DimFort's concern | — |
| 14 | Mixed PRE-and-POST on one continuation (degenerate, only when PRE is unit content) | per-line POST wins per name; PRE refuses with U024 | — |
| 15 | Empty continuation line (`&` alone) | no-op | — |

Each existing U006/U010 fixture maps to an **inverted-success
counterpart** under the new rule (the cases that were rejected
become attached). The test migration is mechanical: same input,
different expected output.

## 9. Implementation sketch

Three layered changes:

1. **Scanner change in `annotations.py`** — new helper
   `_split_decl_names(node) → list[NameSpan]` using tree-sitter's
   native paren/quote tracking. Populate `DeclarationSite.name_spans`
   at construction (`annotations.py:1278`).
2. **Attach-rule change in `attach.py`** — replace the
   `[line_start, line_end]` blanket attach with per-line lookup
   over `name_spans`. The U010 reject branch deletes; the U006
   parallel deletes. New code path computes "names ending on line
   L" via a small index built once per declaration.
3. **Diagnostics** — emit U024 (PRE on multi-line) and U025
   (migration-detection) in the attach pass. Both reuse the
   existing emission helpers; no new infrastructure.

Out of scope for the implementation pass (future work):
- Per-name hover and per-name LSP diagnostic squiggles — the data
  becomes available with `name_spans`, but the LSP wire format
  changes are 0.2.8 inference-cluster work.
- Code-action insertion-point use of `start_col` — same.

## 10. Performance considerations

The scanner already tokenizes the decl statement; adding paren
tracking is a constant-factor overhead on the same byte stream
(O(decl-bytes)). The attach pass goes from O(names × lines) to
O(names) per declaration with a one-time index build, which is a
small win on multi-line decls.

No new caches introduced. `DeclarationSite` is wider by one field
(a tuple of `NameSpan` value-records); memory cost is bounded by
declaration count × names-per-decl, dwarfed by existing AST sizes.

Benchmark gate: lex/parse throughput regression < 5 % on the
validation workspace; if observed, investigate.

## 11. Out of scope

- **Inline tag-style annotations** (`REAL :: x !@unit{m} !@unit{s}`)
  — the per-line attach rule does not change how multiple
  annotations on one line resolve. That remains "all attach to all
  names on the line"; resolving multi-annotation per name is a
  separate design (no demand observed).
- **Cross-statement annotation reuse** ("apply the previous
  annotation to the next decl") — not a real pattern in any
  surveyed corpus.
- **Per-name aliases / array-section annotations** (`!! foo(:,1) [m]`)
  — out of scope; the new rule operates at the variable-name
  level, not at the array-section level.

## 12. Open questions

1. **U024 / U025 numbering.** Suggested numbers; final codes
   assigned at implementation time to avoid colliding with any
   in-flight diagnostic work. Document numbering in the
   implementation PR.
2. **Name spans on derived-type component decls.** Today's scanner
   covers variable declarations; derived-type component lines have
   similar continuation shapes but are scanned through a different
   tree-sitter path. Out of 0.2.7 scope; track if real demand
   surfaces.
3. **PRE-only multi-line refusal vs auto-promotion.** U024 refuses
   when the PRE block IS a unit annotation; an alternative would
   be auto-promote to attach-to-all-names (matching today's
   PRE-on-single-line semantics). Rejected at design time as
   silent — the per-line rule's whole value is making intent
   visible — but worth revisiting if U024 generates noise during
   0.2.7 migration. Per the 2026-06-15 survey, U024 fires on
   exactly 0 sites in the surveyed corpora; this question is
   effectively dormant unless the empirical surface changes.

## 13. Decisions log

- **2026-06-14** — finding surfaced via method-triangulation across
  the 6 surveyed corpora. Empirical payoff identified; design pass
  scheduled before any implementation.
- **2026-06-15** — design pass: per-line attach rule adopted
  (annotation on physical line N attaches to variables whose
  declaration tokens end on line N), hard switch (no opt-out
  flag), richer data model (per-name spans, not just end-line),
  bound-aware tokenization required, permanent migration-detection
  diagnostic (U025), U006 + U010 retired.
- **2026-06-15** — implementation cost re-estimated at 3-4 days
  (up from ~1-2 days for the rule alone) due to richer data model,
  bound-aware tokenization, migration diagnostic, and test
  migration; payoff (~1,700 attachable annotations across 6
  corpora) justifies the depth.
- **2026-06-15** — PRE-on-multi-line disposition refined per a
  follow-up survey (224 union sites; ~80 sampled, 0 are unit
  annotations). U024 fires only when the PRE block actually
  contains a unit annotation, not unconditionally. The empirical
  fire rate is 0 across the surveyed corpora; the diagnostic
  remains as a safety net against future authors who do write the
  ambiguous shape. Reuses existing PRE-content detection in
  `attach.py`; no new pipeline stage.
