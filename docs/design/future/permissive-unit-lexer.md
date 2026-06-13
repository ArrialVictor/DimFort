# Permissive unit lexer — flag-toggled reading modes (FUTURE)

**Status:** future feature, post-0.2.2. Drafted 2026-06-13 after the
Corpus B cycle-0 measurement (separate session log at
`annotation/corpus-b/CYCLE_NOTES.md` §5) surfaced that the canonical
`@unit{}` lexer rejects a sizeable fraction of the unit-shape comments
already present in real climate codebases.

## 1. Problem this solves

A team adopting DimFort on an existing codebase typically writes unit
information in inline comments using the conventions their community
already taught them — almost never DimFort's strict canonical form.
Three Fortran climate codes surveyed 2026-06-13 illustrate the spread:

| corpus | trailing-paren comment slots | dominant lexical features |
|---|---|---|
| Corpus A | 18,161 | `/` division, whitespace mult, udunits integer-suffix exponents (`W m-2`), Fortran-style `**` |
| Corpus B | 8,076 | LaTeX `^{-1}` braces, `.` multiplication (`J.kg^{-1}`), `unitless` keyword, Fortran `**` |
| Corpus C | 9,450 | `/`, whitespace mult, `kg m-3`-style suffix, bare-digit exponents (`m2`) |

Each codebase is internally consistent in style — the Corpus A team writes
`W m-2` uniformly, the Corpus B team writes `W m^{-2}` uniformly — but
the conventions differ across communities. (The three corpora are
private survey codebases and are referenced anonymously throughout
this note.) The strict default lexer
can read none of them losslessly today.

The other half of the adoption story —
[`unit_comment_delimiters`](../shipped/unit-comment-delimiters.md)
(0.2.2) — already lets a team tell DimFort *which substring* in a
comment is the unit. This note covers one orthogonal half: **how
DimFort parses the substring once extracted**. A sibling note
[unit-comment-skip-delimiters](unit-comment-skip-delimiters.md)
covers the other half — **how DimFort decides which parens not to
extract at all** (citation / qualifier / year-only patterns).

## 2. Design principles

Three commitments shape the rest of the note. They emerged from a
2026-06-13 discussion (transcript captured in
`annotation/corpus-b/CYCLE_NOTES.md` discussion log).

### 2.1 Independent flags, not modes

Modes (`unit_lexer = "strict" | "latex" | "udunits2" | "all"`) feel
ergonomic but bundle features that are actually orthogonal — the
Corpus B LaTeX brace `^{-1}` and the Corpus C udunits integer-suffix
`m-3` share zero lexer machinery. A project that wants the first
should not be forced to enable the second.

**Independent boolean flags compose.** Modes can be **sugar** that
sets bundles of flags, but the flags are the source of truth.

### 2.2 Uniform across delimiters

Whatever lexer flags are enabled apply to **every** configured
`unit_comment_delimiter`'s content. There is no asymmetry between
"the canonical form" and "the other forms" — the project decides
what its delimiters are, and the lexer treats them all the same.

(Earlier draft suggested keeping `@unit{...}` strict and only
permitting loose forms inside non-`@unit{}` delimiters; rejected
because with `unit_comment_delimiters` configurable, there is no
blessed delimiter — making one stricter than another would be
incoherent.)

### 2.3 Reading is permissive; canonical writing is strict

DimFort's rewriter (today: error-recovery suggestions; future:
[`dimfort rewrite`](rewrite-rules.md)) always proposes one canonical
target form, **independent of which flags are on**. The flags govern
*what's accepted* as input; the rewriter target governs *what's
preferred* as output.

This keeps the codebase-style guide unambiguous (one canonical form)
while the lexer adapts to whatever the project already wrote. The
two axes never tangle: a `(J.kg^{-1})` site is accepted, then the
rewriter still suggests `J/kg` as the canonical form to migrate to.

(Error-recovery suggestions — e.g. "unknown unit `m3`, did you mean
`m^3`?" — are **flag-aware**: if `allow_bare_digit_exp` is on, the
diagnostic doesn't fire at all. This is a different surface from
the canonical-rewriter target.)

