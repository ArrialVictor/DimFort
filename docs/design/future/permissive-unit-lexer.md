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
Six Fortran climate codes surveyed 2026-06-13 illustrate the spread:

| corpus | unit-slot population | dominant lexical features |
|---|---|---|
| Corpus A | 18,161 trailing-paren | `/` division, whitespace mult, udunits integer-suffix exponents (`W m-2`), Fortran-style `**` |
| Corpus B | 8,076 trailing-paren | LaTeX `^{-1}` braces, `.` multiplication (`J.kg^{-1}`), `unitless` keyword, Fortran `**` |
| Corpus C | 9,450 trailing-paren | `/`, whitespace mult, `kg m-3`-style suffix, bare-digit exponents (`m2`) |
| Corpus D | 7,196 trailing-paren | `/` division, bare-digit exponents, `(-)` dimensionless marker |
| Corpus E | 15,580 trailing-paren | `/` division, bare-digit exponents, heavy dimension-hint / INTENT noise in trailing parens |
| Corpus F | 5,762 trailing-**bracket** | `[unit]` brackets (not parens), `/`, bare-digit, biogeochem tracer-tagging (`mol(C)/m^2`) |

Each codebase is internally consistent in style — the Corpus A team writes
`W m-2` uniformly, the Corpus B team writes `W m^{-2}` uniformly — but
the conventions differ across communities. (The six corpora are
private survey codebases and are referenced anonymously throughout
this note. Corpora A–C were the original 3-corpus measurement that
produced the 0.2.7 priority-four flag set; D–F extended the survey
on 2026-06-13 to validate generalization across distinct convention
lineages and reprioritized one flag — see §3.5.) The strict default
lexer can read none of them losslessly today.

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

Each flag below has been validated against the six-corpus survey.
Counts are trailing-paren-content occurrences across each corpus
(Corpus F counts are trailing-bracket); they bound the absolute
upside (subject to relax-mode interaction — some hits are
false-positive parens like `(France, 2002)`).

### 3.1 `allow_latex_braces`

Accept LaTeX-style braced exponents: `m^{-1}`, `kg^{2}`, `W m^{-2}`.

- **Corpus A:** 0 hits — not used.
- **Corpus B:** **312** hits — dominant form for exponents.
- **Corpus C:** 0 hits — not used.
- **Corpus D:** 3 hits — essentially absent.
- **Corpus E:** 0 hits — not used.
- **Corpus F:** 132 hits — non-trivial; LaTeX braces appear in
  formula-heavy declarations.
- **Union: 447 hits across 6 corpora.**

Corpus B-dominant; Corpus F adds a second user. Single highest-
leverage flag for Corpus B adoption.

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
- **Corpus D:** 86 hits — moderate.
- **Corpus E:** 186 hits.
- **Corpus F:** **0** hits — not used.
- **Union: 1,151 hits across 6 corpora.**

Common in French/MesoNH-lineage climate code; rare in udunits2
canonical and absent from the modern Corpus F lineage.

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
- **Corpus D:** 1,680 hits.
- **Corpus E:** 2,432 hits.
- **Corpus F:** 860 hits.
- **Union: 12,048 hits across 6 corpora — highest-volume of any flag.**

The single highest-volume convention across all six corpora.
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
- **Corpus D:** 303 hits.
- **Corpus E:** 152 hits.
- **Corpus F:** 680 hits.
- **Union: 1,791 hits across 6 corpora.**

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
- **Corpus D:** **1,396** hits.
- **Corpus E:** **1,240** hits.
- **Corpus F:** **1,356** hits.
- **Union: 6,416 hits across 6 corpora — second-highest of any flag,**
  trailing only `allow_implicit_product`.

**Priority status (2026-06-13 update).** The original three-corpus
survey (A–C) had this flag at moderate volume; the three-corpus
extension (D–F) showed it appearing heavily in every additional
corpus measured. The union-evidence makes it as essential as the
top-four flags for the 0.2.7 ship. This flag was previously in
the deferred 5-8 set; **the empirical evidence has reprioritized
it into the priority set**.

