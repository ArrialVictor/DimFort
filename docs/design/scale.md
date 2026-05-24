# Scale checking — design spec for the `scale` branch

Status: **in design**, no implementation yet. Branch created 2026-05-25.

This document is the spec. Code follows the doc, not the other way
around. If something here turns out wrong during implementation,
**update this doc first**, then write the code.

It captures the *what*, the *why*, the data model, the comparison
semantics, the phasing, the diagnostics, the forward-compatibility with
soft-units, the test corpus, and the open questions.


## 1. Problem statement

DimFort today checks **dimension** only. A unit is a 7-tuple of base
exponents plus a `factor` (a `Fraction` prefactor). `equal_dim` compares
the 7-tuples and **ignores the factor**. So these all pass silently
today even though they are magnitude bugs:

- **Multiplicative scale** — same dimension, different prefactor:
  - `hPa` vs `Pa` (×100) — the classic climate-model off-by-100.
  - `g/kg` vs `kg/kg` (×1000) — *on a dimensionless quantity*.
  - `g/m³` vs `kg/m³` (×1000) — `iwcg = iwc*1000` in LSCP.
  - `mb` vs `Pa` — `misc/q_sat.f90`: `r2es` [Pa] / `pres` documented "mb".
  - `L⁻¹` vs `m⁻³` (×1000) — `nb_crystals*1e3`.
- **Affine offset** — same dimension, different zero point:
  - `°C` vs `K` (offset 273.15). The dominant remaining **#006**
    K-literal class (`273.15`, `235.15`, `-21.06+RTT`, `RTT-15.0`,
    `tlcrit`, `t_celsius`).

**Campaign evidence (LMDZ annotation, 2026-05):** the #006 Celsius-literal
family is the single largest remaining H010 class, and LSCP is full of
multiplicative conversions (`iwcg ×1000`, `phpa /100`, `nb_crystals ×1e3`,
Brandes `×1000` mm). A scale layer is the highest-value next feature: it
opens the **dimensionally-clean-but-scale-wrong** bug class DimFort is
currently blind to.

This is the same conclusion mature systems reached: dimension and scale
are different axes, and a units checker that ignores scale misses the
most common real-world numerical bug (unit-prefix mismatch).


## 2. Goals / Non-goals

**Goals**
- Detect same-dimension **multiplicative** scale mismatches (Phase 1).
- Detect same-dimension **affine** offset mismatches (Phase 2, °C/K).
- **Opt-in.** Dimension-only checking stays the first-class default
  (hard requirement). A `scale_mode` flag turns scale on.
- A **structured comparison verdict**, not a bool — so soft-units can
  later remap severity / inject relaxations without touching callers.
