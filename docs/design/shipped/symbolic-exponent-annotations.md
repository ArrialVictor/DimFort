# Symbolic-exponent annotations — surface widening for the `Exponent` algebra

**Status:** **Shipped in 0.2.7.** Drafted 2026-06-15 to close the
long-known parked annotation-surface gap left by the 2026-05-22
symbolic-exponents work; landed 2026-06-16 alongside the §3.0
baseline integer-exponent widening from the permissive-unit-lexer
note. Both halves share ``parse_exp`` as their convergence point —
the implementation covers integer and symbolic shapes in one rewrite.

Sibling notes:
- [`symbolic-exponents.md`](symbolic-exponents.md) — the algebra
  side (shipped 2026-05-22). This note widens the surface
  annotation parser to expose what that algebra already represents.
- [`permissive-unit-lexer.md`](permissive-unit-lexer.md) —
  composition story for the lexer flags
  (`allow_fortran_star_star`, `allow_latex_braces`, etc.) and the
  §3.0 baseline grammar widening.
- [`polymorphic-units.md`](polymorphic-units.md) — interaction with
  parametric polymorphism (`'a`).

## 1. Problem — the algebra is wider than the surface

The 2026-05-22 symbolic-exponents work shipped the `Exponent`
class: a linear form over Q with named opaque generators,
representing exponents like

```
Exponent({"kappa": 1}, 0)                # kappa
Exponent({"kappa": 2}, Fraction(-1, 3))  # 2*kappa - 1/3
Exponent({"kappa": 1, "lambda": -1}, 0)  # kappa - lambda
```