Common in casual annotation. **Highest false-positive risk** of all
the flags — many climate codebases have identifier-like tokens
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
- **Corpus D:** 17 hits.
- **Corpus E:** 73 hits.
- **Corpus F:** 79 hits.
- **Union: 478 hits across 6 corpora.**

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
- **Corpus D:** 0 hits.
- **Corpus E:** 0 hits.
- **Corpus F:** 0 hits.
- **Union: 2 hits across 6 corpora — empirically marginal.**

Almost absent in Fortran climate code (6 corpora measured, 2 union
hits). **Cheap to implement, low real-world payoff.** Include for
completeness so user-typed strings that paste from Unicode-using
papers don't fail; recommend off-by-default given the thin
evidence.

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
- **Corpus D:** 0 hits.
- **Corpus E:** 0 hits.
- **Corpus F:** 0 hits.
- **Union: 0 hits across 6 corpora.**

**Not observed in any surveyed corpus** (now 6 corpora). Listed
here for completeness (it's the SI typographical convention; users
writing new annotations from a CF/SI background may use it).
Recommend off-by-default; the implementation cost is trivial (single
token alias `·` → `*`) so it's available if surfaced demand
emerges, but the empirical evidence does not warrant it being on
by default. Optional;
deferrable.

### 3.9 `strip_inner_parens` — tracer-tag pre-processor (sibling option)

Pre-process unit strings by stripping `\([a-zA-Z]+\)` patterns
before parsing. Converts `mol(C)/m^2(canopy)` → `mol/m^2`,
discarding tracer-species and spatial-domain metadata.

- **Corpus A–E:** 0 hits.
- **Corpus F:** **~240 hits** — biogeochem tracer-tagging convention.
- **Union: ~240 hits across 6 corpora — Corpus F-specific.**

**Different category from §3.1-§3.8.** Those flags add *token
recognition rules* to the lexer; this is a *pre-processing step*
that runs before tokenization. Treated here for empirical
completeness — Corpus F adoption depends on it.

**Lexer scope:** before tokenization, apply a regex pass
substituting `\([a-zA-Z]+\)` → empty within the unit string.
Lossy by design (the metadata is discarded) but safe.

**Implementation cost:** trivial — single regex pre-pass. ~6
test cases.