## 3. Candidate flags (the empirically-grounded list)

Each flag below has been validated against the three-corpus survey.
Counts are trailing-paren-content occurrences across each corpus;
they bound the absolute upside (subject to relax-mode interaction —
some hits are false-positive parens like `(France, 2002)`).

### 3.1 `allow_latex_braces`

Accept LaTeX-style braced exponents: `m^{-1}`, `kg^{2}`, `W m^{-2}`.

- **Corpus A:** 0 hits — not used.
- **Corpus B:** **312** hits — dominant form for exponents.
- **Corpus C:** 0 hits — not used.

Corpus B-specific. Single highest-leverage flag for Corpus B
adoption.

**Lexer scope:** treat `^{...}` as a synonym for `^...` when the
braced content is a valid exponent (integer, signed integer,
`1/N`).

**Composes with:** everything else.

### 3.2 `allow_dot_multiplication`

Accept `.` between alphabetic identifiers as multiplication:
`J.kg^{-1}`, `kgC.m^{-2}.s^{-1}`, `m^2.m^{-2}`.

- **Corpus A:** 364 hits (mostly genuine — `K.s-1`, `Pa.s-1`).
- **Corpus B:** 219 hits.
- **Corpus C:** 296 hits.

Common in French-tradition climate code; rare in udunits2
canonical.

**Lexer scope:** between identifier characters, treat `.` as the
multiplication operator. Critically: **digit-dot-digit stays a
decimal number** (`0.5`, `1.380658E-23`). The disambiguation rule
is per-token: an alphabetic neighbour on both sides selects mult;
a digit neighbour on either side selects decimal.

**Composes with:** everything else.

### 3.3 `allow_implicit_product`

Accept whitespace between identifiers as multiplication:
`kg m`, `m s`, `W m`, `J kg`.

- **Corpus A:** 2,618 hits (very common — `W m-2`, `kg m-3`, etc.).
- **Corpus B:** 1,409 hits.
- **Corpus C:** 3,049 hits.

The single highest-volume convention across all three corpora.
udunits2 canonical.

**Lexer scope:** between two adjacent identifier tokens, treat
whitespace as the multiplication operator.

**Composes with:** everything else.

**Caveat — `ms` vs `m s`:** with this flag *off*, `ms` is
millisecond (prefix-base). With this flag *on*, `ms` is *still*
millisecond — no whitespace, no product. The whitespace requirement
is part of the rule. The ambiguity is therefore deterministic, not
configurable.

### 3.4 `allow_integer_suffix_exp`

Accept a trailing signed integer on an identifier as exponent:
`m s-1`, `kg m-3`, `J mol-1`, `W m-2`.

- **Corpus A:** 221 hits.
- **Corpus B:** 273 hits.
- **Corpus C:** 162 hits.

udunits2 canonical syntax.

**Lexer scope:** after an identifier, an immediately-adjacent
signed integer (no whitespace between identifier and integer) is
parsed as that identifier's exponent. The integer must be a literal,
optionally with leading `+`/`-`.

**Composes with:** `allow_implicit_product`. **Co-dependent**: an
integer-suffix exponent only makes sense in a context where
whitespace separates the next identifier (otherwise `m s-1 kg` has
no parse). Config-load **errors** if `allow_integer_suffix_exp` is
true and `allow_implicit_product` is false.

### 3.5 `allow_bare_digit_exp`

Accept a trailing bare digit (no caret, no signed prefix) on an
identifier as exponent: `m2`, `m3`, `W/m2`.

- **Corpus A:** 1,604 hits — but with high noise (variable names like
  `i2`, `t2m`, `q1` shape the same way).
- **Corpus B:** 360 hits.
- **Corpus C:** 460 hits.

Common in casual annotation. **Highest false-positive risk** of all
the flags — a CESM/climate codebase has many identifier-like tokens
with trailing digits.

