# Scale checking — design spec for the `scale` branch

Status: **Phase 1 shipped** (multiplicative scale, `S001`). **Phase 2a
shipped** (affine offset, `S002`). **Phase 2c shipped** (the verified
`@unit_affine_conversion` directive, `S003` — see §11). All merged to
`main` 2026-05-25. **Phase 2b (named difference units `Cdeg`, teaching
advisory) — specified (§6, §9.7), not yet built.**

This document is the spec. Code follows the doc, not the other way
around. If something here turns out wrong during implementation,
**update this doc first**, then write the code.

It captures the *what*, the *why*, the data model, the comparison
semantics, the phasing, the diagnostics, the forward-compatibility with
soft-units, the test corpus, and the open questions.


## 1. Problem statement

**In one line:** scale is a static safety net for the *conversion-mistake*
class — bugs that are **dimensionally clean** (so dimension-checking is
blind to them) but **magnitude- or zero-point-wrong**. Division of labor:
*dimension* catches a **wrong kind of quantity** (`Pa + m/s`); *scale*
catches the **right kind, wrong magnitude/origin** (`hPa` vs `Pa`, `°C`
vs `K`) — same dimension, different `factor` or `offset`, which sail
through dimension-checking because `Pa/Pa = 1` and `K/K = 1`. The
concrete targets are the classic off-by-100/1000 and forgot-°C→K errors:
missing conversion, wrong factor, wrong direction, missing/wrong offset.

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

### 2.1 Expected yield (be honest)

Scale fires only where two quantities of the **same dimension but
different declared scale** meet at a *check* site (`+ - max min` /
relational / assignment). That has sharp consequences for how much it
actually finds — state them so the feature is not oversold:

- **On LMDZ specifically, Phase 1 (multiplicative) will be mostly
  silent.** LMDZ is internally rigorous SI (`Pa`, `K`, `kg/kg`, `m/s`,
  all `factor = 1`), so nearly every check site is `factor 1` vs
  `factor 1`. Expect *clean negatives* (conversion-site validation,
  confidence), not a bug goldmine like dimension-checking gave.
- **It only bites if quantities are annotated at their *natural* scale**
  (`phpa` as `{hPa}`, not `{Pa}`). Annotate everything as base SI and
  scale mode is inert. So scale changes the annotation discipline.
- **It is structural, not runtime.** It catches a missing/wrong/
  wrong-direction conversion *in the code at a boundary*; it cannot
  catch an arbitrary value that is merely 100× off at runtime.
- **The high-value targets are interfaces and the affine #006 class.**
  Cross-scale bugs concentrate at module/external *boundaries* (e.g.
  `q_sat` `r2es` [Pa] / `pres` documented "mb") — rare inside uniform-SI
  LMDZ, common in codebases that mix unit systems. And Phase 2's payoff
  is **triage, not discovery**: it splits the ~30 undifferentiated #006
  K-literal H010s into *validated correct °C↔K conversions* (silent) vs
  *wrong-sign / wrong-direction / °C-mixed-with-K* (fires) — turning
  noise-to-eyeball into a short worth-looking-at list.
- **Net:** build Phase 1 first because it is the foundation (the
  `compare` verdict, `scale_mode`, S-codes) that the harder affine
  Phase 2 sits on — but expect Phase 1's LMDZ value to be validation,
  with the discovery value concentrated in Phase 2 and at interfaces.
  Scale is also a general capability worth having beyond LMDZ.


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
| °F    | Θ         | 5/9    | 459.67·5/9 |
| Pa    | M/(L·T²)  | 1      | 0       |
| hPa   | M/(L·T²)  | 100    | 0       |
| kg/kg | 1         | 1      | 0       |
| g/kg  | 1         | 1/1000 | 0       |

Note `g/kg` is **dimensionless** (`dimension = {1}`) yet has `factor ≠ 1`.
**Scale must therefore check `factor` even when the dimension is `{1}`** —
this is a defining requirement, not an edge case. `equal_dim` collapses
all dimensionless units today; scale mode must not.

