# Permissive unit lexer — flag-toggled reading modes

**Status:** **0.2.7 design pass complete; implementation pending.**
Drafted 2026-06-13 after the Corpus B cycle-0 measurement
surfaced that the canonical `@unit{}` lexer rejects a sizeable
fraction of the unit-shape comments already present in real
climate codebases. Updated 2026-06-13/14 with the 6-corpus
broadening and the priority promotion of `allow_bare_digit_exp`.
Updated 2026-06-15 with empirical Q1/Q2 resolutions (digits-≥10
strict-lock, `**` four-shape accept), per-flag false-positive
characterization, and preset-bundle deferral to 0.2.8. File
remains in `future/` until the implementation lands and the note
is promoted to `shipped/`.

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
| Corpus F | 5,762 trailing-**bracket** | `[unit]` brackets (dominant), `/`, bare-digit, biogeochem tracer-tagging (`mol(C)/m^2`) |

Each codebase has a **dominant** convention — the Corpus A team
writes `W m-2`, the Corpus B team writes `W m^{-2}`, the Corpus F
team puts the unit in `[brackets]` — but the conventions differ
across communities. **(A late 2026-06-13 follow-up survey found
that every corpus also carries a substantial secondary form: every
parens-dominant corpus uses brackets for a non-trivial fraction of
units too, ranging from ~770 sites in Corpus D up to 5,125 sites
in Corpus E. The "dominant" framing is therefore a simplification;
a complete `dimfort.toml` template includes BOTH `{open=" (",
close=")"}` AND `{open=" [", close="]"}` rules for any corpus.
The lexer-flag conclusions in this note are unchanged — see §10
appendix for trailing-bracket counts where they affect the
flag-coverage story.)** (The six corpora are private survey
codebases and are referenced anonymously throughout this note.
Corpora A–C were the original 3-corpus measurement that produced
the 0.2.7 priority-four flag set; D–F extended the survey on
2026-06-13 to validate generalization across distinct convention
lineages and reprioritized one flag — see §3.5.) The strict
default lexer can read none of them losslessly today.

The other half of the adoption story —
[`unit_comment_delimiters`](../shipped/unit-comment-delimiters.md)
(0.2.2) — already lets a team tell DimFort *which substring* in a
comment is the unit. This note covers one orthogonal half: **how
DimFort parses the substring once extracted**. A sibling note
[unit-comment-skip-delimiters](unit-comment-skip-delimiters.md)
covers the other half — **how DimFort decides which parens not to
extract at all** (citation / qualifier / year-only patterns).

## 2. Design principles

Three commitments shape the rest of the note. They emerged from
the 2026-06-13 design pass informing this revision.

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

**False-positive characterization.**

- **Lexical pattern accepted.** `<ident>^{<exponent>}` where
  `<exponent>` matches `[+-]?\d+` or `1/[+-]?\d+`.