- **Per-check diagnostic codes** with `(Dx.y)` rule-markers, so the
  existing `[diagnostics]` per-rule severity override governs them
  (that *is* soft-units' severity axis, for free).

**Non-goals**
- NOT replacing or weakening dimension checking.
- NOT auto-converting values or rewriting source.
- NOT a units-database expansion effort — scale rides on the existing
  `factor` and `.dimfort.toml` unit definitions.
- NOT soft-units (name-hints, families). Scale must be *built so
  soft-units slots in later*, but soft-units is out of scope here.


## 3. Data model

### 3.1 Today

```python
@dataclass(frozen=True)
class Unit:
    dimension: tuple[Exponent, ...]   # 7 base exponents
    factor: Fraction                  # multiplicative prefactor vs base
```

`factor` already exists and already composes correctly through `*`/`/`
in `combine()` (`__mul__`/`__truediv__` multiply/divide the factors).
`equal_dim` ignores it; `equal_strict` (already present) compares it.

### 3.2 Phase 2 addition

```python
    offset: Fraction = Fraction(0)    # affine zero-point shift vs base
```

**Conversion contract:** a raw value `x` written in unit `U` equals, in
the canonical base unit, `x_base = U.factor * x + U.offset`.

| unit  | dimension | factor | offset  |
|-------|-----------|--------|---------|
| K     | Θ         | 1      | 0       |
| °C    | Θ         | 1      | 273.15  |
| Pa    | M/(L·T²)  | 1      | 0       |
| hPa   | M/(L·T²)  | 100    | 0       |
| kg/kg | 1         | 1      | 0       |
| g/kg  | 1         | 1/1000 | 0       |

Note `g/kg` is **dimensionless** (`dimension = {1}`) yet has `factor ≠ 1`.
**Scale must therefore check `factor` even when the dimension is `{1}`** —
this is a defining requirement, not an edge case. `equal_dim` collapses
all dimensionless units today; scale mode must not.

Where do non-unit factors/offsets come from? From the unit table: base
SI units (factor 1, offset 0) plus `.dimfort.toml` definitions
(`hPa`, `g`, `degC`, …) carrying their factor/offset relative to the
base. (Audit item: confirm `unit_config` can express factor- and
offset-bearing unit defs; today it lists `hPa`/`degree` — verify they
carry a factor.)


## 4. Comparison semantics — the structured verdict

The central refactor. Introduce:

```python
@dataclass(frozen=True)
class Verdict:
    kind: Literal["equal", "dim_mismatch",
                  "scale_mismatch", "offset_mismatch"]
    ratio: Fraction | None = None     # scale_mismatch: a.factor / b.factor
    delta: Fraction | None = None     # offset_mismatch: a.offset - b.offset

def compare(a: UnitExpr, b: UnitExpr) -> Verdict: ...
```

Resolution order for two `Unit` leaves:
1. dimensions differ → `dim_mismatch`.
2. dimensions equal, factors differ → `scale_mismatch(ratio)`.
3. dimensions+factors equal, offsets differ → `offset_mismatch(delta)`.
4. all equal → `equal`.

`compare` is **representation-only**: it reports *what* differs, never
*how severe*. Wrappers (`LogWrap`/`ExpWrap`) recurse into `inner` as in
`equal_dim`.

**`scale_mode` collapses the verdict at the policy layer, not in
`compare`:**
- `scale_mode = off` (default): `scale_mismatch` and `offset_mismatch`
  are treated as **compatible** (today's exact behavior — only
  `dim_mismatch` matters). `equal_dim` semantics preserved bit-for-bit.
- `scale_mode = on`: the scale/offset verdicts surface as diagnostics
  (subject to per-code severity).

`equal_dim`/`equal_strict` stay as thin wrappers over `compare` (or
unchanged) so existing call sites are untouched; new scale-aware sites
consult the verdict.


## 5. Diagnostics

New codes, **separate prefix** so the existing severity-override
grouping is clean and the opt-in is obvious:

| code | rule | when | default severity |
|------|------|------|-------------------|
| `S001` | scale (×) | dim-equal, factor ratio ≠ 1 | warning |
| `S002` | offset (+) | dim+factor equal, offset ≠ 0 difference (Phase 2) | warning |

Rationale for a distinct `S` namespace (vs extending `H0xx`): scale is
opt-in and severity-tunable as a group; a dedicated prefix lets a user
write `[diagnostics] S001 = off` to disable all multiplicative-scale
checks without touching dimension checks. **Do NOT hard-code
`scale_mismatch = error`** — default warning, fully overridable. That
overridability is precisely soft-units' severity axis, obtained for free.

**Why `S` doesn't collide with soft-units (RESOLVED 2026-05-25).**
Diagnostic codes name a **kind of mismatch** — a *representation-axis*
concept. Scale introduces a new kind of mismatch (magnitude / offset),
so it earns codes (`S`). **Soft-units introduces no new kind of
mismatch:** per §7 it is a *severity/confidence* remap plus new
*provenance sources* (name-hints, families) over the **same** mismatches.
So soft-units *consumes* the existing `H`/`S` codes at adjusted severity,
and `families` *suppress* them — it never mints a parallel `S`-style
namespace. The namespace is partitioned by mismatch-kind, and soft-units
is not a mismatch-kind, so there is nothing to collide with. The one
advisory soft-units might later want a code for — a **name↔unit hint
conflict** (e.g. `qmin` named like a humidity but annotated `m/s`,
finding #007) — is an annotation-quality diagnostic and belongs in the
existing `U` family (like `U005`) or takes its own free letter (`N`),
never on `S`. Affine offset stays under the same `S` family (`S002`)
because it is still a scale-axis concept.

Message shape (S001): `Scale mismatch: <a> is <ratio>× <b> (same
dimension) — prefer matching units or a documented conversion`.


## 6. Phasing

### Phase 1 — multiplicative scale (no `offset`)
Catches hPa/Pa, g/kg, g/m³, mb/Pa, L⁻¹/m⁻³.

1. `compare()` returning `equal | dim_mismatch | scale_mismatch(ratio)`
   (offset branch stubbed/absent).
2. `scale_mode` flag: config (`.dimfort.toml`), CLI (`--scale`), and
   LSP init option; threaded into the checker `_Ctx`. Default off.
3. `S001` emission at the assignment + operand check sites, gated on
   `scale_mode` and the verdict; severity via the existing override map.
4. Confirm `factor` composes correctly end-to-end (audit `combine`,
   `pow`, wrappers). Ensure dimensionless-but-scaled (`g/kg`) is caught.
5. Tests + corpus (§8). Dimension-only regression: with `scale_mode`
   off, the entire existing suite and the LMDZ baseline are unchanged.

### Phase 2 — affine offset (`offset`, the °C/K problem)
Closes the dominant #006 Celsius class.

6. Add `offset` to `Unit`; conversion contract `x_base = factor·x+offset`.
7. **The temperature problem.** Absolute offset units don't add: `T1+T2`
   (two absolute °C) is meaningless; `T1−T2` is a *difference* (offset
   cancels → result offset 0); `T + ΔT` is fine. The algebra must
   distinguish **absolute** vs **difference** temperatures. Minimal
   model: `+`/`-` on two equal-offset operands → subtraction yields
   offset 0 (a delta), addition of two non-zero-offset absolutes →
   `S002`/flag. Spec the rules precisely before coding Phase 2.
8. `S002` emission. A correct `°C→K` conversion (`+273.15` against a K
   target) validates; a stray `273.15` against a non-temperature or
   wrong-direction target fires. This is what turns the #006 H010s into
   *actionable* scale verdicts.

Phase 2 is deferred until Phase 1 ships and the temperature rules are
written out in full here.


## 7. Forward-compatibility with soft-units

Keep three concerns separate (the build discipline that lets soft-units
slot in without a rewrite):

- **Representation** = `(dimension, factor, offset)` + `compare()`
  verdict. Scale lives here.
- **Policy** = severity per code (`[diagnostics]` overrides), the
  `scale_mode`/`soft_mode` flags. Soft-units' severity axis is already
  here once S-codes are overridable.
- **Provenance** = where a unit claim comes from. Today: `@unit{}`
  annotations + the table. Soft-units later adds low-confidence sources
  (name-hints: a var named like a humidity ⇒ humidity-ish unit; finding
  #007 `qmin`) and family-relaxations (interchangeable unit families) as
  a relaxation step inside `compare`. None of that touches Phase 1/2.


## 8. Test corpus (from the LMDZ campaign)

**Multiplicative (Phase 1):**
- `iwcg = iwc * 1000.` — `kg/m³` → `g/m³` (×1000).
- `phpa = play / 100.` — `Pa` → `hPa` (÷100).
- `q_sat`: `r2es` [Pa] `/ pres` documented "mb" — silent ×100 if a
  caller passes mb.
- `nb_crystals` `×1e3` — `L⁻¹` → `m⁻³`.
- A `g/kg` vs `kg/kg` assignment — the dimensionless-but-scaled case.

**Affine (Phase 2):**
- The #006 family: `tempvig1 = -21.06 + RTT`, `t_glace_min_old = RTT-15.0`,
  `235.15`-cascade in `calc_gammasat`, `tlcrit`/`t_celsius`.

Each becomes a fixture with the *correct* form (no diagnostic) and the
*buggy* form (S001/S002 fires) so the checker is pinned both ways.


## 9. Open questions

1. ~~Diagnostic namespace~~ **RESOLVED (2026-05-25):** `S0xx`. Soft-units
   is a severity+provenance layer, not a mismatch-code family, so it
   does not collide (see §5). Affine offset stays under `S` (`S002`).
2. ~~Default severity of S001~~ **RESOLVED (2026-05-25):** warning,
   fully overridable. Scope: **Phase 1 (multiplicative) only this
   branch**, then reassess before specs+build of Phase 2 (affine).
3. **Unit table** — does `unit_config` / `.dimfort.toml` already let a
   unit def carry a `factor` (and later an `offset`)? Audit before
   Phase 1; this is the source of hPa=100·Pa, g=kg/1000, degC offset.
4. **Factor audit** — verify `factor` survives every algebra path
   (`combine` +/−/*//, `Unit.pow`, wrapper unwrap) so a derived unit's
   factor is trustworthy. Symbolic/rational exponents on a scaled factor
   already restricted (`units.py:352`) — confirm interaction.
5. **`@unit_assume` connection** — the normalized fit form `(D/D₀)^b` is
   the construction that is BOTH dimension- and scale-clean. A scale
   checker's natural fixed point is normalization; it could *nudge* (not
   force) authors toward normalized fits, which would also make today's
   irreducible non-rational-power escapes (Brandes #016, MARCUS)
   checkable. Make the un-normalized cost visible; don't mandate it.
6. **The implicit-reference-constant idiom** (`EXP(zprec_cond)` with an
   implicit `1 [kg/m²]`) — is that in scope for scale, or a separate
   "dimensional-argument-to-EXP" concern? Likely separate.


## 10. Step-by-step plan (Phase 1)

1. `compare()` + `Verdict` in `units.py`; unit tests on the verdict.
2. Re-express `equal_dim`/`equal_strict` over `compare` (or leave and
   add `compare` alongside) — no behavior change; full suite green.
3. `scale_mode` config plumbing (default off); `_Ctx` carries it.
4. `S001` emit sites + severity wiring; gated on `scale_mode`.
5. Fixtures (§8 multiplicative) — buggy fires S001, correct is clean.
6. Regression gate: `scale_mode` off ⇒ existing 680 tests + LMDZ
   baseline byte-identical.
