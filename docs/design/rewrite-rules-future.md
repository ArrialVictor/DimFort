# Future U002 rewrite rules

The rewrite detector (`src/dimfort/core/rewrite.py`, spec
`unit-comment-delimiters.md` §12) ships exactly one rule in 0.2.2:
**digit-suffix → caret exponent** (`m2` → `m^2`,
`kg/m3` → `kg/m^3`). Two further rule classes were considered and
deliberately deferred. This doc preserves the design analysis so
they can be picked up later without restarting from scratch.

## Guiding principle (from spec §12.5)

> Each new rule is evidence-driven, not speculative.

A rule earns its place when real-world adopter feedback shows the
specific unparseable shape it would fix is recurring. Until then,
shipping a rule means broadcasting one team's guess about
"what users probably meant" into every other project's UX, with
no recourse if the suggestion turns out misleading.

## Candidate rule 1 — separator swaps

**Targets**

- `kg.m` → `kg*m` (period-as-product, common in
  paper-style notation)
- `÷` → `/` (Unicode division sign, copy-pasted from prose)
- `·` → `*` (Unicode middle dot, same)
- `μ` → `u` (Unicode mu, when the project's units table doesn't
  have the SI-style entry)

**Cost**

Low. One regex (or character map) per swap; idempotent because
the targets are all single-character substitutions whose outputs
don't re-match the pattern.

**Risks**

- The period in `kg.m` could be a literal period in a hand-written
  comment ("`! force kg.m units per second"). The rewriter only
  fires after the unit parser rejected the string, so the
  false-positive rate is bounded by "captured text that already
  failed to parse" — much narrower than scanning prose.
- Unicode swaps are context-free (no ambiguity), but the unit
  parser may already accept some of them via the unit table's
  alias mechanism; check before shipping.

**Open questions**

1. Do we ship the four targets as a single rule (one regex pass)
   or as four independent rules in the `RULES` tuple? Single-rule
   keeps the pipeline shorter; independent rules let users disable
   specific swaps via a future per-rule config knob.
2. Should the substitution be paired with a unit-table lookup
   that prefers the SI-canonical equivalent (`µ` → `u` → table
   alias)? Or stay purely syntactic and let the parser do the
   table lookup afterward? Probably the latter — keeps rules
   testable in isolation.

**When this earns shipping**

After ≥5 real-world adopter U002 reports include period-product
or Unicode-prose shapes. Until then it's speculation.

## Candidate rule 2 — typo correction against the unit dictionary

**Targets**

- `metre` → `m`, `meter` → `m`
- `kilogram` → `kg`, `gram` → `g`
- `second` → `s`
- Edit-distance fallback against the units table for typos like
  `kg/sceond` → `kg/s` or `Pasal` → `Pa`.

**Cost**

Multi-day. Requires:
- A curated synonym table (full English unit names → SI symbols).
- An edit-distance algorithm with a tunable threshold (Levenshtein
  ≤ 2? bounded by token length?).
- Tie-breaking when multiple candidates score equally close.
- Tests for "correctly refuses to suggest when the typo is too
  far" (preventing `zz` → `kg` style nonsense).

**Risks**

Higher than the other candidate rules.

- Edit-distance with no semantic context can produce confidently
  wrong suggestions ("`ks`" → "`kg`"? or "`m/s`"?). The
  self-correcting principle (spec §12.3) catches it but adds a
  diagnostic cycle the user has to discard.
- A poorly-tuned threshold blocks legitimate odd-but-correct unit
  names (project-local extensions in the `[units]` table).
- Synonym tables can have locale issues (`metre` vs `meter` —
  ship both? prefer one?).

**Open questions**

1. Synonym table source: hand-curated, generated from SI units
   docs, or sourced from `pint` / `siunitx` packages? Each has
   licence and stability trade-offs.
2. Edit-distance threshold: fixed (≤ 2) or proportional
   (`≤ len(input) / 4`)? Worth a small benchmark on real data.
3. Should typo correction skip when the input contains
   project-local identifiers (extending the units table)? Or
   trust the user — if the table has `hPa` and the input is
   `hPas`, the suggestion is `hPa`.

**When this earns shipping**

After ≥3 real adopter requests "I wrote `metre` and DimFort
didn't tell me what I meant." Typo correction is genuinely
expensive UX surface — speculation cost is high if we get it
wrong.

## Adjacent: per-rule disable knob

If both candidate rules ship, users may want per-project
disable controls — e.g. "we have a `kg.m`-style internal
convention; don't suggest the swap." A `[parser.rewrite]` table:

```toml
[parser.rewrite]
disabled = ["separator-swap", "typo-correction"]
```

The `RULES` tuple already supports this (each rule is a labelled
callable); the loader just filters by name. Out of 0.2.2.

## Tracking

Add a real-world data point here every time an adopter U002
report would be helped by one of these candidates. Five entries
per rule = green light to implement.

- (none yet — 0.2.2 just shipped)