**The affine model — absolute vs difference (the design that governs
everything below).** Temperature (dimension Θ) carries two physically
distinct *meanings* on one dimension:
- an **absolute** temperature is a *point* on an affine scale ("it is
  20 °C" = 293.15 K) — converting needs the offset;
- a temperature **difference / ΔT** is a *vector* ("rose by 20 °C" =
  rose by 20 K) — the offset does **not** apply.

This is the affine-point-vs-vector distinction (cf. timestamp vs
duration). The model encodes it with **one rule, no second mechanism**:

> **`offset ≠ 0` ⟺ an absolute affine quantity. `offset = 0` ⟺ an
> ordinary quantity** — which covers *every* non-temperature unit,
> absolute kelvin (offset 0 by construction), *and* every temperature
> *difference*. A difference is exactly **the offset-0 projection** of
> its unit: drop the offset, keep the factor. So `Δ°C = {Θ, 1, 0} = K`,
> and `Δ°F = {Θ, 5/9, 0}` (≠ K — differences only collapse to K when the
> factor is 1). The offset algebra therefore only *bites* when a unit has
> `offset ≠ 0`; the overwhelming-majority offset-0 case flows through the
> Phase-1 (factor-only) logic untouched.

**Design decision (2026-05-25): design for (B), implement the (A) subset
first.** Two ways to let an author name a *difference*:
- **(A)** a single absolute `degC` (offset 273.15); a Celsius difference
  is annotated at its offset-0 projection (`K`). Lower annotation burden;
  the `+`-ambiguity dissolves (a delta is offset 0, so "absolute + delta"
  is always the legal exactly-one-offset case). Right for LMDZ, whose
  differences are naturally in K.
- **(B)** additionally a *named* difference unit (`Cdeg` / `delta_degC`,
  offset 0 but a distinct name) so author *intent* is explicit — the
  pint / Boost.Units design, and the pedagogically richer one (it forces
  the student to ask "absolute or change?", the exact °C/K bug).

We **design the model and spec for (B)** — offset lives on the unit,
"difference = offset-0 projection" is the general law, named delta units
are first-class in the table schema even before they're all populated,
and nothing special-cases "Celsius" in the algebra. We **implement the
(A) subset first** (absolute `degC` + the full algebra below + `S002`),
because that captures the entire #006 research payoff. Named delta units
+ a "prefer the explicit delta unit" advisory are the additive (B) step,
lit up when the teaching use-case lands. Because (A)'s annotations stay
valid and correctly-typed under (B), this is a non-breaking refinement,
not a migration (see §9.7).

Where do non-unit factors/offsets come from? From the unit table: base
SI units (factor 1, offset 0) plus `.dimfort.toml` definitions
(`hPa`, `g`, `degC`, …) carrying their factor/offset relative to the
base. **Audit (resolved 2026-05-25):** the table schema today carries
`factor` only (`default_units.toml` derived defs: `expr` + optional
`factor`); there is **no `offset` field and no `degC`**. Phase 2 must
extend the derived-unit schema with an optional `offset` and add
`degC = { expr = "K", offset = 273.15 }` (and reserve `Cdeg`/`delta_degC`
for (B)).


### 3.3 How `factor` and `offset` transform under each operation

The scale notion is only fully specified once every operation states
what it does to `factor` (and, Phase 2, `offset`) — *including the ones
already implemented for `factor`* (`*`, `/`, `**`). Two distinct roles:
operations either **propagate** scale (compose it into the result) or
**check** it (require operands to agree). Dimension behavior is shown
for context; it is unchanged by this feature.

| operation | dimension | `factor` | `offset` (Phase 2) | role |
|-----------|-----------|----------|--------------------|------|
| `a * b` | add | multiply `f_a·f_b` *(implemented)* | both must be 0; result 0 | propagate |
| `a / b` | subtract | divide `f_a/f_b` *(implemented)* | both must be 0; result 0 | propagate |
| `a ** n` | ×`n` | `f_a ** n` *(implemented; non-int on a scaled factor already restricted, `units.py:352`)* | must be 0; result 0 | propagate |
| `a + b`, `a - b` | must be equal | **must be equal** → else `S001(ratio)`; result = common factor | see affine algebra below | **check** |
| `max/min(a,b,…)`, `a<b`, `a==b` | must be equal | **must be equal** → else `S001` | see affine algebra below | **check** |
| `LOG(a)` / `EXP(a)` (wrappers) | recurse `inner` | recurse `inner` (see log-domain note) | recurse | check inner |

**Key consequences (state them, don't assume the reader infers them):**
- **Propagating ops never emit `S001`.** Multiplying `hPa` by anything
  is legal; the result simply *carries* `factor 100`. Scale errors are
  only *detected* where operands must agree (`+ - max min` / relational),
  exactly mirroring how dimension mismatches are only detected there.
  This is why `*`/`/` "already work" — they propagate; they were never a
  check site. The spec states it so the boundary is explicit.
- **Dimensionless is not factor-free.** `*`/`/` must keep composing
  `factor` even when the resulting `dimension` is `{1}` (e.g. `g/kg`),
  so that a later `+`/comparison can catch a `g/kg` vs `kg/kg` mismatch.
- **`LOG` turns a factor into a log-domain offset.** `LOG(f·u) =
  LOG(f) + LOG(u)`: a factor *inside* a `LogWrap` is an additive shift,
  not a ratio. So `compare(LOG(hPa), LOG(Pa))` is a scale mismatch that
  manifests additively. Phase 1: `compare` recurses into the wrapper and
  reports `S001(ratio)` on the inner factor (the ratio is still the
  actionable quantity); **audit the Tetens/FCTTRE `LogWrap` algebra
  (R5.x) for factor handling** before relying on this.

#### Affine offset — the complete algebra (Phase 2)

Vocabulary: a unit is **absolute** iff `offset ≠ 0` (an affine *point*,
e.g. `degC`); otherwise it is **ordinary** (`offset 0` — every
non-temperature unit, absolute `K`, and every temperature *difference* /
vector). The rules below are the standard affine-space algebra (point ±
vector); they are stated per operation so implementation has no room to
guess. Dimension and `factor` are checked exactly as in Phase 1 *first*;
the offset rules only apply once dimension and factor agree.

**Propagating ops — an absolute operand is ill-defined.** You cannot
scale or multiply an affine *point* (`2 × 20 °C` is meaningless;
`2 × 20 K` is fine):
- `a * b`, `a / b`, `a ** n`: **require every operand `offset = 0`**;
  result `offset 0`. Any `offset ≠ 0` operand → **`S002`**. (Factor
  propagates as in Phase 1.)

**`a + b` (check site).** After dim + factor agree, by operand kind:

| `o_a` | `o_b` | meaning | result | verdict |
|-------|-------|---------|--------|---------|
| 0 | 0 | ordinary + ordinary | offset 0 | ok (Phase 1) |
| ≠0 | 0 | point + vector | `o_a` | ok |
| 0 | ≠0 | vector + point | `o_b` | ok |
| ≠0 | ≠0 | point + point | — | **`S002`** |

(`+` is commutative; "exactly one absolute" is always the legal
shifted-point case.)

**`a − b` (check site).** Subtraction is *asymmetric* — this is the part
a naive reading gets wrong:

| `o_a` | `o_b` | meaning | result | verdict |
|-------|-------|---------|--------|---------|
| 0 | 0 | ordinary − ordinary | offset 0 | ok (Phase 1) |
| ≠0 | 0 | point − vector | `o_a` | ok |
| 0 | ≠0 | vector − point | — | **`S002`** |
| ≠0 | ≠0, **equal** | point − point | **offset 0** (a ΔT!) | ok |
| ≠0 | ≠0, **unequal** | points, different frames | — | **`S002`** |

The headline row is *point − point (equal offset) → offset 0*: `T2[degC]
− T1[degC]` is a **difference**, and the result is correctly its
offset-0 projection (a ΔT, i.e. `K`). This is the one place an absolute
unit legitimately *produces* an ordinary one.

The `vector − point` row (`ΔT − T_abs` → `S002`) is a **deliberate
strict choice**: it is undefined in affine algebra and has *no operator*
in Boost.Units, so we flag it. pint diverges here — it *permits* it and
returns an absolute (`5 Δ°C − 20 °C → -15 °C`), which is pint
prioritising "compute something" over correctness. For a bug-catching /
teaching linter we take the strict side (it's almost always a real
mistake). See the verification note at the end of this section.

**`max/min`, relational (`<`, `==`, …) — comparison check.** Operands
must share a frame: equal dimension, factor, *and* offset. `absolute vs
ordinary` (comparing a temperature to a change), or two absolutes with
**unequal** offset → **`S002`**. Two absolutes with equal offset compare
fine (result, for `max/min`, keeps that offset). *(Design only — these
sites are not `S001`/`S002` emission points yet; emitting at relational/
`max`/`min` is a deferred Phase-1 follow-on shared by both codes, §6.)*

**`LOG(a)` / `EXP(a)`.** The argument must be ordinary: `LOG` of an
absolute °C is frame-dependent nonsense. Require `offset 0` inside the
wrapper, else **`S002`**; recurse `inner` for factor as in Phase 1.

**Assignment `lhs = rhs` / `compare(lhs, rhs)` — the primary catch.**
Equal dimension and factor but **different offset** → `offset_mismatch
(delta = o_lhs − o_rhs)` → **`S002`**. This is the headline #006 bug:

```fortran
real :: t_k   !< @unit{K}
real :: t_c   !< @unit{degC}
t_k = t_c            ! S002: K vs degC, missing +273.15 conversion
```

As in Phase 1, **most Phase-2 value is at boundaries** (a `degC` quantity
meeting a `K` slot), surfaced by this assignment/comparison check — not
by the arithmetic rules, which mainly *reject* nonsense.

**The literal-conversion caveat (honest limitation).** Phase 1 could
bless a correct conversion by typing the factor on a `PARAMETER`
(`/ 100[Pa/hPa]`). An **additive offset cannot ride a multiplicative
`PARAMETER`**, so the correct-but-literal form still fires:

```fortran
t_k = t_c + 273.15   ! S002 (untyped offset conversion): t_c[degC] + a
                     ! bare offset-0 literal stays degC, ≠ K target.
```

This is the offset analogue of Phase 1's untyped-`/100` S001: the
*missing* conversion is caught regardless, and the nudge is toward
**keeping absolute temperatures consistently typed** (don't hand-roll
`+273.15`). A future "documented conversion" escape (an offset-aware
sibling of `@unit_assume`, or recognising `degC + <its exact offset>` →
`K`) could bless the literal form; **out of scope for the (A) subset**,
noted in §9.

**Worked #006 examples** (what fires once the Celsius quantities are
typed `degC`; today they are untyped K-literals and so silent):
- `tempvig1[K] = -21.06 + RTT` with `RTT = 273.15[K]`: `-21.06` is an
  untyped Celsius literal; typed as `degC`, `degC + K(offset 0)` → degC,
  assigned to `K` → **`S002`** (untyped °C→K). Validates only if the
  Celsius value never claims to be K without conversion.
- `t_glace_min_old[K] = RTT - 15.0`: `RTT[K] − 15.0`(ordinary) → K; clean
  *if* `RTT` is K and `15.0` a ΔT. Fires only if `15.0` is typed `degC`.
- A genuine bug it now catches: `T1[degC] + T2[degC]` (two absolutes
  added) → **`S002`** point+point.

**Verification against pint (2026-05-25, pint 0.25.3).** Every rule above
was cross-checked against `pint` (the reference Python units library with
explicit offset support). **9 of 10 core rows match exactly**, confirmed
empirically: `degC − degC → Δ°C`; `degC ± Δ → degC`; `degC + degC`,
`2 * degC`, `degC * degC` all raise `OffsetUnitCalculusError` (= our
`S002`); `Δ ± Δ` and scaling a `Δ` are fine; in-frame comparison works.
The **one divergence is `vector − point`** (`ΔT − T_abs`): pint permits it
(`-15 °C`), we flag `S002` — the deliberate strict choice noted above
(Boost.Units and affine algebra side with us). Two corroborations: pint
*rejects* `degC + 273.15` outright (`DimensionalityError` — can't add a
bare number to a temperature), strengthening the untyped-literal caveat;
and pint *auto-converts* cross-frame `degC < degF`, whereas we flag — but
°C/°F differ in `factor` (5/9), so our **`S001`** fires first regardless.


### 3.4 Numeric literals — the conversion-vs-arithmetic question

A literal multiplier in source is ambiguous: `×1000` might be a **unit
conversion** (kg/m³→g/m³) or **genuine arithmetic** (`×2` doubling,
`π`). DimFort cannot tell which from the literal alone. This looked
(during the Phase-1 build, 2026-05-25) like a fatal false-positive
source; on analysis it is not, **because of how literals are modelled**:

- A bare numeric literal resolves to **dimensionless `factor 1`** (a
  *value*, not a unit-factor — `ts_checker.py`: "numeric literals are
  dimensionless"). So a literal in `*`/`/` does **not** change the
  expression's unit-factor.

**Consequence 1 — genuine arithmetic never false-positives.**
`y[m] = x[m]*2` → `x*2` is `{m, factor 1}` → `compare` = equal → no
S001. `area[m²] = π·r²` → `{m², factor 1}` → equal. A `×2`/`π`/`0.5`
multiplier leaves the unit-factor untouched, so scale never fires on it.

**Consequence 2 — S001 fires on real scale boundaries only.** Two cases,
both legitimate:
- **Missing conversion (bug):** `phpa[hPa] = play[Pa]` — factor 100 vs 1
  → S001. A genuine off-by-100.
- **Untyped conversion (style):** `phpa[hPa] = play[Pa]/100` — the bare
  `/100` is factor-1-inert, so factors still differ → S001. This is a
  *true* finding: the conversion is not typed. The **fix is the existing
  discipline** — extract the literal to a typed PARAMETER carrying the
  conversion unit (`100. !< @unit{Pa/hPa}`, factor 100); then `compare`
  reconciles (`play / 100[Pa/hPa]` resolves to `{hPa, factor 100}`) and
  it validates. Identical in spirit to the #006 K-literal → PARAMETER
  moves and the irreducible-only policy.

So **scale-checking and the PARAMETER-extraction discipline reinforce
each other**: scale surfaces untyped conversions; typing them makes the
conversion explicit and checkable. The rejected alternative — *fold
literal values into the factor* (treat `1000` as `factor 1000`) — is the
one that genuinely false-positives on arithmetic (`×2` → "scale
mismatch") and inverts confusingly; **do not do that.**

**Design principle (stated outright):** *a conversion is only checkable
when its factor is carried by a typed name (a PARAMETER or a scaled
unit). Bare-literal conversions are opaque — the **missing**-conversion
bug is caught regardless, but **blessing** a correct conversion requires
typing it.* This puts DimFort in the strongly-typed-UoM family (F#,
Frink, pint, uom: conversions are explicit typed operations), but as a
*linter on annotated unitless Fortran* — it cannot force typed
conversions, so it warns toward them. (The alternative, CamFort-style
*unit inference* on literals, silently accepts present-literal
conversions and only catches missing ones; we choose the opinionated
end, consistent with the #006 / irreducible-only PARAMETER discipline.)
No system can validate the *magnitude* of a bare literal (`/10` vs
`/100` both pass) — they track units, not whether a number equals its
unit's factor.

**The noise/nudge dial — RESOLVED (2026-05-25): option (a).** S001 fires
whenever `scale_mode` is on and a boundary has a factor mismatch — both
*missing* (`= play`) and *untyped* (`= play/100`) conversions — at
**warning** severity, opt-in. Rationale: it is the nudge-toward-typing
the discipline wants (consistent with H010); it is the simpler emit (no
literal-detection guard); and it is **near-silent on the current LMDZ
annotations** (everything is base-SI ⇒ factors are 1 ⇒ nothing to
mismatch — noise only appears once scaled units are annotated). Option
(b) — *fire only on literal-free boundaries, silent when a literal is
present* — remains documented as a **fallback config narrowing**
(`[scale] untyped_conversions = warn|off`) if (a) proves noisy in
practice. This is a severity/scope choice, not a correctness wall.


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

`compare` is **representation-only**: it reports *what* differs between
two units, never *how severe*. Wrappers (`LogWrap`/`ExpWrap`) recurse
into `inner` as in `equal_dim`.

**`compare` is not the whole offset story.** It detects offset
*mismatches* (operands with different offsets), which covers `S001` at
every site and the boundary `S002` (`K = degC`). It does **not** detect
affine *operation-validity* failures, where the operands' offsets are
equal but the operation is still ill-defined (`degC + degC`, `2 * degC`,
`LOG(degC)`, `ΔT − degC`). Those live in `combine()`/`power()` per §3.3
and §5 path 2 — do not expect `compare()` to surface them.

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
| `S002` | offset (+) | affine offset violation (Phase 2) — see two paths below | warning |

**`S002` has two detection paths — this matters for the build.** Unlike
`S001` (always "factors differ", uniformly a `compare()` `scale_mismatch`
wherever checked), `S002` covers two structurally different failures:

1. **Boundary `offset_mismatch` (via `compare()`):** two units, same
   dimension and factor, **different** offset — `t_k[K] = t_c[degC]`.
   This is the `offset_mismatch(delta)` verdict of §4.
2. **Affine-invalid operation (via `combine()`/`power()`, *not*
   `compare()`):** an operation ill-defined on an absolute operand even
   when the offsets are **equal** — `degC + degC` (point + point),
   `2 * degC` (scale a point), `ΔT − degC` (vector − point), `LOG(degC)`.
   `compare(degC, degC)` returns **`equal`**, so these are invisible to
   `compare()`; they must be flagged by the affine algebra inside
   `combine()`/`power()` (§3.3). Path 2 is the easy one to forget — the
   point+point case has *identical* operands.

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

Message shape (S001, as shipped): `Scale mismatch: same dimension
(<dim>) but the magnitudes differ by ×<ratio>. If this is a unit
conversion, carry the factor on a typed PARAMETER; otherwise the units
disagree in scale.` S002 will follow the same shape, leading with the
offset `delta` (or naming the ill-defined operation for path-2 cases).


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
Closes the dominant #006 Celsius class. The algebra is fully specified in
§3.2–§3.3 (written out before any code, per the build discipline). Split
into two additive milestones; **2a is the (A) subset**, **2b is the (B)
refinement** (see §3.2 design decision, §9.7 migration note).

**Milestone 2a — absolute `degC` + the algebra + `S002`:**
6. Add `offset: Fraction = 0` to `Unit`; conversion contract
   `x_base = factor·x + offset`. Extend the derived-unit table schema
   with an optional `offset`; add `degC = { expr = "K", offset = 273.15 }`.
7. Add `delta` to `Verdict` and the `offset_mismatch` branch to
   `compare()` (dim+factor equal, offset differs). Implement the affine
   algebra of §3.3 in `combine()`/`power()`/wrapper unwrap: propagating
   ops reject absolute operands; `+`/`-` per the point/vector tables;
   relational/`max`/`min` require a shared frame.
8. `S002` emission at the **sites `S001` actually fires today —
   assignment + binary `+`/`-`** (ts_checker.py: the `op in ("+","-")`
   branch and the assignment verdict path), gated on `scale_mode`,
   default warning, overridable. Relational / `max` / `min` are **not**
   `S001` sites yet (a Phase-1 deferred follow-on); adding them is a
   shared S001+S002 extension, out of 2a scope. The assignment/`compare`
   offset_mismatch is the headline #006 catch (`K = degC`); the path-2
   affine-operation checks (§5) live in `combine()`/`power()`.
9. Tests + the §8 affine corpus (correct form silent, buggy form fires).
   Regression: `scale_mode` off ⇒ unchanged; offset-0-everywhere (today's
   annotations) ⇒ S002 silent.

**Milestone 2b — named difference units + pedagogy (the (B) step):**
10. Add `Cdeg`/`delta_degC` (offset 0, distinct name) to the table, plus
    any `Kdeg` alias; first-class but numerically the offset-0 projection.
11. Optional opt-in advisory nudging a Celsius *difference* annotated `K`
    toward the explicit delta unit (severity-tunable; default off to keep
    2a code non-breaking). This is the teaching-oriented surface.

Everything 2b adds is additive: 2a annotations stay valid and correctly
typed under 2b (§9.7). 2a ships first for the research payoff; 2b lands
when the teaching use-case does.


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

**Affine (Phase 2a — absolute `degC` + the algebra):**
- `t_k[K] = t_c[degC]` — the headline missing-conversion offset_mismatch
  → S002. The correct counterpart: both sides `K` (or both `degC`).
- `T1[degC] + T2[degC]` — point + point → S002. Correct: `T[degC] +
  dT[K]` (absolute + difference) → degC, silent.
- `T2[degC] - T1[degC]` — point − point, equal offset → ΔT (offset 0),
  **silent** (must NOT fire — pins the legal difference case).
- `2.0 * T[degC]` — scaling an absolute → S002. Correct: `2.0 * dT[K]`.
- The #006 family once typed: `tempvig1 = -21.06 + RTT` (RTT=273.15[K]),
  `t_glace_min_old = RTT-15.0`, `235.15`-cascade in `calc_gammasat`,
  `tlcrit`/`t_celsius` — untyped K-literals today (silent), fire once the
  Celsius quantities are annotated `degC`.
- `t_k = t_c + 273.15` — the untyped-offset-conversion caveat: fires S002
  (a fixture pinning the documented limitation, not a bug).

**Affine (Phase 2b — named difference units):**
- A Celsius *difference* annotated `Cdeg` vs the same annotated `K` —
  must be numerically interchangeable (compare → equal), pinning that 2b
  is a non-breaking refinement of 2a.

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
7. ~~Absolute-vs-difference unit modelling~~ **RESOLVED (2026-05-25):
   design for (B), implement the (A) subset first** (see §3.2). The model
   law is "`offset ≠ 0` ⟺ absolute; a difference is the offset-0
   projection"; the spec/table reserve named delta units (`Cdeg`),
   2a ships a single absolute `degC`, 2b adds the explicit delta names +
   a teaching advisory.
   **Migration is non-breaking (the reason (A)-now is safe):** every
   annotation valid under (A) stays valid *and correctly typed* under
   (B) — a Celsius difference written `K` is numerically identical to the
   `Cdeg` (B) would prefer, and the offset algebra (§3.3) is unchanged
   between milestones (2b only *adds* names + an opt-in advisory; it
   reinterprets nothing). Costs of the 2a→2b step are bounded: a
   curriculum/doc refinement and one opt-in (default-off) advisory — no
   verdict flips, no data-model rework, provided 2a keeps the offset on
   the unit and never special-cases "Celsius" in the algebra.
8. ~~Documented offset conversion~~ **RESOLVED (2026-05-25): a *verified*
   affine-conversion directive `@unit_affine_conversion{src, tgt}`, fully
   specified in §11.** Not a trusted escape (that would mis-use
   `@unit_assume`, which is for the irreducible): DimFort *knows* both
   offsets, so the directive is **checked** — it blesses a correct °C↔K
   conversion *and errors on a wrong one*, which is what turns the #006
   triage into validated-vs-suspicious. Phase **2c** (after 2a ships).


## 10. Step-by-step plan (Phase 1)

1. `compare()` + `Verdict` in `units.py`; unit tests on the verdict.
2. Re-express `equal_dim`/`equal_strict` over `compare` (or leave and
   add `compare` alongside) — no behavior change; full suite green.
3. `scale_mode` config plumbing (default off); `_Ctx` carries it.
4. `S001` emit sites + severity wiring; gated on `scale_mode`.
5. Fixtures (§8 multiplicative) — buggy fires S001, correct is clean.
6. Regression gate: `scale_mode` off ⇒ existing 680 tests + LMDZ
   baseline byte-identical.


## 11. `@unit_affine_conversion` — the verified affine-conversion directive (Phase 2c)

Status: **SHIPPED 2026-05-25** (built on top of Phase 2a). This is the
resolution of §9.8 and the piece that makes the #006 triage actually pay
off. Orthogonal to Phase 2b (delta units, still future).

**As built (notes vs the spec below):**
- The general affine law (§11.3, handles `a*≠1`) is implemented — *not*
  the same-factor subset of §11.7.1 — at no extra cost, because the
  directive suppresses the path-2 affine-op `S002` over the whole annotated
  RHS (so `(5/9)*t_f` doesn't self-fire). No `degF` table entry ships, so
  the °F path is untested in practice; add `degF` + a fixture to exercise it.
- **Leaf-only source detection (the one non-obvious implementation point):**
  a node is the source `s` only at a *leaf*, and a literal/PARAMETER with a
  foldable value is treated as a **constant first** — even when it is typed
  like the source frame. This is what lets the reverse `t_c = t_k - RTT`
  (`{K -> degC}`) verify: `RTT` is `K` (= the source frame) but is the
  273.15 constant, not a second `K` source operand. A compound RHS like
  `t_c + RTT` resolves to `degC` as a whole, so the leaf-only rule also
  stops the entire expression being mistaken for the source.
- The directive is gated on `scale_mode` (the whole scale family is opt-in):
  scale off ⇒ even a wrong conversion is silent.
- A valid directive walks the RHS for genuine dimension/structure errors
  (H00x / D1.x still surface) but drops `S001`/`S002` from that walk — the
  verification is the sole scale authority for the statement.
- Code/tests: `annotations.py` (`RawAffineConv`, `_find_affine_invocations`),
  `ts_checker.py` (`_lin_reduce`, `_verify_affine_conversion`, `_emit_s003`,
  the assignment branch), `multifile.py` wiring, `S003` in the LSP marker
  set. Tests: `tests/unit/test_affine_conversion.py` (§11.9 corpus) +
  scanner tests in `test_annotations.py`.

### 11.1 Why a directive (and why a *verified* one)

A multiplicative conversion rides on a typed `PARAMETER` because units
compose under `*`/`/`: `play[Pa] / PA_PER_HPA[Pa/hPa] = [hPa]`. An
**affine** (offset) conversion cannot — addition *preserves* the frame
(`point + vector = point`, §3.3), and there is no unit you can add that
turns a `degC` into a `K` (same dimension and factor; only the zero-point
differs). So a correct `t_k = t_c + 273.15` always fires `S002` (§3.3
caveat). We need a way to *bless* it.

Crucially, an affine conversion is **not irreducible** — DimFort *knows*
both offsets — so it must **not** use `@unit_assume` (which is for the
genuinely underivable: non-rational powers, empirical fits; and is
*trusted/unchecked*, hence the registry). Instead, because the conversion
is **computable**, the directive is **verified**: DimFort confirms the
statement actually performs the `src→tgt` conversion and **errors if it
doesn't**. That check is what splits the #006 class into *validated*
(blessed, silent) vs *suspicious* (wrong direction / wrong constant /
°C-mixed-with-K → fires) — the whole point.

Contrast, to keep the two directives cleanly separated:

| | `@unit_assume` | `@unit_affine_conversion` |
|---|---|---|
| domain | the **irreducible** (non-rational power, empirical fit) | a **computable** affine frame conversion |
| nature | **trusted** (asserted, unchecked) | **verified** (checked against the known offsets) |
| justification | external → `UNIT_ASSUME_REGISTRY.md` | the **check is the justification** → no registry |
| failure mode | n/a (can't be checked) | **errors** if the arithmetic doesn't fit |

### 11.2 Syntax & placement

A **statement-level directive**, placed exactly like `@unit_assume` (reuse
that plumbing — it already maps a source line → directive payload):

```fortran
t_k = t_c + RTT    !< @unit_affine_conversion{degC -> K}
```

- Payload: `{ <src-unit> -> <tgt-unit> }`. Arrow form is primary (the
  direction is the point); accept `{src, tgt}` (comma) as a synonym.
- Applies to the **assignment statement** it annotates. One per statement.
- `src` and `tgt` are unit names that must resolve in the active table and
  be **affine-compatible**: same dimension (they may differ in `factor`
  *and* `offset`). (Provisional name; `@unit_convert` is a shorter
  alternative — bikeshed, the semantics are what matter.)

### 11.3 Semantics — the conversion law it asserts

For affine units `S = (dim, f_s, o_s)` and `T = (dim, f_t, o_t)`
(base-value contract `x_base = f·x + o`, §3.2), the *unique* conversion of
the same physical quantity from `S` to `T` is:

```
x_t = a*·x_s + b*        with    a* = f_s / f_t ,   b* = (o_s − o_t) / f_t
```

- `degC → K`: `f_s=f_t=1, o_s=273.15, o_t=0` ⟹ `a*=1, b*=273.15` → `x_K = x_C + 273.15`.
- `K → degC`: `a*=1, b*=−273.15` → `x_C = x_K − 273.15`.
- `degF → K`: `f_s=5/9, f_t=1` ⟹ `a*=5/9, b*=459.67·5/9` → `x_K = (5/9)·x_F + 459.67·5/9`.

A directive is **valid** iff the annotated statement `lhs = RHS` satisfies:
1. `lhs`'s unit == `T` (the declared target), and
2. `RHS` is **affine-linear in exactly one `S`-typed operand** `s`, i.e.
   reduces to `a·s + b` with constant `a`, `b`, and
3. `a == a*` and `b == b*` (exact `Fraction` equality).

Valid ⟹ the statement is the blessed conversion: **suppress `S002` /
`offset_mismatch` here**, and treat the result as cleanly `T`. Invalid ⟹
**`S003`** (§11.5) with the specific reason.

### 11.4 Verification algorithm (the crux — spec it exactly)

**(a) Resolve & check the units.** Look up `src`, `tgt`. Both must exist
and share dimension (else `S003`: "not affine-compatible"). Compute
`a* = f_s/f_t`, `b* = (o_s − o_t)/f_t` (Fractions).

**(b) Identify the source operand.** Among `RHS`'s sub-terms, exactly one
must resolve to unit `S` (a variable, or a `PARAMETER` typed `S`). That is
`s`. Zero or more-than-one `S`-typed operand ⟹ `S003` ("need exactly one
`{src}` operand"). *Bare-literal sources must first be typed* (the #006 →
`PARAMETER` discipline H010 already nudges) — this keeps `s` unambiguous
and composes with the existing rule.

**(c) Reduce `RHS` to `a·s + b`.** A recursive evaluator `lin(node) →
(a, b)` over **values** (units of the constant terms are irrelevant here —
only their numeric value matters):

```
lin(s)                = (1, 0)                       # the source operand
lin(const c)          = (0, value(c))                # literal or PARAMETER value
lin(n1 + n2)          = (a1+a2, b1+b2)
lin(n1 - n2)          = (a1-a2, b1-b2)
lin(-n)               = (-a, -b)
lin(n1 * n2)          = if a1==0: (a2*b1, b2*b1)      # const * linear
                        elif a2==0: (a1*b2, b1*b2)    # linear * const
                        else: ERROR (non-linear in s)
lin(n1 / n2)          = if a2==0 and value≠0: (a1/b2, b1/b2)
                        else: ERROR
lin(paren)            = lin(inner)
otherwise / unresolved const / >1 occurrence of s  → ERROR
```

`value(c)` reuses `_resolve_constant_value` (literals + PARAMETER folding).
A non-`s` *variable* with no resolvable value ⟹ ERROR ("RHS not
affine-linear in `{src}` with constant coefficients" — a conversion must
not pull in other variables). `RTT[K] = 273.15` contributes `(0, 273.15)`;
its `K` annotation doesn't matter to the value check.

**(d) Compare.** `a == a*` and `b == b*` (exact). Match ⟹ valid. Else
`S003`: "stated `degC → K` conversion is wrong: RHS computes `a·s + b`
(a=…, b=…), expected a=1, b=273.15" (the diagnostic shows both so the
author sees *how* it's off — wrong sign, wrong constant, etc.).

### 11.5 Diagnostics

| code | when | default severity |
|------|------|-------------------|
| `S003` | an `@unit_affine_conversion` directive that does **not** verify (non-affine units / wrong target / not affine-linear in src / `a`,`b` mismatch) | **error** |

`S003` is **error** by default (unlike S001/S002 warnings): a *claimed*
conversion that is actually wrong is a bug, not a style nudge. Overridable
via `[diagnostics] S003`. A **valid** directive emits nothing and
*suppresses* the `S002` that the statement would otherwise raise. It does
**not** touch `H010`/`D1.5` on a bare additive literal — naming the
constant is an orthogonal nudge (and the recommended idiom uses a
`PARAMETER`, so no `H010` arises).

### 11.6 The recommended idiom — a verified conversion function

The cleanest "proper definition": a conversion **function** whose
signature gives callers a clean typed conversion, with the one irreducible
body line verified by the directive:

```fortran
real function c_to_k(t) result(tk)
  real, intent(in) :: t   !< @unit{degC}
  real             :: tk  !< @unit{K}
  tk = t + RTT            !< @unit_affine_conversion{degC -> K}
end function
```

Callers (`x_k = c_to_k(x_c)`) are checked against the `degC → K` signature
— clean and typed — while the single conversion line is *verified* once.
This is the pattern to teach. Inline use at one-off sites is also fine.

### 11.7 Edge cases & open questions

1. **Same-factor only, or general affine?** The law (§11.3) is fully
   general (handles `degF`'s `a*=5/9`). Implementation MAY ship the
   same-factor (`a*=1`, pure-offset) case first (covers all of #006 °C/K)
   and extend to `a*≠1` later — `lin` already returns `a`, so the check is
   the same; only `degF`-style table entries differ. Decide at build time.
2. **`src == tgt`** (identity, `a*=1,b*=0`): valid but pointless; consider
   a `U`-level "redundant conversion directive" info. Low priority.
3. **Unresolvable conversion constant** (e.g. `tk = t + some_runtime_var`):
   `lin` errors → `S003` "cannot verify: non-constant coefficient". Correct
   — an unverifiable claim must not be silently trusted (that's the
   `@unit_assume` lane, deliberately not reused here).
4. **Naming** — `@unit_affine_conversion` vs `@unit_convert` vs `@affine`.
   Provisional; settle before shipping.
5. **Directive on a non-conversion statement** (no `S`/`T` involvement):
   `S003` ("affine-conversion directive but the statement isn't one").

### 11.8 Step-by-step plan (Phase 2c — for a fresh session)

Prereq: Phase 2a (offset, `S002`) is merged (it is — `main`).
1. **Parse** the directive: reuse the `@unit_assume` statement-directive
   scanner (`ts_checker` already collects `assumes: dict[line → payload]`);
   add a parallel `affine_conversions: dict[line → (src, tgt)]`. Same
   `!< @...{...}` comment lexing.
2. **`lin` reducer** in `ts_checker` (or `units`): `Node → (Fraction a,
   Fraction b) | None`, per §11.4(c), reusing `_resolve` (to find the `S`
   operand) and `_resolve_constant_value` (for constants).
3. **Emit site**: in the assignment check, if the statement has a
   directive, run §11.4(a)–(d). Valid → set a flag that *suppresses* the
   `S002`/`offset_mismatch` for this statement (and reframes the result to
   `T`). Invalid → `_emit_s003(node, reason)`.
4. **Severity**: register `S003` default `error` in the override map path
   (it flows through `finalize_diagnostics` like the rest).
5. **Markers**: free — `S003` is a consistency-family code, so add it to
   `_MARKER_DIAG_CODES` (markers.md §2) so a bad directive shows 🔴 in the
   panel/hover; a *valid* directive leaves the statement 🟢 (no diagnostic).
6. **Tests + §11.9 corpus**; docs: this section + `annotations.md` +
   `UNIT_ASSUME_REGISTRY.md` note (affine-conversion is *not* an assume and
   needs no registry entry — state it so the discipline stays clear).

### 11.9 Test corpus

Each as a fixture, valid form silent (S002 suppressed) + each invalid form
firing `S003`:
- **Valid** `t_k = t_c + RTT  !< @unit_affine_conversion{degC -> K}` with
  `RTT = 273.15 !< @unit{K}` → silent (a*=1,b*=273.15 match).
- **Valid reverse** `t_c = t_k - RTT  !< @unit_affine_conversion{K -> degC}`.
- **Valid via function** the `c_to_k` idiom (§11.6) — body silent, callers clean.
- **Wrong direction** `t_k = t_c - RTT  !< @{degC -> K}` → `S003` (b=−273.15, expected +273.15).
- **Wrong constant** `t_k = t_c + 100. !< @{degC -> K}` → `S003` (b=100≠273.15).
- **Wrong target** `lhs` typed `degC` but `{degC -> K}` → `S003` (target mismatch).
- **Non-affine** `{Pa -> hPa}` (a factor pair, not affine) → `S003` ("use a typed PARAMETER for multiplicative conversions").
- **Non-linear / extra variable** `t_k = t_c * other + RTT` → `S003`.
- **`degF -> K`** (if the general case ships) `t_k = (5./9.)*t_f + b !< @{degF -> K}` → silent.
