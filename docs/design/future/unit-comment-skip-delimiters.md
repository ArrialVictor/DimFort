# Skip delimiters — author-declared non-unit parens (FUTURE)

**Status:** future feature. Drafted 2026-06-13 alongside
[permissive-unit-lexer.md](permissive-unit-lexer.md). Sibling design:
that note covers how DimFort *parses* the substring once extracted;
this note covers how DimFort decides **which parens not to extract in
the first place**.

## 1. Problem this solves

Inline comments in climate-Fortran codebases routinely contain
parenthesized text that is **not** a unit:

```fortran
REAL :: foo  !! albedo coefficient (see Schmidt et al., 2002) (unitless)
REAL :: bar  !! latent heat (after Bolton 1980) (J kg^{-1})
REAL :: baz  !! cell area (m^2) (Hadley grid)
```

The author's intent is unambiguous to a human: the rightmost paren
(or the only one carrying a unit-shaped string) is the unit. To
DimFort with `unit_comment_delimiters = [{open=" (", close=")"}]`,
all three parens look like unit slots — producing `U001 More than
one @unit on one line` or worse, mis-parsing a citation as a unit.

Three approaches were considered:

| approach | cost | author surface | failure mode |
|---|---|---|---|
| Relax-mode heuristic ("is this content unit-shaped?") | low | none | can eat real units in corner cases |
| Per-comment escape syntax (`\(...\)`) | medium | source rewrite required | requires touching every file |
| **Configured skip delimiters** | low | one TOML block | none if patterns are explicit |

Configured skip delimiters win on **author-controllable**, **no source
rewrites**, and **no false-positive risk** beyond what the project
explicitly opts into.

## 2. Three-corpus survey (2026-06-13)

Counts of trailing-paren content matching each candidate skip pattern,
out of 35,687 total trailing-paren content lines across Corpus A (18,161)
+ ORCHIDEE (8,076) + NEMO (9,450).

| skip pattern | Corpus A | ORCHIDEE | NEMO | total |
|---|---:|---:|---:|---:|
| starts `(see …)` | 52 | 43 | **596** | **691** |
| starts `(cf …)` / `(cf. …)` | 9 | 0 | 43 | 52 |
| starts `(after …)` | 5 | 7 | 5 | 17 |
| starts `(from …)` | 31 | 39 | 23 | 93 |
| contains "et al" | 53 | 14 | 82 | 149 |
| year-only `(2002)` | 260 | 175 | 690 | **1,125** |
| any 4-digit year inside | 567 | 239 | 922 | 1,728 |
| `Author, year` shape | 58 | 23 | 88 | 169 |
| uppercase tags `(STATIC,OMP_CHUNK)` etc. | 365 | 0 | 24 | 389 |

**Observations:**

- **NEMO is `(see ...)`-dominated** — 596 sites, more than 10× the
  citation noise of either other corpus. NEMO authors cross-reference
  heavily inside source comments.
- **Year-only parens are universal** — 1,125 sites across the three
  corpora — and shape-defined (`^[12][0-9]{3}$`), so they call for
  the content-regex form (§3.2) rather than a fixed open prefix.
- **Corpus A has a distinct non-unit class** — 365 uppercase OpenMP /
  threading tags (`(STATIC,OMP_CHUNK)`, etc.). Shape-defined
  (all-caps + commas).
- ORCHIDEE has the *least* citation noise but the most LaTeX /
  unit-content lexical issues. The two surveys cover non-overlapping
  pain.

Total approximate non-unit-paren footprint across all three:
**~3,200+ sites** addressable by skip delimiters.

## 3. Design

### 3.1 TOML shape — mirror `unit_comment_delimiters`

```toml
[parser]
unit_comment_skip_delimiters = [
  # Prefix-based (open includes a marker word):
  { open = "(see ",   close = ")" },
  { open = "(cf ",    close = ")" },
  { open = "(cf. ",   close = ")" },
  { open = "(after ", close = ")" },
  { open = "(from ",  close = ")" },

  # Content-regex (open/close generic; predicate on content):
  { open = "(", close = ")", content = "^[12][0-9]{3}$" },   # year-only
  { open = "(", close = ")", content = "^[A-Z_,]+$" },        # OMP/threading tags
]
```