**Lexer scope:** after an identifier whose name is a known unit
(`m`, `s`, `kg`, …), an immediately-adjacent unsigned digit `2-9`
parses as exponent. The "known unit" guard is essential — without
it, every identifier ending in a digit (`i2`, `t2m`) becomes a
parse candidate.

**Composes with:** `allow_implicit_product`. **NOT co-dependent**
strictly, but a 1-character exponent makes the most sense
following whitespace-multiplication conventions.

**Open question:** should `allow_bare_digit_exp` accept digits ≥10
(`m10`)? The survey shows none; recommend rejecting digits ≥10 to
narrow the false-positive surface.

### 3.6 `allow_fortran_star_star`

Accept Fortran-style `**` as the exponentiation operator in the
unit string: `m**2`, `kg**2/m**3`, `s**(-1)`.

- **Corpus A:** 76 hits.
- **Corpus B:** 222 hits.
- **Corpus C:** 11 hits.

Programmer-natural carry-over from Fortran expression syntax.
Always orthogonal to other flags.

**Lexer scope:** treat `**` as a synonym for `^` between identifier
and exponent. Parenthesised exponents are allowed
(`s**(-1)` ≡ `s^(-1)`).

**Composes with:** everything else.

### 3.7 `allow_unicode_superscripts`

Accept Unicode superscript characters (`⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺`) as exponents:
`m·s⁻¹`, `kg m⁻³`.

- **Corpus A:** 0 hits.
- **Corpus B:** 2 hits.
- **Corpus C:** 0 hits.

Almost absent in Fortran climate code. **Cheap to implement, low
real-world payoff.** Include for completeness so user-typed strings
that paste from Unicode-using papers don't fail.

**Lexer scope:** map the superscript codepoints to their ASCII
equivalents during tokenization.

**Composes with:** everything else, including a hypothetical
`allow_middot` for `·` as multiplication.

### 3.8 `allow_middot_multiplication`

Accept `·` (U+00B7 middle dot) as multiplication: `m·s⁻¹`,
`kg·m⁻³`.

- **Corpus A:** 0 hits.
- **Corpus B:** 0 hits.
- **Corpus C:** 0 hits.

**Not observed in any surveyed corpus.** Listed here for
completeness (it's the SI typographical convention; users writing
new annotations from a CF/SI background may use it). Optional;
deferrable.

### 3.9 Non-lexer fellow travellers (NOT in this note)

The following also surfaced in the empirical survey but are
properly **not lexer concerns**:

- **`unitless` keyword** (740 hits in Corpus B) — alias mapping to
  `1`. Belongs in `default_units.toml` or a project `[units] file`
  extension. **Available today via config.**
- **`days`, `hPa`, `mb`, `ubar`, `MJ`** — vocabulary extensions.
  Belong in a project `[units] file`. **Available today via
  config.**
- **`unitless;0-1`, `0-1, unitless`, `true/false`, `T/F`, `-`** —
  prose/range/qualifier markers that aren't units. Belong in
  [relax-mode](relax-mode.md) — extract-a-unit-from-a-comment
  heuristics, not unit-string lexer.
- **Year-only `(2002)`** (Corpus A 260 / Corpus B 175 / Corpus C 690) —
  citation false positives. Belong in
  [skip delimiters](unit-comment-skip-delimiters.md) — author-declared
  non-unit parens, not unit-string lexer.
- **`(see Schmidt et al., 2002)`** (Corpus A 52 / Corpus B 43 / Corpus C 596) —
  prefix-marked citations. Same — [skip delimiters](unit-comment-skip-delimiters.md).
- **`(STATIC,OMP_CHUNK)`-style uppercase OMP/threading tags** —
  Corpus A 365 sites. Content-regex skip delimiter.

These are listed so a reader skimming the empirical numbers
doesn't conclude the lexer must address them.

## 4. Compatibility matrix

| flag | composes with | co-dependent on | mutually exclusive with |
|---|---|---|---|
| `allow_latex_braces` | all | — | — |
| `allow_dot_multiplication` | all | — | — |
| `allow_implicit_product` | all | — | — |
| `allow_integer_suffix_exp` | all except as noted | **`allow_implicit_product`** | — |
| `allow_bare_digit_exp` | all | — | — |
| `allow_fortran_star_star` | all | — | — |
| `allow_unicode_superscripts` | all | — | — |
| `allow_middot_multiplication` | all | — | — |