This closed the Exner-kappa family of D1.4 ("power exponent is
not a literal rational") by letting the **checker** construct
`Exponent` objects when it sees variable-as-exponent in source
code (`p ** kappa` builds `Pa^Exponent({"kappa": 1}, 0)` once
`kappa` resolves to a dim'less PARAMETER in scope).

**The annotation surface was not widened in lockstep.** The
shipped `parse_exp` in `src/dimfort/core/units.py` only accepts
`int | (int/int) | -exp`. So:

| form | algebra supports? | source-side accepts? | annotation surface accepts? |
|---|:---:|:---:|:---:|
| `m^2`, `m^-1`              | ✓ | ✓ | ✓ |
| `m^(2)`, `m^(-1)`          | ✓ | n/a | ✓ (after lexer note §3.0) |
| `m^(2/3)`                  | ✓ | n/a | ✓ |
| `m^kappa`                  | ✓ | ✓ (`p**kappa` in expressions) | **✗** |
| `m^(2*kappa - 1/3)`        | ✓ | ✓ (in expressions) | **✗** |
| `'a^kappa`                 | ✓ | n/a | **✗** |
| `m^(kappa - lambda)`       | ✓ | ✓ (in expressions) | **✗** |

The asymmetry has been acknowledged on main since 2026-05-22 —
the shipped design doc says (`symbolic-exponents.md` line 8-9):

> the cross-module symbolic-PARAMETER case closes the Exner-kappa
> family of D1.4s but **a Tetens-multiplier annotation gap was
> surfaced and parked as a separate follow-up.**

A physicist annotating downstream variables of an Exner / Tetens
expression can't say *"this output has unit Pa^kappa"* in source.
They can `@unit_assume{}` it (escape valve), but that's the wrong
posture for a unit the algebra can express directly.

## 2. Goal

Close the gap. After 0.2.7:

> The annotation surface accepts every exponent shape the
> `Exponent` algebra represents — bare identifiers, paren'd
> identifiers, linear combinations of identifiers with rational
> coefficients, and rational-only literal forms continue to work.

Resolution of identifier names happens at **check time** via the
existing symbol-table the checker already consults for
variable-as-exponent in source expressions. The annotation parser
gains a thin dependency on the symbol-resolution callback but does
NOT take a symbol-table dependency at parse time.

Identifier vocabulary is **open** — any identifier passes the
parser; the checker either resolves it to a dim'less PARAMETER
(success) or fires the same D1.4 / D1.7 diagnostic it fires for
the equivalent source-side expression.

## 3. Surface grammar

After the [lexer-note §3.0](permissive-unit-lexer.md) widening
(strict `^` accepts all four integer shapes), the exponent
production becomes:

```
exp = signed_atom | (linear_form) | -exp
signed_atom = [+-]? atom
atom = INT | IDENT
linear_form = lin_term (('+' | '-') lin_term)*
lin_term = rational '*' IDENT | INT '*' IDENT | IDENT | rational | INT
rational = INT '/' INT     # parens-required form survives unchanged at (int/int)
```

In prose: an exponent is either an atom (a bare integer or a bare
identifier, optionally signed), OR a parenthesised linear
expression over identifiers with rational coefficients, OR a
unary-minus of either.

### 3.1 Shapes accepted

| shape | grammar slot | example | semantic Exponent |
|---|---|---|---|
| Bare positive integer        | `signed_atom`  | `m^2`                | `Exponent({}, 2)` |
| Bare negative integer        | `signed_atom`  | `m^-1`               | `Exponent({}, -1)` |
| Paren positive integer       | `(signed_atom)`| `m^(2)`              | `Exponent({}, 2)` |
| Paren negative integer       | `(signed_atom)`| `m^(-1)`             | `Exponent({}, -1)` |
| Rational with parens         | `(rational)`   | `m^(2/3)`            | `Exponent({}, 2/3)` |
| Negated rational             | `-exp`         | `m^-(2/3)`           | `Exponent({}, -2/3)` |
| Bare identifier              | `signed_atom`  | `m^kappa`            | `Exponent({"kappa": 1}, 0)` |
| Bare signed identifier       | `signed_atom`  | `m^-kappa`           | `Exponent({"kappa": -1}, 0)` |
| Paren identifier             | `(signed_atom)`| `m^(kappa)`          | `Exponent({"kappa": 1}, 0)` |
| Paren signed identifier      | `(signed_atom)`| `m^(-kappa)`         | `Exponent({"kappa": -1}, 0)` |
| Coef × identifier            | `linear_form`  | `m^(2*kappa)`        | `Exponent({"kappa": 2}, 0)` |
| Rational coef × identifier   | `linear_form`  | `m^(1/3*kappa)`      | `Exponent({"kappa": 1/3}, 0)` |
| Ident + const                | `linear_form`  | `m^(kappa+1)`        | `Exponent({"kappa": 1}, 1)` |
| Const - ident                | `linear_form`  | `m^(1-kappa)`        | `Exponent({"kappa": -1}, 1)` |
| Multi-ident linear           | `linear_form`  | `m^(kappa-lambda)`   | `Exponent({"kappa": 1, "lambda": -1}, 0)` |
| Multi-ident + coef + const   | `linear_form`  | `m^(2*kappa-lambda+1/3)` | `Exponent({"kappa": 2, "lambda": -1}, 1/3)` |

The `*` between coefficient and identifier is **required** in the
paren'd linear form to disambiguate from juxtaposition; `2 kappa`
without `*` does NOT parse.

### 3.2 Shapes rejected

- Non-linear functions of identifiers: `m^(kappa^2)`,
  `m^sin(kappa)`. The algebra is a linear form over Q; non-linear
  shapes have no representation.
- Cross-product of symbolic identifiers: `m^(kappa*lambda)`. The
  shipped algebra doc explicitly says (`symbolic-exponents.md`
  line 76): *"Exponents-with-symbols (e.g. `kappa * lambda`) are
  not supported."* Same rejection here.
- Identifier as denominator: `m^(1/kappa)`. Reciprocal of a
  symbolic exponent isn't expressible in the algebra.
- Float literals: `m^(1.5*kappa)`. The Exponent algebra is over
  rationals only; floats don't compose into the algebra.

Each rejected shape gets a clear diagnostic explaining the
mismatch with the algebra and pointing at `@unit_assume{}` as the
escape valve.

### 3.3 Composition with lexer flags

The widened `parse_exp` lives inside the unit-string parser, which
runs after delimiter extraction. All shape rules apply uniformly
regardless of which lexer flags are on:

| flag | example with symbolic exponent | rendered Exponent |
|---|---|---|
| `allow_fortran_star_star` ON | `Pa**kappa`              | `Exponent({"kappa": 1}, 0)` |
| `allow_fortran_star_star` ON | `Pa**(2*kappa-1)`        | `Exponent({"kappa": 2}, -1)` |
| `allow_latex_braces` ON      | `Pa^{kappa}`             | `Exponent({"kappa": 1}, 0)` |
| `allow_latex_braces` ON      | `Pa^{2*kappa-1/3}`       | `Exponent({"kappa": 2}, -1/3)` |
| both flags ON                | `Pa**{2*kappa}`          | `Exponent({"kappa": 2}, 0)` — `**` alias applies first (→ `Pa^{2*kappa}`), then LaTeX-brace rewrite (→ `Pa^(2*kappa)`); no conflict. Empirically rare (0 survey occurrences) but composes cleanly. |

The `**` operator continues to act as a pure alias for `^` after
§3.0 widening — its exponent surface is identical. The
`allow_latex_braces` rewrite path retargets `^{<content>}` to the
strict-grammar shape; with the widening, `^{kappa+1}` rewrites to
`^(kappa+1)` cleanly.

### 3.4 Composition with polymorphism

The polymorphic tyvar mechanism (`'a`) carries its own per-tyvar
`Exponent`. Today's grammar accepts `'a^2`, `'a^-1`, `'a^(2/3)`.
The widening extends symmetrically:

| shape | example | semantic |
|---|---|---|
| Tyvar with symbolic exponent       | `'a^kappa`              | `tyvars=(('a', Exponent({"kappa": 1}, 0)),)` |
| Tyvar with linear form             | `'a^(kappa-1)`          | `tyvars=(('a', Exponent({"kappa": 1}, -1)),)` |
| Tyvar with coefficient × symbol    | `'a^(2*kappa)`          | `tyvars=(('a', Exponent({"kappa": 2}, 0)),)` |

Same parser path; tyvar handling at `parse_factor` is unchanged,
the widened `parse_exp` handles the exponent after the `^`.

## 4. Resolution semantics — check-time, open vocabulary

### 4.1 Parse time

The annotation parser emits an `Exponent` with **unresolved
identifier names** as the symbol generators. Concrete:

```
@unit{Pa^kappa}                # parser builds Exponent({"kappa": 1}, 0)
@unit{Pa^(2*kappa-1)}          # parser builds Exponent({"kappa": 2}, -1)
@unit{Pa^foo}                  # parser builds Exponent({"foo": 1}, 0) — no validation yet
```

No symbol-table lookup. No vocabulary check. The parser doesn't
know whether `kappa` is a declared PARAMETER, or whether it
resolves to a dim'less unit. The shape is recorded; resolution is
deferred.

This preserves the unit parser's existing dependency surface
(`UnitTable` only). No symbol-table injection.

### 4.2 Check time

When the checker encounters the annotated `Unit` in a context that
requires resolving its `Exponent` (most checks, formatting, hover
rendering), it walks the symbol generators and resolves each name
against the file's PARAMETER table — **exactly the same
resolution path** that handles variable-as-exponent in source
expressions today (`p ** kappa` resolution).

Outcomes per resolved identifier:

| identifier resolves to | diagnostic | example |
|---|---|---|
| Dim'less PARAMETER (real, declared, has `@unit{1}`)        | success — Exponent verified | `kappa = 2.0/7.0` in `phys_constants.F90` |
| Dim'd PARAMETER                                            | **D1.7** — exponent must be dim'less | `kappa` declared as `kg` |
| Undeclared identifier                                      | **D1.4** — exponent reference unresolved | `kappa` never declared |
| Non-PARAMETER (regular variable)                           | **D1.4-variant** — exponent must be statically constant | `kappa` is a runtime assignment |
| Mismatched scope (e.g. local shadowing a module ident)     | resolves per shadowing rule | per symbol-table normal rule |

These are the SAME diagnostic codes the source-side path fires.
A user who writes `@unit{Pa^kapa}` (typo of `kappa`) gets the
identical D1.4 they'd get from writing `p ** kapa` in Fortran
source. One mental model, two surfaces.

### 4.3 Why open vocabulary

The source-side path is open — anyone writing `p ** anything` in
Fortran gets per-symbol resolution against scope, no parser-level
vocabulary check. The annotation surface mirroring that posture
delivers:

- Same error patterns for typos, dim mismatches, undeclared
  identifiers across both surfaces.
- No need for a `dimfort.toml` `[symbolic_exponents]` vocabulary
  config (which would have to be maintained per-project and would
  drift from actual PARAMETER declarations).
- No "parser said yes but checker said no" stratification — the
  parser only checks shapes, the checker checks identities.

A closed vocabulary would require either auto-population from
PARAMETER declarations (synthesising what the symbol table already
provides) or hand-curation (a maintenance burden with no
correctness benefit). Both reject for the same reason: the
canonical "is `kappa` a known symbolic exponent here?" check
already exists at check time.

## 5. Diagnostic messages

The shipped D1.4 / D1.7 codes carry forward. Two new message
variants for the annotation-surface case:

### 5.1 Identifier resolves to a dim'd quantity

```
@unit{Pa^kappa} on src/foo.F90:42
  kappa is declared at src/constants_mod.F:18 with unit `kg`
  Exponents must be dim'less. Did you intend a different identifier?
  (D1.7 — exponent must be dim'less)
```

Same code, same wording template the source-side path uses; only
the location pointer differs (annotation vs expression).

### 5.2 Identifier is undeclared in scope

```
@unit{Pa^kappa} on src/foo.F90:42
  kappa is not declared in the visible scope of foo.F90
  Did you mean one of: kappa_si (phys_constants.F90:18), kappa_ec (constants_mod.F:54)?
  (D1.4 — exponent identifier unresolved)
```

Fuzzy-match suggestion uses the same approach the existing
"unknown unit identifier" suggestion does.

### 5.3 Parser-time rejection of non-linear / cross-product shapes

When the parser itself rejects the shape (§3.2 rejects), the
diagnostic explains the algebra constraint:

```
@unit{m^(kappa*lambda)} on src/bar.F90:17
  Cross-product of symbolic identifiers (kappa*lambda) is not
  representable in DimFort's Exponent algebra.
  Workarounds: (a) annotate via @unit_assume{} if kappa*lambda is
  empirically constant; (b) rephrase the dimensional argument.
  (U002 — unit parse error)
```

## 6. Test plan

Coverage extends the existing `tests/unit/test_units.py` parser
tests. The fixtures cover every shape in §3.1's table plus
composition with lexer flags and polymorphism. Outline:

| # | shape | flags | expected |
|---:|---|---|---|
| 1  | `Pa^kappa`              | none      | parses to `Pa^Exponent({"kappa":1},0)` |
| 2  | `Pa^-kappa`             | none      | parses to `Pa^Exponent({"kappa":-1},0)` |
| 3  | `Pa^(kappa)`            | none      | parses to `Pa^Exponent({"kappa":1},0)` |
| 4  | `Pa^(2*kappa-1)`        | none      | parses to `Pa^Exponent({"kappa":2},-1)` |
| 5  | `Pa^(kappa-lambda)`     | none      | parses to `Pa^Exponent({"kappa":1,"lambda":-1},0)` |
| 6  | `Pa^(kappa*lambda)`     | none      | parse error (cross-product not allowed) |
| 7  | `Pa^(1/kappa)`          | none      | parse error (ident in denominator) |
| 8  | `Pa**kappa`             | starstar  | same AST as Pa^kappa |
| 9  | `Pa^{kappa}`            | braces    | same AST as Pa^kappa |
| 10 | `Pa^{2*kappa-1/3}`      | braces    | same AST as Pa^(2*kappa-1/3) |
| 11 | `Pa**{kappa}`           | both      | same AST as `Pa^kappa` (`**` alias then LaTeX-brace rewrite; per §3.3 composition) |
| 12 | `'a^kappa`              | none      | tyvar with symbolic exponent |
| 13 | `'a^(2*kappa)`          | none      | tyvar with coef × ident |
| 14 | `Pa^kappa * m^lambda`   | none      | composes through `parse_term` correctly |
| 15 | `Pa^(kappa) / s^lambda` | none      | composes through `parse_unit` (division) |
| 16 | `Pa^(1.5*kappa)`        | none      | parse error (float coef) |
| 17 | `Pa^kappa^2`            | none      | parse error (chained exponentiation undefined) |

Integration tests in `tests/unit/test_ts_checker.py` cover the
check-time resolution paths against fixture Fortran files with
known PARAMETER declarations.

## 7. Implementation sketch

### 7.1 Parser changes (`src/dimfort/core/units.py`)

- `parse_exp` extends to accept `IDENT` tokens and linear-form
  parsing inside parens.
- New helper `_parse_linear_form` walks `lin_term (('+'|'-')
  lin_term)*`, building a `dict[str, Fraction]` (the linear-form
  coefficient map) plus a `Fraction` constant accumulator.
- Final step: construct `Exponent(coef_map, constant)`. The
  existing `Exponent.__post_init__` handles canonicalization
  (drop zero coefficients, sort by name).

### 7.2 Resolution hook

The checker already has a "resolve identifier in scope" callback
for the source-side path. Refactor so the same callback walks an
`Exponent`'s coefficient-map keys when the annotation surface
produces a symbolic-exponent `Unit`. Two call sites converge on
one implementation.

### 7.3 Pretty-print

`format_unit` already handles `Exponent` (per the read of
`units.py` — it renders `^({exp})` for non-fraction exponents).
Verify rendering of the new shapes is readable:

```
Pa^kappa            -> Pa^κ           (or Pa^kappa per renderer)
Pa^(2*kappa-1)      -> Pa^(2κ - 1)
'a^kappa            -> 'a^κ
```

Exact Unicode-vs-ASCII rendering is settled by the parked
verbatim-input display normalization decision (deferred to a
future cycle).

### 7.4 Diagnostic message wiring

New message templates for the cases in §5; reuse existing D1.4 /
D1.7 emitter infrastructure.

## 8. Performance considerations

- Parser: O(linear-form-length) at parse time; bounded small in
  practice (real annotations rarely exceed 3-4 terms).
- Check-time resolution: same cost as the existing source-side
  path; no new bottleneck.
- Hover rendering: `format_unit` already handles `Exponent`; no
  additional per-call work.

Benchmark gate: lex/parse throughput regression < 5 % on the
validation workspace; if observed, investigate.

## 9. Interaction with other 0.2.7 work

- **Permissive lexer (§3.0 + flags):** the widened `parse_exp`
  composes uniformly. All lexer flags pass exponent content to the
  same `parse_exp`; new shapes participate in all flag-paired
  rewrite rules (Layer 3a) and canonical-form suggestions (Layer
  3b) per the udunits2 design.
- **Per-variable continuation-attach:** orthogonal. Same parser
  runs on every extracted annotation regardless of which decl line
  it attaches to.
- **udunits2 vocabulary (Layer 2):** orthogonal. Symbolic
  exponents are about exponent shape; vocabulary is about unit-
  identifier names. No collision.

## 10. Out of scope

- **Non-linear shapes** — `kappa^2`, `sin(kappa)`,
  `kappa*lambda`. Algebra constraint, not parser-cost; remain
  rejected.
- **Float coefficients** — `1.5*kappa`. The Exponent algebra is
  rational. Workaround: `(3/2)*kappa`.
- **LogWrap × symbolic exponent** — the shipped doc parks this:
  *"`LOG(p) * kappa` (LogWrap × scalar). The current LogWrap
  algebra fires D1.4 if `kappa` is not a literal. The symbolic
  path could produce `LogWrap(Pa^kappa)` if we generalize the
  wrapper similarly. TBD — start with `**` only, revisit LogWrap
  once the basics work."* Same disposition here — annotation
  surface support follows the algebra-side decision.
- **Cross-module symbolic-exponent identity** — `kappa` from
  module A and `kappa` from module B compare equal by name today.
  Shadowing edge cases inherit whatever the symbol-table resolution
  produces; annotation surface doesn't change that policy.

## 11. Open questions

1. **Bare identifier in compound expressions outside parens.**
   `m^kappa*s` — does the `*` bind to `m^kappa` (product of two
   units) or extend the exponent (`m^(kappa*s)`)? The proposed
   grammar binds `*` to unit-product; the linear-form `*` is only
   inside parens. This is the same disambiguation as `m^2*s` today
   (product of `m^2` and `s`). Consistent, but worth a test.
2. **Unicode rendering of identifier exponents** — should `kappa`
   render as `κ` in `format_unit`? The shipped renderer uses
   `^(<expr>)` for non-fraction exponents; symbol-to-Greek mapping
   is a follow-up cosmetic question, not load-bearing.
3. **Per-symbol scope qualifier** — `comconst_mod::kappa` vs bare
   `kappa`. The shipped doc parks this as a future decision; same
   disposition here.

## 12. Decisions log

- **2026-05-22** — `Exponent` algebra shipped, supporting symbolic
  exponents as linear forms over Q. Surface annotation parser
  deliberately left unchanged; gap parked.
- **2026-06-15** — design pass: close the parked gap in 0.2.7.
  Three forks settled:
  - **Scope: T3** (full linear forms over Q with identifier
    generators) — matches the algebra exactly; avoids future
    parser revisits.
  - **Resolution timing: check-time** — reuses existing source-
    side resolution path; preserves the unit parser's small
    dependency surface.
  - **Vocabulary: open** — mirrors the source-side path; any
    identifier accepted at parse time, resolved at check time.
- **2026-06-15** — `parse_exp` becomes the convergence point for
  all integer / rational / identifier / linear-form exponents.
  Composition with lexer flags (`**`, `^{…}`) inherits the
  widening automatically.