- **Mitigation in the lexer rule.** The opening `^{` is the
  unambiguous trigger. Braces alone are insufficient — the rule
  requires the caret-plus-brace sequence following a unit
  identifier, so unrelated `{...}` shapes (e.g., F2003 derived-type
  initializers in unit-context strings, which don't occur) never
  match.
- **Known FP shapes.** Prose math inside the extracted unit
  substring (`@unit{ rate increases like x^{2} }`) — but DimFort
  only sees content within configured `unit_comment_delimiters`,
  so the FP surface is whatever projects put inside their
  annotations. The braced exponent rule only triggers when the
  identifier preceding `^{` is in the known-unit set, narrowing
  the surface further.
- **Corpus fixtures.** `tests/unit_lexer/fixtures/latex_braces/`
  with `accept.in` (`m^{-1}`, `kg^{2}`, `W m^{-2}`,
  `J.kg^{-1}.K^{-1}`) and `reject.in` (`^{}` empty braces,
  `m^{abc}` non-integer exponent, `^{2}` unanchored).
- **Concrete FP scenario.** A team enables the flag, then later
  adds an annotation `@unit{ s^{-1} on output channel 2 }`. The
  lexer reads `s^{-1}` cleanly; the trailing prose generates a
  U002 "unexpected trailing input" the same way it would in strict
  mode. Adoption guidance (CHANGELOG + adoption template): keep
  prose out of `@unit{}` bodies — the flag widens the unit
  vocabulary, not the comment language.

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

Common in one major European convention lineage; rare in udunits2
canonical and absent from the modern Corpus F lineage.

**Lexer scope:** between identifier characters, treat `.` as the
multiplication operator. Critically: **digit-dot-digit stays a
decimal number** (`0.5`, `1.380658E-23`). The disambiguation rule
is per-token: an alphabetic neighbour on both sides selects mult;
a digit neighbour on either side selects decimal.

**Composes with:** everything else.

**False-positive characterization.**

- **Lexical pattern accepted.** `<ident>.<ident>` where both sides
  are identifier characters (alphabetic, optionally trailing digits
  / underscore after the head). The dot is rewritten to `*` post-
  tokenization.
- **Mitigation in the lexer rule.** Decimal literals
  (`<digit>+ . <digit>+`, with optional sign and exponent) are
  classified at the tokenizer level **before** the dot-mult rule
  fires. Period inside a number stays inside the number; period
  between two identifiers becomes a mult. The two cases never
  overlap because the lexer's lookahead checks neighbour character
  class.
- **Known FP shapes.** Decimal literals embedded in extracted unit
  content (e.g., `@unit_assume{0.5: m/s}`) — the rule must not eat
  the `0.5` separator. Scientific notation `1.380658E-23` —
  similarly preserved. Pseudo-method calls in unit prose
  (`module.symbol`) — vanishingly rare inside unit strings; would
  be read as multiplication if both sides happen to be known units
  (acceptable parse) or otherwise yields U002.
- **Corpus fixtures.** `tests/unit_lexer/fixtures/dot_multiplication/`
  with `accept.in` (`J.kg^{-1}`, `kg.m`, `m^2.m^{-2}`,
  `kgC.m^{-2}.s^{-1}`) and `reject.in` (`0.5`, `1.380658E-23`,
  `1.0e-3`, `.5` leading-dot decimal).
- **Concrete FP scenario.** A coefficient like `1.380658E-23 J/K`
  reaches the lexer via `@unit_assume{1.380658E-23: J/K}`. Without
  proper decimal classification, the dot-mult rule would tokenize
  `1.380658E-23` as `1 * 380658E-23` (catastrophic). Test coverage
  MUST exercise the decimal-vs-mult disambiguation as a regression
  gate.

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

**False-positive characterization.**

- **Lexical pattern accepted.** Whitespace between two recognized
  unit identifiers acts as multiplication: `kg m`, `W m`, `J kg`.
  The whitespace must be ordinary ASCII space or tab; newlines
  break the unit-string scope.
- **Mitigation in the lexer rule.** Both flanking tokens must be
  in the known-unit vocabulary. Prose drifting into the unit slot
  (`mass kg`, `pressure Pa`) does NOT silently parse — `mass`
  remains an unknown identifier (U002), so the rule never erases
  prose into a valid parse. The `ms`-vs-`m s` disambiguation (§3.3
  caveat) is anchored to the whitespace requirement, deterministic,
  and tested.
- **Known FP shapes.** Numeric-prefix shapes (`1 kg`, `1 W m-2`)
  parse as numerator-1 multiplied by the unit. Acceptable —
  matches udunits2 convention where leading `1` indicates
  dimensional emphasis. Authors writing `1 unit` get `unit` (the
  multiplicative identity composes cleanly).
- **Corpus fixtures.** `tests/unit_lexer/fixtures/implicit_product/`
  with `accept.in` (`kg m^-3`, `W m-2`, `J mol^-1`, `1 kg m^-3`)
  and `reject.in` (`ms` → millisecond not product, `m s-1 kg` with
  integer_suffix_exp OFF — bare `-1` orphaned, `mass kg` — `mass`
  unknown).
- **Concrete FP scenario.** A two-character unit like `Pa` (pascal)
  followed by `s` (second) — `Pa s` parses as `Pa * s` (correct,
  Pa-seconds is dynamic viscosity). Without `allow_implicit_product`,
  the same string errors at `Pa` because no token continuation
  exists. This is the canonical empirical-payoff case, not an FP.

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

**False-positive characterization.**

- **Lexical pattern accepted.** Identifier immediately followed by
  a signed integer (no whitespace between): `m-3`, `s-1`, `mol-1`,
  `W+2`. Unsigned positive is `allow_bare_digit_exp` territory
  (§3.5); the sign distinguishes the two rules.
- **Mitigation in the lexer rule.** The identifier must be in the
  known-unit set (excludes variable-name tokens). The signed
  integer must be attached with no whitespace (`m -1` does NOT
  parse as `m^-1` — the space breaks the suffix-exponent rule).
  Combined with the co-dependence on `allow_implicit_product`, the
  surface tightens further: the suffix only fires within a
  product context where whitespace separates the next term.
- **Known FP shapes.** Variable names matching `<ident>-N` are
  almost absent in practice (Fortran doesn't allow `-` in
  identifiers). Equation-number citations like `eq-1` would only
  fire if `eq` were a known unit; it isn't.
- **Corpus fixtures.**
  `tests/unit_lexer/fixtures/integer_suffix_exp/` with `accept.in`
  (`m s-1`, `kg m-3`, `J mol-1`, `W m-2 K-1`) and `reject.in`
  (`s-1` standalone — no product context, `m s -1` — whitespace
  before sign, `rate-1` — `rate` not a unit).
- **Concrete FP scenario.** Author writes `@unit{m-1 sec-1}`
  thinking shorthand. With `allow_integer_suffix_exp` ON, parses
  cleanly as `m^-1 * sec^-1`. The `sec` synonym would error at
  vocabulary lookup; canonical `s` is required (or supply a
  project `[units]` alias). The rule itself is robust; FP risk
  sits in upstream vocabulary choices.

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

**Digits ≥10 — strict rule (settled 2026-06-15).** The rule
rejects bare-digit exponents ≥10. Empirical basis (`unit-symbol`
followed by a 2-digit number, with word boundaries, run against
each corpus's trailing-paren and trailing-bracket content):

- **4 real sites** across **2 corpora**, all the same unit form
  (`m13/kg4`, a snow-physics empirical-fit coefficient in a
  land-surface family). Forcing these to write `m^13/kg^4`
  (caret form, which the default lexer reads) is acceptable.
- A broad-filter sweep before strict-filtering surfaced ~1,000+
  false-positive candidates. Dominant FP patterns: paper-equation
  labels in code comments (`(s10)`, `(s11)`, `(s12)`, …, ~8 sites
  in a single file in one corpus), isotope notation (`N15` in
  biogeochem), source-file path references (`stomate_*_ter_m10.f90`),
  equation citations (`Y83`, `B92`, `PL98`, etc.), and variable /
  array names (`radscr10`–`radscr17`, `Vcmax25`, `fu10`, `wind10m`).
- The unit-symbol guard plus the digits ≤9 cap excludes these
  cleanly. Allowing ≥10 would generate ~8 FPs per affected file in
  one observed corpus alone — the trade-off (4 real sites
  reclaimable via `^`, ~8+ per-file FPs averted) is empirically
  defensible.

The rule is empirically locked, not heuristic — surface
recategorization (e.g., extending the unit-symbol allowlist)
remains future work, but the digits-≥10 cap stays.

**False-positive characterization.**

- **Lexical pattern accepted.** Known-unit identifier (`m`, `s`,
  `kg`, `K`, `Pa`, `J`, `W`, `N`, `mol`, `rad`, `cm`, `mm`, `km`,
  `hPa`, …) immediately followed by an unsigned digit `2-9`.
- **Mitigation in the lexer rule.** Three stacked guards: (a)
  identifier must be in the known-unit set (excludes `i2`, `t2m`,
  `q1`, all variable names); (b) digit must be `2-9` (excludes
  equation labels `s10`+, isotopes `N15`, compound variable names
  `Vcmax25`); (c) digit must be immediately adjacent — no whitespace.
  The opt-in default amplifies the mitigation: ON-by-default would
  be a soundness regression, and the warning on enable (per the
  0.2.7 plan §"Code action enable flag UX") surfaces the trade-off
  at the moment of decision.
- **Known FP shapes.** Variable names that happen to be known
  units plus a single digit (`m2` as a mathematical variable; `s2`
  as a state-variable index). With bracket-extraction enabled
  (Corpus F lineage), the FP surface widens — `[m2]` extracted
  from `! see m2 in [m2]` could be read as `m²` when the author
  meant a variable reference.
- **Corpus fixtures.**
  `tests/unit_lexer/fixtures/bare_digit_exp/` with `accept.in`
  (`m2`, `m3`, `W/m2`, `kg m2`, `Pa s2`) and `reject.in` (`i2`,
  `t2m`, `m10`, `m1` — digit 1 ambiguous with dimensionless,
  `Vcmax25`, `radscr10`, `N15`, paper-equation label `(s11)`).
- **Concrete FP scenario.** A project uses bracket extraction
  (`{open="[", close="]"}` per Corpus F convention) over a file
  where a Fortran variable named `m2` appears in inline doc:
  `! Snowmelt rate, see m2 in [m2]`. The bracket-extractor pulls
  `m2` from `[m2]`; with the flag ON, the lexer reads it as `m²`.
  The author meant a variable reference, not a unit. **This is
  the canonical high-FP scenario** — it drives the strict default,
  the opt-in posture, and the warning notification on flag
  activation. The adoption template surfaces the trade-off in
  the comment header at the moment the user decides.

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
and exponent. Both bare and parenthesised exponents are accepted:

```
m**2    ≡ m^2     (bare positive — 639 union sites, dominant)
m**-1   ≡ m^-1    (bare negative — 8 union sites, real units)
m**(2)  ≡ m^2     (paren positive — 0 observed, accepted for completeness)
m**(-1) ≡ m^-1    (paren negative — 0 observed, accepted for completeness)
```

The 2026-06-15 empirical follow-up (`climate_model_survey/data/`)
found 639 bare-positive and 8 bare-negative sites across 647 total
`**` sites in 6 corpora. **No corpus used the parenthesised form
in either sign** — the operator-precedence reasoning that motivates
parens in Fortran source code doesn't carry over because unit-
comment exponents aren't parsed by the Fortran compiler. Accepting
all four shapes is trivial (a few lexer alternation rules) and
recovers the 8 real bare-negative sites (`K**-1`, `m**-3`, `cm**-3`,
`s**-1`, etc. — heat-capacity reciprocal, number density, frequency)
that the originally-implied "parens required" rule would have
rejected.

**Composes with:** everything else.

**False-positive characterization.**

- **Lexical pattern accepted.** Four shapes: `<unit>**<int>`,
  `<unit>**-<int>`, `<unit>**(<int>)`, `<unit>**(-<int>)`.
- **Mitigation in the lexer rule.** `**` is an unambiguous two-
  character token. No other unit-string idiom collides; Fortran
  source code uses `**` as exponentiation, and unit comments
  follow the same convention. The bare and paren forms share the
  same downstream AST so no behavioural divergence arises from
  accepting both.
- **Known FP shapes.** None observed. The `**` operator carries no
  overlap with prose, variable names, or other unit-string
  conventions across all 6 surveyed corpora.
- **Corpus fixtures.**
  `tests/unit_lexer/fixtures/fortran_star_star/` with `accept.in`
  (all 4 shapes plus compounds `kg m**-1 s**-1`,
  `J m**-3 K**-1`) and `reject.in` (`m***2` — triple stars,
  `m** 2` — whitespace between `**` and exponent, `**2` — no base).
- **Concrete FP scenario.** None observed across 647 union sites
  in 6 corpora. The flag is essentially free to enable; the only
  reason to leave it off is project-level convention enforcement
  (a team that wants to standardize on `^` may flag `**` as
  non-canonical via the rewrite-suggestion path).

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

**False-positive characterization.**

- **Lexical pattern accepted.** Static codepoint substitution
  `⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺` → ASCII `0123456789-+` at tokenization time.
- **Mitigation in the lexer rule.** Single deterministic
  translation table. The Unicode codepoints carry no other meaning
  in scientific Fortran source; they exist only as typographic
  exponents (papers, slides, prose comments).
- **Known FP shapes.** None. The codepoint range is dedicated to
  superscript glyphs by Unicode; no overlap with any other lexer
  rule, no other use in Fortran source.
- **Corpus fixtures.**
  `tests/unit_lexer/fixtures/unicode_superscripts/` with
  `accept.in` (`m·s⁻¹`, `kg m⁻³`, `J kg⁻¹ K⁻¹`) and a sanity
  `reject.in` (`⁻⁻` — stray sign run with no base, codepoint
  outside the supported range).
- **Concrete FP scenario.** None. Implementation is a static
  codepoint table; the only failure mode is a missing codepoint,
  which is a coverage bug not a false positive. Adoption is free.

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

**False-positive characterization.**

- **Lexical pattern accepted.** `·` (U+00B7 middle dot) between
  identifiers acts as multiplication; rewritten to `*` at
  tokenization.
- **Mitigation in the lexer rule.** The middle dot has no other
  use in scientific text — distinct from period `.` (which is
  `allow_dot_multiplication`-controlled and decimal-aware),
  distinct from bullet `•` (U+2022), distinct from interpunct used
  in non-scientific text. No collision.
- **Known FP shapes.** None observed. U+00B7 is a dedicated SI
  multiplication codepoint with no overlap.
- **Corpus fixtures.**
  `tests/unit_lexer/fixtures/middot_multiplication/` with
  `accept.in` (`m·s`, `kg·m⁻³`, `J·kg⁻¹·K⁻¹` composed with
  `allow_unicode_superscripts`) and `reject.in` (`· · ·` —
  isolated middots with no surrounding identifiers).
- **Concrete FP scenario.** None observed across 6 corpora (0
  union hits). Shipped for completeness against future SI-conformant
  projects and authors transcribing from typeset papers.

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
  Corpus A 365 + Corpus D 53 (profiling-framework threading
  handles). Content-regex skip delimiter on uppercase-comma
  patterns.
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
# Each flag is independent. All default to OFF — strict behaviour
# unless the project explicitly opts in. There are no preset bundles
# in 0.2.7; preset sugar is deferred to 0.2.8 once the 8 flags ship
# and real preset bundles become meaningful (see §8 open questions).
allow_latex_braces          = false
allow_dot_multiplication    = false
allow_implicit_product      = false
allow_integer_suffix_exp    = false   # requires allow_implicit_product
allow_bare_digit_exp        = false   # high FP risk — see §3.5
allow_fortran_star_star     = false
allow_unicode_superscripts  = false
allow_middot_multiplication = false
```

**Default state — all 8 OFF.** Strict, conservative, no out-of-box
silent misparses. `allow_bare_digit_exp` ON-by-default would
constitute a soundness regression (high false-positive surface per
§3.5) for the price of out-of-box convenience; instead users opt
into permissiveness explicitly, which surfaces the trade-off
visibly rather than hiding it. The same logic applies — to lesser
degree — for the other flags.

**Co-dependence enforced at config load.** The single hard
relationship — `allow_integer_suffix_exp` requires
`allow_implicit_product` — is enforced by a config-load error with
a clear remediation message. No other dependencies exist (see §4
compatibility matrix).

**Adoption template (docs deliverable, separate from this note).**
`docs/adoption/permissive-lexer-template.dimfort.toml` ships every
flag explicitly — priority 6 set `= true`, trivial 2 set `= false`
(no commented-out mystery lines). Comment header makes the FP-risk
on `allow_bare_digit_exp` visible right where users decide. Pattern
parallels the climate-vocabulary template.

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

Settled in the 2026-06-15 design pass (no longer open):
- **Digits ≥10** under `allow_bare_digit_exp` — rejected (§3.5).
- **`**` exponent shapes** — all four accepted (§3.6).
- **Default state** — all 8 flags OFF (§6).
- **Preset names + multi-preset composition** — moot for 0.2.7;
  preset sugar deferred to 0.2.8 once the 8 flags ship and real
  preset shapes can be designed against observed adoption patterns.

Still open:

1. **Vocabulary extensions in scope?** `unitless`/`days`/`hPa` etc.
   are config-only today (`[units] file`). Should DimFort ship a
   `climate.toml` companion units file users can `include`, or
   leave it project-by-project? Adjacent feature; see also the
   udunits2 vocabulary-ingestion work and the `climate-template`
   adoption file.
2. **Lexer flag affects `@unit{}` body, yes — but does it affect
   the LSP hover renderer?** Hover and panel should presumably
   always render canonical form. Worth a separate render section
   during implementation.
3. **Migration story for existing strict-only projects.** If a
   project flips multiple flags on at once, every previously-
   rejected comment becomes a candidate annotation overnight —
   could be a surge of newly-derived diagnostics. Recommendation:
   when the lexer flag-set widens, emit a one-time summary of
   newly-readable comment counts so the user can audit.

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

**Corpus scale.** Counts below are not from cherry-picked modules
but from whole-codebase sweeps. Aggregate: **7,300 source files
across 6 corpora, ~3.95 MLoC** spanning six distinct convention
lineages (three classes of atmospheric model, a land-surface
family, a distinct-tradition atmospheric model, and a modern
coupled atmosphere+ocean system). The smallest corpus (95 files)
is a focused biogeochem land-surface module; the largest (1,889
files, 1.27 MLoC) is a research+operational coupled system.
Lineage diversity matters more than absolute file count for the
generalization claim — six lineages exceeding ~150 kLoC each
defends against "you measured one team's idiosyncratic style."

| pattern \ corpus | A | B | C | D | E | F | **union** |
|---|---:|---:|---:|---:|---:|---:|---:|
| **source files (`.f90`/`.F90`)** | ~2,095 | 95 | 701 | 1,366 | 1,154 | 1,889 | **~7,300** |
| **lines of code (k)** | ~793 | 150 | 369 | 480 | 890 | 1,270 | **~3,952** |
| **trailing-paren slots** | 18,161 | 8,076 | 9,450 | 7,196 | 15,580 | 33,330¹ | **91,807** |
| **trailing-bracket slots** | 1,474 | 810 | 1,914 | 773 | **5,125** | **5,762** | **15,858** |
| **leading-paren slots** | 1,871 | 608 | 753 | 796 | 1,435 | 2,686¹ | **8,149** |
| **leading-bracket slots** | 294 | 40 | 57 | 265 | 541 | 472 | **1,669** |
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
relax-mode filter would catch. Lexer-flag hit-counts in §3.x and the
"by-pattern" rows above are extracted from trailing-paren content
only.)

¹ Corpus F's trailing-paren and leading-paren counts include
substantial source-code noise (the codebase carries heavy OpenACC
GPU-port markers like `lzacc`/`lacc`/`PRESENT`/`:,:` as parenthesised
identifiers inside inline comments). The bracket counts are
correspondingly the meaningful unit-slot count for Corpus F.

**Mixed-convention observation (2026-06-13 follow-up).** Every
corpus uses BOTH paren and bracket delimiter forms for unit
annotations. The "dominant form" simplification in §1 understates
this — trailing-bracket counts range from ~770 (Corpus D) to 5,125
(Corpus E) for the parens-dominant corpora, and the bracket-dominant
Corpus F has 5,762 trailing-bracket sites. The lexer-flag analysis
in §3 is unchanged (flags apply identically to either delimiter's
content), but a project `dimfort.toml` template should configure
**both** `{open=" (", close=")"}` and `{open=" [", close="]"}`
delimiter rules to capture each corpus's full annotation surface.
Implementation of the 8 flags in 0.2.7 should explicitly exercise
both delimiter forms in the test corpus.