**No mutually-exclusive pairs in the current set.** Most flags are
purely additive (accept-more shapes, don't reinterpret existing
ones). One co-dependence
(`allow_integer_suffix_exp` ⇒ `allow_implicit_product`) is enforced
by a config-load error.

This matrix is the contract — adding a new flag in the future MUST
include a row here, especially where the new flag introduces a new
mutual-exclusion (e.g. if someone proposes
`allow_dot_decimal_separator` that conflicts with
`allow_dot_multiplication`, the matrix surface that).

## 5. Edge-case corpus (regression-test starter set)

Every flag combination must produce a deterministic parse for each
of these strings. This corpus lives in `tests/unit_lexer/edge_corpus.toml`
and grows as new flags land.

```
# format: input | flags | expected
"ms"                 | all-off        | millisecond
"ms"                 | implicit_product=on        | millisecond
"m s"                | all-off        | parse error
"m s"                | implicit_product=on        | m * s
"m s-1"              | implicit_product=on, int_suffix=on  | m * s^-1
"ms-1"               | implicit_product=on, int_suffix=on  | parse error (no ws)
"m^{-1}"             | latex_braces=on            | m^-1
"kg.m"               | dot_mult=on                | kg * m
"0.5"                | dot_mult=on                | 0.5 (decimal stays decimal)
"m**2"               | fortran_starstar=on        | m^2
"m**(-2)"            | fortran_starstar=on        | m^-2
"m2"                 | bare_digit_exp=on          | m^2
"i2"                 | bare_digit_exp=on          | parse error (i not a unit)
"m·s⁻¹"              | middot=on, unicode_sup=on  | m * s^-1
"J.kg^{-1}.K^{-1}"   | dot_mult=on, latex_braces=on  | J/(kg*K)
"W m-2"              | implicit_product=on, int_suffix=on  | W * m^-2
```

The corpus must be exhaustive enough that adding a flag forces
the author to confirm every existing entry stays unchanged (or
explicitly note which entries change and why).

## 6. Config surface

```toml
[parser.unit_lexer]
# Sugar preset. Sets the boolean flags below to a coherent group.
# Recognized presets: "strict" (default), "permissive_climate",
# "latex", "udunits2", "all".
preset = "strict"

# Boolean flags (override the preset).
allow_latex_braces        = false
allow_dot_multiplication  = false
allow_implicit_product    = false
allow_integer_suffix_exp  = false   # requires allow_implicit_product
allow_bare_digit_exp      = false
allow_fortran_star_star   = false
allow_unicode_superscripts = false
allow_middot_multiplication = false
```

**Preset semantics.** A preset is a starting point; explicit flag
keys override their preset value. Config-load error if explicit
flags contradict a co-dependence (e.g. `allow_integer_suffix_exp=true`
with `allow_implicit_product=false`).

**Suggested preset bundles** (subject to design review):

| preset | flags enabled |
|---|---|
| `strict` | none |
| `permissive_climate` | `allow_dot_multiplication`, `allow_implicit_product`, `allow_integer_suffix_exp`, `allow_fortran_star_star` |
| `latex` | `allow_latex_braces`, `allow_dot_multiplication`, `allow_fortran_star_star` |
| `udunits2` | `allow_implicit_product`, `allow_integer_suffix_exp`, `allow_unicode_superscripts`, `allow_middot_multiplication` |
| `all` | every flag |

Per the corpora: Corpus B wants approximately `latex`; Corpus A and
Corpus C want approximately `udunits2`; an "Corpus B-but-with-fortran-
exponent" project (which exists) wants the union, hence the
preferred ergonomics is to **state the preset and add the missing
flag**, rather than mode-bundles that lock the choice.

## 7. Diagnostic-message implications

Existing error-recovery diagnostics need to be flag-aware. Examples:

- `U002 unknown unit identifier: 'm3'; did you mean 'm^3'?` — should
  not fire when `allow_bare_digit_exp` is on (parses to `m^2` cleanly).
- `U002 unexpected character '.' in 'm^2.m^{-2}'` — should suggest
  "enable `allow_dot_multiplication`" when the surrounding tokens
  match the mult-pattern, not the existing "use `*`".
- `U002 unexpected trailing input near ('ID', 'm') in 'kg m-3'` —
  should suggest "enable `allow_implicit_product` and
  `allow_integer_suffix_exp`" when the input looks udunits-shaped.

Whether to emit these as *upgraded* hints in the U002 message or as
separate codes (`U030 lexer convention available`?) is an open
question for implementation.

## 8. Open questions

1. **Preset names.** `permissive_climate` is verbose; `legacy` is
   loaded; `flexible` is vague. Naming TBD.
2. **Multiple presets composable?** `preset = ["latex", "udunits2"]`
   as a list, taking the union? Or one preset + flag overrides only?
   Recommendation: one preset, overrides on top — simpler mental
   model.
3. **Vocabulary extensions in scope?** `unitless`/`days`/`hPa` etc.
   are config-only today (`[units] file`). Should DimFort ship a
   `climate.toml` companion units file users can `include`, or
   leave it project-by-project? Adjacent feature.
4. **Lexer flag affects `@unit{}` body, yes — but does it affect
   the LSP hover renderer?** Hover and panel should presumably
   always render canonical form. Worth a separate render section.
5. **Migration story for existing strict-only projects.** If a
   project flips the preset, every previously-rejected comment
   becomes a candidate annotation overnight — could be a surge of
   newly-derived diagnostics. Recommendation: when a preset
   widens, emit a one-time summary of newly-readable comment
   counts so the user can audit.

## 9. Out of scope

- **Modes as the canonical surface.** Modes are sugar; flags are
  the contract.
- **udunits2 reading parity with full semantic equivalence.** This
  note covers lexical-only — accepting the syntax. Semantic
  parity (canonicalization rules, log-scale `dB`, time-since-epoch
  `hours since 1970-01-01`) stays out of scope, matching the
  existing internal udunits2-parity scoping.
- **The rewriter target.** Always canonical, regardless of flags.
  See [rewrite-rules.md](rewrite-rules.md).
- **Comment-extraction rules** (which substring of an inline
  comment is the unit). That's
  [relax-mode](relax-mode.md), not the lexer.
- **Vocabulary expansion** (`unitless`/`days`/`hPa`). Project
  `[units] file`, not lexer.

## 10. Empirical appendix

Per-corpus pattern counts measured 2026-06-13 (script:
`grep -hE '!!?.*\([^()]*\)[[:space:]]*$' <files> | sed -E 's|.*\(([^()]*)\)[[:space:]]*$|\1|'`
piped into pattern-specific `grep -c`). Corpus A excludes archive
directories and symlink-duplicated subtrees from the count.

| pattern \ corpus | Corpus A | Corpus B | Corpus C |
|---|---|---|---|
| total trailing-paren slots | 18,161 | 8,076 | 9,450 |
| LaTeX `^{...}` braces | 0 | 312 | 0 |
| dot-mult `X.Y` | 364 | 219 | 296 |
| udunits integer-suffix | 221 | 273 | 162 |
| bare-digit exponent | 1,604 | 360 | 460 |
| Fortran `**` exponent | 76 | 222 | 11 |
| Unicode superscript | 0 | 2 | 0 |
| middle dot `·` | 0 | 0 | 0 |
| `unitless` keyword | 1 | 1,109 | 0 |
| year-only `(2002)` | 260 | 175 | 690 |
| `true/false` etc. | 4 | 168 | 33 |
| `/` division | 1,144 | 1,088 | 1,146 |
| whitespace-mult leading | 2,618 | 1,409 | 3,049 |
| comma list | 5,198 | 992 | 1,292 |

(Bare-digit counts include false-positive identifier tokens; treat
as upper bound. Year-only counts include legitimate citations the
relax-mode filter would catch.)