Same `{open, close}` keys as
[`unit_comment_delimiters`](../shipped/unit-comment-delimiters.md). An
optional `content` key adds a predicate the captured content must
match for the skip to apply.

Users who already configured `unit_comment_delimiters` know the model
on sight.

### 3.2 Two flavors — prefix-based and content-regex

Both are expressed in one TOML shape, but they cover distinct cases:

| flavor | when to use | example |
|---|---|---|
| **Prefix-based** (no `content` key) | author convention starts every non-unit paren with a marker word (`see`, `cf`, `after`) | `{open="(see ", close=")"}` |
| **Content-regex** (`content` key present) | the non-unit is shape-defined, not prefix-defined (year alone, uppercase tag, …) | `{open="(", close=")", content="^[12][0-9]{3}$"}` |

The regex grammar is restricted (see §6 OQ2): anchors (`^`, `$`),
character classes (`[A-Z]`, `[0-9]`), basic quantifiers (`*`, `+`,
`?`, `{n,m}`), no backreferences, no lookaround. Just enough to
express shapes without becoming a foot-gun.

### 3.3 Algorithm — compose order with `unit_comment_delimiters`

```
Given an inline comment c:
  1. Extract every span of c matching any unit_comment_delimiter.
     → candidate_set
  2. Remove from candidate_set every span that also matches any
     unit_comment_skip_delimiter (open/close match AND, if present,
     content-regex match against the captured content).
     → filtered_set
  3. If |filtered_set| == 1 → that's the unit slot.
  4. If |filtered_set| > 1 → fall to last-paren rule (or fire U001
     if [parser] sets multi_unit_resolution = "strict").
  5. If |filtered_set| == 0 → comment carries no unit annotation.
```

Step 2's "also matches" requires *both* the delimiter pair *and* the
content predicate (when present) to match the same span. A skip
entry with no `content` key matches every span whose open/close
pair matches — i.e., it's a prefix-only rule.

**Skip scope is project-global** — a skip delimiter applies to spans
extracted under any configured `unit_comment_delimiter`, not just the
delimiter with the matching syntax. This keeps the mental model
simple. (Reconsider if a use case appears where per-delimiter skips
matter — none identified today.)

### 3.4 Composition with other features

- **Independent of [permissive-unit-lexer](permissive-unit-lexer.md)
  flags.** Skip delimiters operate on the *extraction* axis; lexer
  flags operate on the *parsing* axis. They commute — skip first
  (subtract non-units), then parse what remains.
- **Independent of relax-mode** (when relax-mode ships). Skip
  delimiters subtract spans by author-declared convention; relax-mode
  can still apply downstream filters on the residue. In practice,
  skip delimiters reduce relax-mode's job to the genuinely-ambiguous
  cases relax-mode is *for*.
- **Pure-additive in the compatibility matrix.** No mutual exclusion
  against any other parser option.

## 4. Diagnostic

```
U031 unit comment skipped — matched skip delimiter
```

Severity **info** by default; off in non-verbose. Emitted on every
span subtracted by step 2 of §3.3, so authors can audit that the
skip-list is doing what they expect when they enable verbose mode
or when debugging an unexpected `U001` / mis-extraction.

Example:

```
constantes_var.f90:208: info: U031 skipped (see Schmidt et al., 2002)
  — matched {open="(see ", close=")"}; remaining candidates: 1
```

## 5. Default skip preset

Ship a small default list, commented out, that users can opt into
by uncommenting. Captures the highest-volume universal patterns
without surprising teams who legitimately wrote `(2002 K)`
somewhere:

```toml
[parser]
# unit_comment_skip_delimiters = [
#   { open = "(see ",   close = ")" },                              # ~691 across Corpus A+ORC+NEMO
#   { open = "(cf ",    close = ")" },                              # ~52
#   { open = "(cf. ",   close = ")" },
#   { open = "(after ", close = ")" },                              # ~17
#   { open = "(from ",  close = ")" },                              # ~93
#   { open = "(",       close = ")", content = "^[12][0-9]{3}$" },  # ~1,125 year-only
# ]
```

Recommendation: **comment-but-show**, don't activate by default.
Reasoning: the default flips behaviour on a corpus the user never
asked about, which violates the principle of least surprise. But
showing the list inline in any generated `dimfort.toml` (e.g. via
the eventual [`dimfort init`](audit-command.md) command, or in the
in-repo `dimfort.toml.example`) gives every new user the same
informed starting point.

The uppercase-tags entry (`{open="(", close=")", content="^[A-Z_,]+$"}`)
is **not** in the default — it's Corpus A-specific (365 hits there, 0 in
ORCHIDEE, 24 in NEMO).

## 6. Open questions

1. **Should `(from ...)` be in the default list?** 93 sites across
   the three corpora; all are prose qualifiers (`(from observations)`,
   `(from module X)`, etc.). *Resolved 2026-06-13:* yes, ship `(from `
   in the default commented list.
2. **Regex grammar.** Open: how much of POSIX ERE does `content`
   accept? *Resolved 2026-06-13:* small subset — anchors, character
   classes, basic quantifiers, no backrefs/lookaround. Expand on
   user demand.
3. **Default behaviour — on or commented?** *Resolved 2026-06-13:*
   commented-but-shown. Configuration template includes the list
   pre-written; user opts in.
4. **Skip scope.** *Resolved 2026-06-13:* global across all unit
   delimiters. May reconsider if a use case appears for per-delimiter
   skips.
5. **U031 verbose diagnostic.** *Resolved 2026-06-13:* ship it, info
   severity, off outside verbose.

## 7. Out of scope

- **Heuristic non-unit detection** (regex-free "this content doesn't
  look unit-shaped"). That's a relax-mode concern.
- **Per-comment author escape syntax** (e.g. `\(skip me\)` per
  comment). Would require source edits across the codebase. The
  TOML approach achieves the same outcome project-wide for free.
- **The lexer flags.** Covered separately in
  [permissive-unit-lexer.md](permissive-unit-lexer.md). The two
  features are orthogonal by design.

## 8. Empirical appendix

Reproducer commands (run from `Homogeneity/`):

```bash
# Extract every trailing-paren content per corpus
xargs grep -hE '!!?.*\([^()]*\)[[:space:]]*$' < /tmp/files_<corpus>.txt \
  | sed -E 's|.*\(([^()]*)\)[[:space:]]*$|\1|' > /tmp/parens_<corpus>.txt

# Then count each skip pattern (use grep -a; some sources contain
# non-ASCII bytes that grep otherwise rejects as binary)
grep -acE '^see '                  /tmp/parens_<corpus>.txt
grep -aciE '^cf\.? '               /tmp/parens_<corpus>.txt
grep -aciE '^after '               /tmp/parens_<corpus>.txt
grep -aciE '^from '                /tmp/parens_<corpus>.txt
grep -acE '^[12][0-9]{3}$'         /tmp/parens_<corpus>.txt
grep -acE '[12][0-9]{3}'           /tmp/parens_<corpus>.txt
grep -acE 'et al'                  /tmp/parens_<corpus>.txt
grep -acE 'STATIC,OMP|OMP_|MPI'    /tmp/parens_<corpus>.txt
```

File lists (`/tmp/files_<corpus>.txt`) built with:

```bash
find sources/<corpus-a>/  -name '*.f90' -o -name '*.F90' \
   -not -path '*obsolete*' > /tmp/files_corpus-a.txt
find sources/orchidee/ -name '*.f90' -not -path '*.svn*' > /tmp/files_orchidee.txt
find sources/nemo/  -name '*.F90' > /tmp/files_nemo.txt
```
