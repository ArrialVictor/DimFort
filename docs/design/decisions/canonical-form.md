# Canonical-form nudging — designed, then dropped

**Status:** **Not shipping.** Proposed during 0.2.7 planning as
"Track C Layer 3b — canonical-form suggestion (S025)." Walked back
across a design conversation that established the premise was
wrong. This note records the reasoning so a future maintainer
considering the same feature doesn't re-derive the argument.

The companion design notes that this decision touches:
[permissive-unit-lexer.md](../shipped/permissive-unit-lexer.md)
(the Layer 1/2 reading-permissively work that shipped in 0.2.7)
and the `rewrite.py` Layer 3a flag-paired rewrite rules (which
fire on parse *failure* — and which remain the right scope for
nudging).

## What was proposed

A new diagnostic `S025` at HINT severity, fired on parse *success*
when the input wasn't in "DimFort canonical form." Code action
would rewrite to canonical. Canonical was to be cached on
`UnitExpr` at parse time for LSP performance.

## Why it was dropped — three layers of pushback

### 1. HINT-spam is the wrong delivery mechanism

A team enables `allow_dot_multiplication` precisely because their
codebase has 364+ `J.kg` sites (real number from the §10 empirical
appendix). Lighting every site with a HINT tells them what they
already know — they enabled the flag deliberately. The only
rational response is to disable the diagnostic, which deletes the
nudge value entirely. This is the classic IDE-noise antipattern:
loud enough to annoy, weak enough to ignore.

### 2. Canonicalizing the algebra destroys author intent

`format_unit_source(parse(input))` round-trips through `Unit`, which
is a dimension vector. By that point, semantic information the
author encoded in the *spelling* is gone. The killer case in
climate code is mass-mixing ratio `kg/kg` — which the algebra has
already reduced to dimensionless `1`. A canonicalize-via-algebra
tool would propose `kg/kg → 1`, destroying exactly the
documentation the author wrote.

Siblings of the same pattern: `mol/mol` (volume mixing ratio),
`m^3/m^3` (volume fraction), `m/m` (strain), `K/K` (temperature
ratio), `rad/rad`. In each, the identity ratio is the author's
documentation. The dim-vec cannot distinguish them; therefore a
canonical-form derived from the dim-vec cannot preserve them.

(A narrower scope — "spelling-only canonicalization, reuse the
Layer 3a pipeline, never touch algebra" — solves this layer.
`kg/kg` is invariant under every Layer 3a rule, so the destructive
case never arises. The narrowing was valid; pushback #3 made it
moot.)

### 3. "DimFort canonical" isn't a standard

The decisive layer. What makes a form "DimFort canonical"?

- `^` for exponentiation (not `**`)
- `*` for multiplication (not `·`, `.`, whitespace)
- ASCII digits (not Unicode superscripts)
- Parens for grouping

This is not a community standard. It's the form most amenable to
DimFort's tokenizer. The actually-widely-accepted standards live
in real communities, and they each chose differently:

| Standard | Example | Where it's authoritative |
|---|---|---|
| BIPM / SI Brochure | `m·s⁻¹` | Physics papers, SI-conformant writing |
| udunits2 / CF Conventions | `kg m-2 s-1` | NetCDF metadata — *required* by CF |
| UCUM | `m.s-1` | HL7, medical informatics |
| LaTeX / siunitx | `\si{m \per s}` | Anything rendered through LaTeX |
| DimFort canonical | `kg*m^-2*s^-1` | DimFort. |

A CF-compliant climate codebase writes `kg m-2 s-1` in NetCDF
`units:` attributes because the standard the data layer enforces
requires it. Nudging those authors toward `kg*m^-2*s^-1` would
push them *away from* an actual standard their files have to
comply with, *toward* a notation no one outside DimFort
recognizes. That's anti-adoption.

The principle that closed the discussion: **the job is accepting
their notation, not migrating them off it.** The lexer flags
shipped in 0.2.7 (Tracks B.1, B.2a, B.2b) do exactly that — they
let teams keep writing in their established convention. Adding a
nudge that pulls them back toward DimFort's parser-convenience
form contradicts the empirical commitment made by the lexer
work itself.

## What remains valid

The Layer 3a flag-paired rewrite rules (shipped in 0.2.7 as
[DimFort#106](https://github.com/ArrialVictor/DimFort/pull/106))
fire on parse *failure*: when the input doesn't parse under the
current config, suggest the form that does. This is the genuine
unblock case — the user is already stuck, and the suggestion is
contextually right (canonical *to this project's config*, not
canonical to DimFort's parser).

## Where multi-target rewriting actually belongs

A bidirectional or multi-target rewriter (`m·s⁻¹` ↔ `kg m-2 s-1` ↔
`\si{...}`) is a real feature worth building — but its natural home
is **variable-unit export**, not background diagnostics. Concrete
scenarios:

- Paper writing: emit annotated variables as a LaTeX `siunitx`
  nomenclature table (existing parked idea —
  [LaTeX/siunitx symbol-table export](../../../README.md)
  and the `project_latex_export_idea.md` memory note).
- NetCDF/CF emission: a `dimfort export --cf` that walks annotated
  variables and emits udunits2-canonical strings for `units:`
  attributes.
- Hover display: optionally re-render the hover unit in a chosen
  target convention without touching source.

These share an underlying need — convert a `Unit` to a chosen
target community's notation — and would benefit from a single
multi-target formatter. That formatter is a 0.2.8+ piece, scoped
against concrete export use cases rather than against the abstract
"canonicalize" framing this note rejects.

## What this means for 0.2.7 scope

- `S025` is not assigned. The diagnostic-code registry
  (`docs/reference/diagnostic-codes.md`) skips from S024 to S026
  if a future suggested-code lands.
- No `canonical_form` cache on `UnitExpr`. The frozen-dataclass
  workaround conversation is moot.
- No `dimfort canonicalize` CLI subcommand.
- Layer 3a stays as-is.
