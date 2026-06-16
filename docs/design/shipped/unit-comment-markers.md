# Unit-comment markers — STRUCT / nonSTRUCT under `[parser.unit_comments]`

**Status:** **Shipped in 0.2.7.** Drafted 2026-06-14 alongside the
permissive-unit-lexer design pass; rebuilt 2026-06-15 with the unified
six-key namespace; landed 2026-06-16 with the namespace migration +
nonSTRUCT filter implementation. This note describes what actually
shipped; the precursor `future/unit-comment-skip-delimiters.md` is
retired.

Sibling design notes:
- [`permissive-unit-lexer.md`](../future/permissive-unit-lexer.md) —
  how DimFort *parses* the substring once extracted.
- [`unit-comment-delimiters.md`](unit-comment-delimiters.md) — the
  pre-0.2.7 delimiter machinery (0.2.2). The configurable
  `{open, close}` shape preserved; the surrounding namespace replaced.

## 1. What this design solves

Two coupled adoption questions:

1. **Project-level filters** — climate-Fortran inline comments
   routinely carry parenthesized text that is NOT a unit:
   citations `(see Schmidt 2002)`, year-only `(2002)`, dimension
   hints `(ncol, nlay)`, INTENT markers `(in)` / `(out)`. With a
   parens-style unit delimiter (`{open="(", close=")"}`), these all
   look like unit slots and produce U002 noise or worse — silent
   misparse.
2. **Per-site author markers** — sometimes a single inline comment
   contains a paren / bracket the author knows isn't a unit, and a
   project-level filter would be overkill. `@nonunit{...}` is the
   author-typed marker DimFort drops silently.

Both surfaces share one mental model: each extraction family
(`unit` / `unit_assume` / `unit_affine`) is paired with a nonSTRUCT
filter list (`nonunit` / `nonunit_assume` / `nonunit_affine`), and
what DimFort actually extracts is the set subtraction
`STRUCT \ nonSTRUCT`.

## 2. Surface — `[parser.unit_comments]`

Six keys grouped into three pairs. Entry shape mirrors the matching
STRUCT entry, plus an optional `regex` predicate.

| key                  | entry shape                                  | default |
|---|---|---|
| `unit`               | `{open, close}`                              | `[{open="@unit{", close="}"}]` |
| `nonunit`            | `{open, close, regex?}`                      | three shipped patterns (see §4) |
| `unit_assume`        | `{open, close, sep}`                         | `[{open="@unit_assume{", close="}", sep=":"}]` |
| `nonunit_assume`     | `{open, close, sep?, regex?}`                | `[]` |
| `unit_affine`        | `{open, close, sep}`                         | `[{open="@unit_affine_conversion{", close="}", sep="->"}]` |
| `nonunit_affine`     | `{open, close, sep?, regex?}`                | `[]` |

### 2.1 Why the STRUCT / nonSTRUCT split

A regex-only filter inside each STRUCT row would not express the
per-site marker case: the author needs to write `@nonunit{...}` in
*source code*, not in `dimfort.toml`. The split encodes two genuinely
different mechanisms — author-intent markers in source vs
project-level filter patterns — sharing the same delimiter shape.

A pure-config alternative (single regex predicate per STRUCT entry)
was considered and rejected during the 2026-06-14 design pass.

### 2.2 Optional fields

- **`regex` (NonSTRUCT)** — predicate matched against the inner
  content (whitespace-stripped for `nonunit`; full content for the
  structured filters). Encode separator-specific filtering by writing
  the separator literal into the regex
  (`regex = "^0\\s*:.*"` to filter `unit_assume` directives whose unit
  part is "0").
- **`sep` (NonSTRUCT)** — when present, targets a specific
  `{open, close, sep}` triple. When absent, the rule degenerates to a
  bare-pair scan that filters all matching `{open, close}` regardless
  of separator content.

## 3. Filter semantics — dead-range overlap

For each comment body, each nonSTRUCT list produces a set of "dead
ranges": `[start, end)` half-open spans where the matching nonSTRUCT
pattern's `{open, close}` (+ optional regex predicate) accepts. A
STRUCT candidate whose span overlaps any dead range is silently
dropped before reaching the unit lexer. No diagnostic.

**Per-family.** `nonunit` only filters `unit` candidates;
`nonunit_assume` only filters `unit_assume` candidates;
`nonunit_affine` only filters `unit_affine` candidates. No cross-
family bleed.

**Precedence.** nonSTRUCT silently wins over STRUCT — when a
candidate matches both, it's dropped without a diagnostic. The
silence is intentional: a config that fires would mean the project
declared both that the shape IS a unit slot AND that it ISN'T, which
the project meant the latter by writing the nonSTRUCT entry.

## 4. Shipped `nonunit` defaults

Three patterns ship enabled by default. Each targets a shape that
empirically appears across the 6-corpus survey and almost never
represents a real unit annotation.

| pattern                                           | targets                            | empirical union (6 corpora) |
|---|---|---|
| `{open="@nonunit{", close="}"}`                   | Per-site author marker             | n/a (new surface) |
| `{open="(see ", close=")"}`                       | Citation prefix `(see Smith 2002)` | ~793 hits |
| `{open="(", close=")", regex="^\\d{4}$"}`         | Year-only parens `(2002)`          | ~1,375 hits |