**Open question:** should the strip be configurable to preserve
specific patterns? E.g., a project that uses `mol(C)` consistently
and wants DimFort's polymorphic-units machinery (`mol('a)`) to
treat `(C)` as a species parameter could opt in. Recommend
deferring this question until polymorphic units are routinely used
on this kind of tagged corpus.

**Composes with:** everything else; orthogonal to all lexer flags.

### 3.10 Non-lexer fellow travellers (NOT in this note)

The following also surfaced in the empirical survey but are
properly **not lexer concerns**:

- **`unitless` keyword** (1,109 hits in Corpus B + 140 in Corpus F)
  — alias mapping to `1`. Belongs in `default_units.toml` or a
  project `[units] file` extension. **Available today via config.**
- **`(-)` dimensionless marker** (Corpus D 453 + Corpus F 188) —
  the dash-alone variant of "unitless". Alias to `1` in a project
  `[units] file`, OR skip-delimiter on content-regex
  `^-$`. **Available today via config.**
- **`days`, `hPa`, `mb`, `ubar`, `MJ`, `microns`, `radians`,
  `degrees`** — vocabulary extensions. Belong in a project
  `[units] file`. **Available today via config.**
- **`unitless;0-1`, `0-1, unitless`, `true/false`, `T/F`, `-`** —
  prose/range/qualifier markers that aren't units. Belong in
  **relax-mode** (planned sibling design; see IDEAS_REGISTRY entry
  in the Homogeneity work-notes) — extract-a-unit-from-a-comment
  heuristics, not unit-string lexer.
- **Year-only `(2002)`** (Corpus A 260 / B 175 / C 690 / D 140 /
  E 110 / F absent) — citation false positives. Belong in
  [skip delimiters](unit-comment-skip-delimiters.md) — author-declared
  non-unit parens, not unit-string lexer.
- **`(see Schmidt et al., 2002)`** (Corpus A 52 / B 43 / C 596 /
  D 27 / E 75) — prefix-marked citations. Same —
  [skip delimiters](unit-comment-skip-delimiters.md).
- **`(STATIC,OMP_CHUNK)`-style uppercase OMP/threading tags** —
  Corpus A 365 + Corpus D 53 (DrHook `ZHOOK_HANDLE_OMP`-style).
  Content-regex skip delimiter on uppercase-comma patterns.
- **`(ncol,nlay)`, `(i,j,k)` dimension hints** — Corpus E ~3,696
  sites; the dominant non-unit-paren class in this convention lineage.
  Content-regex skip delimiter on lowercase-comma patterns.
- **`(in)`, `(out)`, `(inout)` Fortran INTENT declarations** —
  Corpus E 1,063 sites. Content-regex skip delimiter on
  `^(in|out|inout)$`.
- **`[unit]` square-bracket delimiter convention** — Corpus F
  uses brackets instead of parens. Already solved by the existing
  0.2.2 `unit_comment_delimiters` config — no lexer or extraction
  change needed; the bracket form just needs to be exercised in
  the 0.2.7 test corpus.

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
  comment is the unit). That's **relax-mode** (planned sibling
  design), not the lexer.
- **Vocabulary expansion** (`unitless`/`days`/`hPa`). Project
  `[units] file`, not lexer.

## 10. Empirical appendix

Per-corpus pattern counts measured 2026-06-13 (script:
`grep -hE '!!?.*\([^()]*\)[[:space:]]*$' <files> | sed -E 's|.*\(([^()]*)\)[[:space:]]*$|\1|'`
piped into pattern-specific `grep -c`). For Corpus F the same
pipeline is used but with `\[[^]]+\]` in place of `\([^()]*\)`
because Corpus F uses brackets, not parens, as the unit-slot
delimiter. Corpus A excludes archive directories and
symlink-duplicated subtrees from the count.

**Survey base.** Corpora A–C are the original 2026-06-13 3-corpus
survey that produced the priority-four flag set. Corpora D–F are
the 2026-06-13 extension that broadened the empirical base across
distinct convention lineages (an additional land-surface code, a
distinct-tradition atmospheric code, a modern coupled atmosphere +
ocean code) and reprioritized §3.5
(`allow_bare_digit_exp`) from deferred to priority.

| pattern \ corpus | A | B | C | D | E | F | **union** |
|---|---:|---:|---:|---:|---:|---:|---:|
| total unit slots | 18,161 | 8,076 | 9,450 | 7,196 | 15,580 | 5,762 | **64,225** |
| LaTeX `^{...}` braces | 0 | 312 | 0 | 3 | 0 | 132 | **447** |
| dot-mult `X.Y` | 364 | 219 | 296 | 86 | 186 | 0 | **1,151** |
| udunits integer-suffix | 221 | 273 | 162 | 303 | 152 | 680 | **1,791** |
| **bare-digit exponent** | **1,604** | 360 | 460 | **1,396** | **1,240** | **1,356** | **6,416** |
| Fortran `**` exponent | 76 | 222 | 11 | 17 | 73 | 79 | **478** |
| Unicode superscript | 0 | 2 | 0 | 0 | 0 | 0 | **2** |
| middle dot `·` | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| `unitless` keyword | 1 | 1,109 | 0 | 0 | 1 | 140 | 1,251 |
| `(-)` dimensionless | 10 | n/a | 4 | 453 | n/a | 188 | ~655 |
| year-only `(2002)` | 260 | 175 | 690 | 140 | 110 | n/a | ~1,375 |
| `/` division | 1,144 | 1,088 | 1,146 | 1,776 | 2,501 | 2,404 | 10,059 |
| whitespace-mult leading | 2,618 | 1,409 | 3,049 | 1,680 | 2,432 | 860 | 12,048 |
| tracer-tag `mol(C)` | 0 | 0 | 0 | 0 | 0 | **240** | 240 |
| dimension hints (`ncol,nlay`) | n/a | n/a | n/a | 150 | **~3,696** | n/a | ~3,846 |
| INTENT `(in)`/`(out)` | n/a | n/a | n/a | n/a | **1,063** | n/a | 1,063 |
| OpenMP/threading tags | **365** | 0 | 24 | **53** | n/a | n/a | ~442 |

(Bare-digit counts include false-positive identifier tokens; treat
as upper bound. Year-only counts include legitimate citations the
relax-mode filter would catch.)