The defaults only fire when a project configures parens or brackets
as a `unit` delimiter — projects on the canonical `@unit{...}` form
see no behaviour change from these defaults shipping enabled.

Opt out: `nonunit = []` (explicit empty list). Opt out of one pattern:
declare your own list verbatim minus the unwanted entry.

`nonunit_assume` and `nonunit_affine` ship empty by default — they're
the surface for projects that hit a real assume / affine false
positive, not pre-empted filters.

## 5. Pipeline order

For each comment body:

```
extract unit-comment delimiter content
  ↓
compute dead_ranges over nonunit / nonunit_assume / nonunit_affine
  ↓
run _select_unit / _select_assume / _select_affine
  with per-family dead_ranges → drop overlapping captures
  ↓
unit lexer
```

The `dead_ranges` computation is O(N) over the comment body length
per nonSTRUCT pattern. On the canonical canonical-`@unit{}`
annotation surface, each shipped default `nonunit` is one `text.find`
that returns -1 at the first attempt and exits — negligible cost.

Per-corpus perf check on a real-world Fortran workset (~2,400 files)
at the Track A.1 checkpoint: no measurable engine regression vs the
0.2.7 baseline.

## 6. Cache invalidation

`CHECKER_OUTPUT_VERSION` bumped 9 → 10 for the 0.2.7 release: the
shipped `nonunit` defaults now drop captures that a pre-0.2.7 cache
entry would replay as live annotations. All six pattern lists
contribute to the `patterns_fingerprint` folded into
`ProjectionKey`, so changing any list invalidates affected per-file
projection cache entries naturally.

## 7. Migration from pre-0.2.7

Flat keys at `[parser]` are warn-and-ignored:

| pre-0.2.7 key (under `[parser]`)     | 0.2.7 key (under `[parser.unit_comments]`) |
|---|---|
| `unit_comment_delimiters`            | `unit`         |
| `unit_assume_comment_delimiters`     | `unit_assume`  |
| `unit_affine_comment_delimiters`     | `unit_affine`  |

The hard switch (no opt-in flag, no transitional alias path) was
adopted for the same reasons the 0.2.7 per-variable continuation-
attach migration was a hard switch: beta release line + bounded user
surface + permanent migration-detection beats a heuristic.

Detailed cookbook + worked examples at
[`docs/troubleshooting/unit-comments-migration.md`](../../troubleshooting/unit-comments-migration.md).

## 8. Out of scope

- **Cross-family filters** — `nonunit` does not filter
  `unit_assume` captures. By design: a per-family override scopes the
  filter cleanly; a cross-family override would force a configuration
  language change (which-family-does-this-filter-target field) for a
  scenario no surveyed corpus surfaces today.
- **Diagnostic for filtered captures** — drops are silent. A
  diagnostic-on-drop would be noise on the shipped defaults (which
  fire 0 times on the canonical `@unit{...}` configuration) and add
  cost to the hot scan path. A future `dimfort check --verbose` flag
  could surface drop counts at the report level.
- **`STRUCT \ nonSTRUCT` overlap surface beyond delimiters** —
  filtering on resolved unit dimensions (e.g. "drop captures that
  parse to dimensionless") is a checker-layer concern, not an
  extraction-layer concern. The split keeps the two layers
  independent.
- **Generalized capture-group regex extraction** — relax-mode (the
  capture-group-shaped extraction surface) is parked for 0.2.8 per
  the design-pass empirical case: `{open, close}` already covers
  ~93 % of unit-bearing comment lines on the surveyed corpora;
  capture-group surfaces close the residual ~7 %.

## 9. Test coverage

- `tests/unit/test_unit_patterns.py` — runtime pattern types
  (`NonUnitPattern`, `NonStructuredPattern`, `dead_ranges`,
  `overlaps_any`) + compile helpers.
- `tests/unit/test_nonunit_filter.py` — end-to-end `scan_text`
  semantics: shipped defaults drop citation / year shapes when parens
  are configured as `unit`; canonical `@unit{}` config sees no
  behaviour change; per-site `@nonunit{...}` drops overlapping
  bracket matches; per-family isolation (`nonunit` does NOT filter
  `unit_assume`).
- `tests/unit/test_config.py` — `[parser.unit_comments]` parse path:
  six-key shape; legacy-flat-key warn-and-ignored; regex validation
  drops malformed entries; empty `nonunit` opts out.
- `tests/integration/test_cli_check.py` — U021 pattern-conflict
  behaviour preserved across nested-namespace migration.

## 10. Decisions log

- **2026-06-14** — STRUCT / nonSTRUCT split adopted; regex-only
  alternative rejected (per-site marker case requires source-side
  surface).
- **2026-06-14** — six-key nested namespace finalized; flat keys
  scheduled for hard removal.
- **2026-06-15** — set-subtraction conceptual framing documented;
  precedence rule (nonSTRUCT silently wins) locked.
- **2026-06-15** — three shipped `nonunit` defaults selected on the
  6-corpus empirical case.
- **2026-06-16** — implementation landed: nested
  `UnitCommentsConfig` dataclass, runtime `NonUnitPattern` /
  `NonStructuredPattern` + `dead_ranges` / `overlaps_any` helpers,
  per-family dead-range filter inside
  `_select_unit` / `_select_assume` / `_select_affine`. Cache key
  version bumped 9 → 10. Migration doc published.
