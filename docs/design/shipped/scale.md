# Scale checking — design spec

Status snapshot (per-phase, current as of 2026-05-30):

| phase | scope | status |
|-------|-------|--------|
| **1** | multiplicative scale (`factor`) + `S001` + `scale_mode` plumbing | **shipped** |
| **2a** | affine offset (`offset`) + `S002` (boundary + affine-invalid op) + absolute `degC` | **shipped** |
| **2b** | named difference units (`Cdeg` / `delta_degC`) + teaching advisory | **not built** |
| **2c** | verified `@unit_affine_conversion` directive + `S003` | **shipped** |
| **3+** | relational / `max` / `min` as S001/S002 emission sites | **not built** |
| future | soft-units (severity remap + name-hints + family relaxations) | not built |

What this doc is: the spec for the scale axis of the unit checker — the
data model (`factor`, `offset`), the structured comparison verdict, the
diagnostics (`S001` / `S002` / `S003`), the affine algebra, the
`@unit_affine_conversion` directive, the uniform scale-aware display
rule, and the open questions.


## 1. Problem statement

**In one line:** scale is a static safety net for the *conversion-mistake*
class — bugs that are **dimensionally clean** (so dimension-checking is
blind to them) but **magnitude- or zero-point-wrong**. Division of labor:
*dimension* catches a **wrong kind of quantity** (`Pa + m/s`); *scale*
catches the **right kind, wrong magnitude/origin** (`hPa` vs `Pa`, `°C`
vs `K`) — same dimension, different `factor` or `offset`, which sail
through dimension-checking because `Pa/Pa = 1` and `K/K = 1`. The
concrete targets are the classic off-by-100/1000 and forgot-°C→K errors.

A `Unit` is a 7-tuple of base exponents plus a `factor` and (Phase 2a) an
`offset`. `equal_dim` compares the 7-tuples only and **ignores** factor
and offset. Without the scale axis these all pass silently:

- **Multiplicative scale** — same dimension, different prefactor:
  - `hPa` vs `Pa` (×100) — the classic off-by-100.
  - `g/kg` vs `kg/kg` (×1000) — *on a dimensionless quantity*.
  - `g/m³` vs `kg/m³` (×1000).
  - `mb` vs `Pa`.
  - `L⁻¹` vs `m⁻³` (×1000).
- **Affine offset** — same dimension, different zero point:
  - `°C` vs `K` (offset 273.15).

The high-value targets are interfaces and the affine `°C`/`K` class:
cross-scale bugs concentrate at module/external *boundaries* (a `degC`
quantity meeting a `K` slot), and the affine path turns the
"untyped K-literal" family into *validated correct conversions*
(silent) vs *wrong-sign / wrong-direction* (fires).


## 2. Goals / Non-goals

**Goals**
- Detect same-dimension **multiplicative** mismatches.
- Detect same-dimension **affine** offset mismatches.
- **Opt-in.** Dimension-only checking stays the first-class default
  (hard requirement). A `scale_mode` flag turns scale on.
- A **structured comparison verdict**, not a bool — so future soft-units
  can remap severity / inject relaxations without touching callers.
- **Per-check diagnostic codes** with `(Sx.y)` rule-markers governed by
  the existing `[diagnostics]` per-rule severity override map.

**Non-goals**
- NOT replacing or weakening dimension checking.
- NOT auto-converting values or rewriting source.
- NOT a units-database expansion effort — scale rides on the existing
  `factor` / `offset` and `.dimfort.toml` unit definitions.
- NOT soft-units (name-hints, families). Future, out of scope here.

### 2.1 Expected yield (be honest)

Scale fires only where two quantities of the **same dimension but
different declared scale** meet at a *check* site (`+ - max min` /
relational / assignment). Consequences:

- **On a uniformly-SI workspace Phase 1 is mostly silent.** A workspace
  where everything is `Pa`, `K`, `kg/kg`, `m/s` (`factor = 1`) gives
  *clean negatives*: confidence, not a bug goldmine.
- **It only bites if quantities are annotated at their *natural* scale**
  (`phpa` as `{hPa}`, not `{Pa}`). Annotate everything as base SI and
  scale mode is inert. Scale changes the annotation discipline.
- **It is structural, not runtime.** It catches a missing / wrong /
  wrong-direction conversion *in the code at a boundary*; it cannot
  catch an arbitrary runtime value that is merely 100× off.
- **Discovery value concentrates in Phase 2 and at interfaces.** The
  affine path is *triage* — splits an undifferentiated K-literal noise
  pile into validated-correct (silent) vs wrong-sign / wrong-direction
  (fires).


## 3. Data model

```python
@dataclass(frozen=True)
class Unit:
    dimension: tuple[Exponent, ...]   # 7 base exponents
    factor: Fraction                  # multiplicative prefactor vs base
    offset: Fraction = Fraction(0)    # affine zero-point shift vs base
```

**Conversion contract:** a raw value `x` written in unit `U` equals, in
the canonical base unit, `x_base = U.factor * x + U.offset`.

| unit  | dimension | factor | offset      |
|-------|-----------|--------|-------------|
| K     | Θ         | 1      | 0           |
| °C    | Θ         | 1      | 273.15      |
| °F    | Θ         | 5/9    | 459.67·5/9  |
| Pa    | M/(L·T²)  | 1      | 0           |
| hPa   | M/(L·T²)  | 100    | 0           |
| kg/kg | 1         | 1      | 0           |
| g/kg  | 1         | 1/1000 | 0           |

Note `g/kg` is **dimensionless** (`dimension = {1}`) yet has `factor ≠ 1`.
**Scale must therefore check `factor` even when the dimension is `{1}`** —
this is a defining requirement, not an edge case. `equal_dim` collapses
all dimensionless units; scale mode must not.

### 3.1 The affine model — absolute vs difference

Temperature (dimension Θ) carries two physically distinct *meanings* on
one dimension:
- an **absolute** temperature is a *point* on an affine scale
  ("it is 20 °C" = 293.15 K) — converting needs the offset;
- a temperature **difference** / ΔT is a *vector* ("rose by 20 °C" =
  rose by 20 K) — the offset does **not** apply.

This is the standard affine-point-vs-vector distinction (cf. timestamp
vs duration). The model encodes it with **one rule, no second
mechanism**:

> **`offset ≠ 0` ⟺ an absolute affine quantity. `offset = 0` ⟺ an
> ordinary quantity** — which covers *every* non-temperature unit,
> absolute kelvin (offset 0 by construction), *and* every temperature
> *difference*. A difference is exactly **the offset-0 projection** of
> its unit: drop the offset, keep the factor. So `Δ°C = {Θ, 1, 0} = K`,
> and `Δ°F = {Θ, 5/9, 0}` (≠ K — differences only collapse to K when the
> factor is 1). The offset algebra therefore only *bites* when a unit
> has `offset ≠ 0`; the overwhelming-majority offset-0 case flows
> through the multiplicative (factor-only) logic untouched.

Phase 2a ships a single absolute `degC` (offset 273.15); a Celsius
*difference* is annotated at its offset-0 projection (`K`). Phase 2b
will add a named difference unit (`Cdeg`/`delta_degC`, offset 0 but a
distinct name) so author *intent* is explicit — the pint /
Boost.Units design, and the pedagogically richer one (forces the
student to ask "absolute or change?", the exact °C/K bug). Migration
2a→2b is non-breaking: 2a annotations stay valid and correctly typed
under 2b.

Non-unit factors/offsets come from the unit table: base SI units
(factor 1, offset 0) plus `.dimfort.toml` definitions (`hPa`, `g`,
`degC`, …) carrying their factor/offset relative to the base. The
derived-unit schema today carries `factor` *and* `offset`;
[default_units.toml](../../../src/dimfort/core/default_units.toml) ships
`degC = { expr = "K", offset = "273.15" }`. `Cdeg`/`delta_degC` are
reserved for 2b.


## 4. Operation algebra

The scale notion is only fully specified once every operation states
what it does to `factor` and `offset`. Two distinct roles:
operations either **propagate** scale (compose it into the result) or
**check** it (require operands to agree). Dimension behavior is shown
for context; it is unchanged by this feature.

| operation | dimension | `factor` | `offset` | role |
|-----------|-----------|----------|----------|------|
| `a * b` | add | multiply `f_a·f_b` | both must be 0; result 0 | propagate |
| `a / b` | subtract | divide `f_a/f_b` | both must be 0; result 0 | propagate |
| `a ** n` | ×`n` | `f_a ** n` | must be 0; result 0 | propagate |
| `a + b`, `a - b` | must be equal | **must be equal** → else `S001(ratio)` | affine algebra below | **check** |
| `max/min(a,b,…)`, `a<b`, `a==b` | must be equal | must be equal *(emission deferred)* | shared frame *(emission deferred)* | **check** |
| `LOG(a)` / `EXP(a)` (wrappers) | recurse `inner` | recurse `inner` | recurse | check inner |

**Key consequences:**
- **Propagating ops never emit `S001`.** Multiplying `hPa` by anything
  is legal; the result simply *carries* `factor 100`. Scale errors are
  only *detected* where operands must agree, mirroring how dimension
  mismatches are only detected there.
- **Dimensionless is not factor-free.** `*`/`/` must keep composing
  `factor` even when the resulting `dimension` is `{1}` (e.g. `g/kg`),
  so that a later `+`/comparison can catch a `g/kg` vs `kg/kg`
  mismatch.
- **`LOG` turns a factor into a log-domain offset.** `LOG(f·u) =
  LOG(f) + LOG(u)`: a factor *inside* a `LogWrap` is an additive shift.
  `compare` recurses into the wrapper and reports `S001(ratio)` on the
  inner factor.

### 4.1 Affine offset — the complete algebra

Vocabulary: a unit is **absolute** iff `offset ≠ 0` (an affine *point*,
e.g. `degC`); otherwise it is **ordinary** (`offset 0` — every
non-temperature unit, absolute `K`, and every temperature *difference*).
The rules below are the standard affine-space algebra (point ± vector);
they are checked *after* dimension and `factor` agree.

**Propagating ops — an absolute operand is ill-defined.** You cannot
scale or multiply an affine *point* (`2 × 20 °C` is meaningless; `2 × 20
K` is fine):
- `a * b`, `a / b`, `a ** n`: **require every operand `offset = 0`**;
  result `offset 0`. Any `offset ≠ 0` operand → **`S002`** via the
  affine-violation path (`_affine_violation` in
  [ts_checker.py](../../../src/dimfort/core/ts_checker.py)).

**`a + b` (check site).** After dim + factor agree, by operand kind:

| `o_a` | `o_b` | meaning | result | verdict |
|-------|-------|---------|--------|---------|
| 0 | 0 | ordinary + ordinary | offset 0 | ok |
| ≠0 | 0 | point + vector | `o_a` | ok |
| 0 | ≠0 | vector + point | `o_b` | ok |
| ≠0 | ≠0 | point + point | — | **`S002`** |

**`a − b` (check site).** Subtraction is asymmetric:

| `o_a` | `o_b` | meaning | result | verdict |
|-------|-------|---------|--------|---------|
| 0 | 0 | ordinary − ordinary | offset 0 | ok |
| ≠0 | 0 | point − vector | `o_a` | ok |
| 0 | ≠0 | vector − point | — | **`S002`** |
| ≠0 | ≠0, **equal** | point − point | **offset 0** (a ΔT!) | ok |
| ≠0 | ≠0, **unequal** | points, different frames | — | **`S002`** |

The headline row is *point − point (equal offset) → offset 0*: `T2[degC]
− T1[degC]` is a **difference**, the result is correctly its offset-0
projection (a ΔT, i.e. `K`). This is the one place an absolute unit
legitimately *produces* an ordinary one.

The `vector − point` row (`ΔT − T_abs` → `S002`) is a **deliberate
strict choice**: it is undefined in affine algebra and has no operator
in Boost.Units. pint diverges here — it permits it and returns an
absolute (`5 Δ°C − 20 °C → -15 °C`), which prioritises "compute
something" over correctness. For a bug-catching / teaching linter we
take the strict side.

**`max/min`, relational (`<`, `==`, …) — comparison check.** Operands
must share a frame: equal dimension, factor, *and* offset. Two
absolutes with unequal offset → ill-defined; absolute vs ordinary
(comparing a temperature to a change) is ill-defined. *These sites are
not S001/S002 emission points yet* — a Phase-3 follow-on shared by both
codes.

**`LOG(a)` / `EXP(a)`.** The argument must be ordinary: `LOG` of an
absolute °C is frame-dependent nonsense. Require `offset 0` inside the
wrapper; recurse `inner` for factor.

**Assignment `lhs = rhs` — the primary catch.** Equal dimension and
factor but **different offset** → `offset_mismatch(delta = o_lhs −
o_rhs)` → **`S002`**. The headline missing-conversion bug:

```fortran
real :: t_k   !< @unit{K}
real :: t_c   !< @unit{degC}
t_k = t_c            ! S002: K vs degC, missing +273.15 conversion
```

Most Phase-2 value is at boundaries (a `degC` meeting a `K` slot),
surfaced by this assignment check — not by the arithmetic rules, which
mainly *reject* nonsense.

**The literal-conversion caveat (honest limitation).** Phase 1 can
bless a correct multiplicative conversion by typing the factor on a
`PARAMETER` (`/ 100[Pa/hPa]`). An **additive offset cannot ride a
multiplicative `PARAMETER`**, so the correct-but-literal form still
fires:

```fortran
t_k = t_c + 273.15   ! S002 (untyped offset conversion): t_c[degC] + a
                     ! bare offset-0 literal stays degC, ≠ K target.
```

This is the offset analogue of Phase 1's untyped-`/100` S001. The
*missing* conversion is caught regardless; the nudge is toward
**keeping absolute temperatures consistently typed**. The structured
escape is `@unit_affine_conversion` (§8), which **verifies** the
arithmetic and blesses a correct conversion.

### 4.2 Numeric literals — the conversion-vs-arithmetic question

A literal multiplier in source is ambiguous: `×1000` might be a **unit
conversion** (kg/m³→g/m³) or **genuine arithmetic** (`×2` doubling,
`π`). The checker cannot tell which from the literal alone.

A bare numeric literal resolves to **dimensionless `factor 1`** (a
*value*, not a unit-factor — "numeric literals are dimensionless"). So
a literal in `*`/`/` does **not** change the expression's unit-factor.

**Consequence 1 — genuine arithmetic never false-positives.**
`y[m] = x[m]*2` → `x*2` is `{m, factor 1}` → `compare` equal → no
S001. `area[m²] = π·r²` → `{m², factor 1}` → equal.

**Consequence 2 — S001 fires on real scale boundaries only.** Two
legitimate cases:
- **Missing conversion (bug):** `phpa[hPa] = play[Pa]` — factor 100 vs
  1 → S001. A genuine off-by-100.
- **Untyped conversion (style):** `phpa[hPa] = play[Pa]/100` — the bare
  `/100` is factor-1-inert, so factors still differ → S001. This is a
  *true* finding: the conversion is not typed. The fix is the existing
  discipline — extract the literal to a typed `PARAMETER` carrying the
  conversion unit (`100. !< @unit{Pa/hPa}`, factor 100); then `compare`
  reconciles and it validates.

**Design principle:** *a conversion is only checkable when its factor
is carried by a typed name (a PARAMETER or a scaled unit). Bare-literal
conversions are opaque — the **missing**-conversion bug is caught
regardless, but **blessing** a correct conversion requires typing it.*


## 5. Comparison semantics — the structured verdict

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
into `inner`.

**`compare` is not the whole offset story.** It detects offset
*mismatches* (operands with different offsets), which covers the
boundary `S002` (`K = degC`). It does **not** detect affine
*operation-validity* failures, where the operands' offsets are equal
but the operation is still ill-defined (`degC + degC`, `2 * degC`,
`LOG(degC)`, `ΔT − degC`). Those live in the affine-violation path
(`_affine_violation` / the `_emit_s002` arithmetic branch) per §4.1.

**`scale_mode` collapses the verdict at the policy layer, not in
`compare`:**
- `scale_mode = off` (default): `scale_mismatch` and `offset_mismatch`
  are treated as **compatible** (today's exact behavior — only
  `dim_mismatch` matters). `equal_dim` semantics preserved bit-for-bit.
- `scale_mode = on`: the scale/offset verdicts surface as diagnostics
  (subject to per-code severity).

`equal_dim`/`equal_strict` stay as thin wrappers over `compare` so
existing call sites are untouched; scale-aware sites consult the
verdict.


## 6. Diagnostics

| code | rule | when | default severity |
|------|------|------|-------------------|
| `S001` | scale (×) | dim-equal, factor ratio ≠ 1 | warning |
| `S002` | offset (+) | affine offset violation — two paths below | warning |
| `S003` | invalid `@unit_affine_conversion` directive | the directive's arithmetic doesn't match the claimed `src→tgt` | **error** |

All three are opt-in (gated on `scale_mode`); severities overridable
via `[diagnostics] S00x`. **Do NOT hard-code any default to error
beyond `S003`** — that overridability is precisely soft-units' future
severity axis, obtained for free.

**`S002` has two detection paths.** Unlike `S001` (always "factors
differ", uniformly a `compare()` `scale_mismatch`), `S002` covers two
structurally different failures:

1. **Boundary `offset_mismatch` (via `compare()`):** two units, same
   dimension and factor, **different** offset — `t_k[K] = t_c[degC]`.
   This is the `offset_mismatch(delta)` verdict of §5. Emitted in
   the assignment branch.
2. **Affine-invalid operation (via `_affine_violation`, *not*
   `compare()`):** an operation ill-defined on an absolute operand
   even when the offsets are **equal** — `degC + degC` (point +
   point), `2 * degC` (scale a point), `ΔT − degC` (vector − point).
   `compare(degC, degC)` returns **`equal`**, so these are invisible
   to `compare()`; they're flagged inside the affine algebra at the
   `+`/`-`/`*`/`/` check sites.

Rationale for a distinct `S` namespace (vs extending `H0xx`): scale is
opt-in and severity-tunable as a group; a dedicated prefix lets a user
write `[diagnostics] S001 = off` to disable all multiplicative-scale
checks without touching dimension checks. Future soft-units does not
collide: it is a *severity/confidence* remap plus new *provenance
sources*, never a new mismatch-kind, so it consumes the existing `H`/
`S` codes at adjusted severity rather than minting a parallel
namespace.

Message shape (S001, as shipped): `Scale mismatch: same dimension
(<dim>) but the magnitudes differ by ×<ratio>. If this is a unit
conversion, carry the factor on a typed PARAMETER; otherwise the units
disagree in scale.` S002 leads with the offset `delta` (or names the
ill-defined operation for path 2).


## 7. The uniform display rule

A piece of UX that landed alongside Phase 2 and is documented here
because every surface consumes it. The rule is one sentence:

> **The displayed unit shows whatever the checker considers significant
> at the current `scale_mode`.** Scale mode off → multiplicative
> factor hidden (it's ignored anyway). Scale mode on → factor surfaced
> everywhere (it's part of the comparison).

| surface (scale-mode on) | rendering | source |
|-------------------------|-----------|--------|
| Expression tree unit column | `format_unit(u, show_factor=True)` | [expr_tree.py](../../../src/dimfort/lsp/expr_tree.py) |
| Scope `unitNormalized` | normalized base-SI form includes the factor | [panel.py](../../../src/dimfort/lsp/panel.py) / `_normalized_unit` |
| Imports `unitNormalized` (and call-site arg/return) | same | [imports.py](../../../src/dimfort/lsp/imports.py) |
| Hover (short and detailed) | same `show_factor=True` path | [hover.py](../../../src/dimfort/lsp/hover.py) |

Affine `offset` rendering is **independent of `show_factor`**: the
offset is the only thing that distinguishes `degC` from `K`, so it is
always shown when present (regardless of scale mode) — otherwise
`degC` would render identically to `K` and the offset semantics would
be invisible. See `format_unit` in
[units.py](../../../src/dimfort/core/units.py).

Worked example:

- `hPa`, scale off: `kg·m⁻¹·s⁻²` (dim expansion only).
- `hPa`, scale on: `100×kg·m⁻¹·s⁻²` (factor visible).
- `degC`, either mode: `K + 273.15` (offset always shown).

One rule across every surface: the display matches what the checker
considers significant. The [panel-info.md](panel-info.md)
refresh is the primary consumer; this section is its specification.

**`_is_conflict` (the interactions consumer) is scale-mode-aware.**
The cross-site conflict detector in
[interactions.py](../../../src/dimfort/core/interactions.py) treats a
`dim_mismatch` as a conflict always and a `scale_mismatch` as a
conflict only under `scale=True`. It does **not** currently treat
`offset_mismatch` as a conflict; if Phase 3 broadens the relational
emission set this is the place to extend (the verdict is already
there).


## 8. `@unit_affine_conversion` — the verified affine-conversion directive

This is the resolution of the literal-conversion caveat (§4.1) and the
piece that makes the affine triage actually pay off.

### 8.1 Why a directive (and why a *verified* one)

A multiplicative conversion rides on a typed `PARAMETER` because units
compose under `*`/`/`: `play[Pa] / PA_PER_HPA[Pa/hPa] = [hPa]`. An
**affine** conversion cannot — addition *preserves* the frame
(`point + vector = point`, §4.1), and there is no unit you can add that turns
a `degC` into a `K`. So a correct `t_k = t_c + 273.15` always fires
`S002`. We need a way to *bless* it.

Crucially, an affine conversion is **not irreducible** — the checker
*knows* both offsets — so it must **not** use `@unit_assume` (which is
for the genuinely underivable: non-rational powers, empirical fits;
and is *trusted/unchecked*). Instead, because the conversion is
**computable**, the directive is **verified**: the checker confirms
the statement actually performs the `src→tgt` conversion and **errors
if it doesn't**. That check is what splits an undifferentiated K-
literal pile into *validated* (blessed, silent) vs *suspicious*
(wrong direction / wrong constant / °C-mixed-with-K → fires).

|                  | `@unit_assume` | `@unit_affine_conversion` |
|------------------|----------------|---------------------------|
| domain           | the **irreducible** (non-rational power, empirical fit) | a **computable** affine frame conversion |
| nature           | **trusted** (asserted, unchecked) | **verified** (checked against the known offsets) |
| justification    | external → `UNIT_ASSUME_REGISTRY.md` | the **check is the justification** → no registry |
| failure mode     | n/a (can't be checked) | **errors** if the arithmetic doesn't fit |

### 8.2 Syntax & placement

A **statement-level directive**, placed exactly like `@unit_assume`:

```fortran
t_k = t_c + RTT    !< @unit_affine_conversion{degC -> K}
```

- Payload: `{ <src-unit> -> <tgt-unit> }`. Arrow form is primary; comma
  form `{src, tgt}` accepted as a synonym (see
  [annotations.py](../../../src/dimfort/core/annotations.py): `RawAffineConv`,
  `_find_affine_invocations`).
- Applies to the **assignment statement** it annotates. One per
  statement.
- `src` and `tgt` must resolve in the active unit table and be
  affine-compatible: same dimension (they may differ in `factor` *and*
  `offset`).
- The directive is gated on `scale_mode`. Scale off ⇒ even a wrong
  conversion is silent.

### 8.3 Semantics — the conversion law

For affine units `S = (dim, f_s, o_s)` and `T = (dim, f_t, o_t)`
(base-value contract `x_base = f·x + o`), the *unique* conversion of
the same physical quantity from `S` to `T` is:

```
x_t = a*·x_s + b*        with    a* = f_s / f_t ,   b* = (o_s − o_t) / f_t
```

- `degC → K`: `f_s=f_t=1, o_s=273.15, o_t=0` ⟹ `a*=1, b*=273.15` →
  `x_K = x_C + 273.15`.
- `K → degC`: `a*=1, b*=−273.15` → `x_C = x_K − 273.15`.
- `degF → K` (when `degF` is in the table): `a*=5/9, b*=459.67·5/9`.

A directive is **valid** iff the annotated statement `lhs = RHS`
satisfies:
1. `lhs`'s unit == `T`, and
2. `RHS` is **affine-linear in exactly one `S`-typed operand** `s`,
   i.e. reduces to `a·s + b` with constant `a`, `b`, and
3. `a == a*` and `b == b*` (exact `Fraction` equality).

Valid ⟹ the statement is the blessed conversion: **suppress
`S001`/`S002` for this statement** (the directive owns scale-checking
here), and treat the result as cleanly `T`. Invalid ⟹ **`S003`**.

### 8.4 Verification algorithm

Implemented as `_verify_affine_conversion` in
[ts_checker.py](../../../src/dimfort/core/ts_checker.py); the core reducer is
`_lin_reduce`.

**(a) Resolve & check the units.** Look up `src`, `tgt`. Both must
exist and share dimension (else `S003`: "not affine-compatible").
Compute `a* = f_s/f_t`, `b* = (o_s − o_t)/f_t` (Fractions).

**(b) Identify the source operand.** Among `RHS`'s sub-terms, exactly
one *leaf* must resolve to unit `S`. **Leaf-only source detection** is
the one non-obvious implementation point: a node is the source `s`
only at a leaf, and a literal / `PARAMETER` with a foldable value is
treated as a **constant first** — even when it is typed like the
source frame. This is what lets the reverse `t_c = t_k - RTT` (`{K ->
degC}`) verify: `RTT` is `K` (= the source frame) but is the 273.15
constant, not a second `K` source operand. A compound RHS like `t_c +
RTT` resolves to `degC` as a whole, so the leaf-only rule also stops
the entire expression being mistaken for the source.

**(c) Reduce `RHS` to `a·s + b`.** A recursive evaluator `lin(node) →
(a, b)` over **values** (units of the constant terms are irrelevant
here — only their numeric value matters):

```
lin(s)                = (1, 0)
lin(const c)          = (0, value(c))
lin(n1 + n2)          = (a1+a2, b1+b2)
lin(n1 - n2)          = (a1-a2, b1-b2)
lin(-n)               = (-a, -b)
lin(n1 * n2)          = if a1==0: (a2*b1, b2*b1)
                        elif a2==0: (a1*b2, b1*b2)
                        else: ERROR (non-linear in s)
lin(n1 / n2)          = if a2==0 and value≠0: (a1/b2, b1/b2)
                        else: ERROR
lin(paren)            = lin(inner)
otherwise / unresolved const / >1 occurrence of s  → ERROR
```

`value(c)` reuses `_resolve_constant_value` (literals + `PARAMETER`
folding). A non-`s` *variable* with no resolvable value ⟹ ERROR ("RHS
not affine-linear in `{src}` with constant coefficients").

**(d) Compare.** `a == a*` and `b == b*` (exact). Match ⟹ valid. Else
`S003`: "stated `degC → K` conversion is wrong: RHS computes `a·s + b`
(a=…, b=…), expected a=1, b=273.15" (the diagnostic shows both so the
author sees *how* it's off).

### 8.5 Interaction with the surrounding walk

A valid directive walks the RHS for genuine dimension/structure errors
(H00x / D1.x still surface) but drops `S001` and `S002` from that walk
— the verification is the sole scale authority for the statement. This
is what lets `(5/9)*t_f` not self-fire under the general affine law:
the affine-op `S002` over the whole annotated RHS is suppressed.

### 8.6 The recommended idiom — a verified conversion function

The cleanest "proper definition": a conversion **function** whose
signature gives callers a clean typed conversion, with the one
irreducible body line verified by the directive:

```fortran
real function c_to_k(t) result(tk)
  real, intent(in) :: t   !< @unit{degC}
  real             :: tk  !< @unit{K}
  tk = t + RTT            !< @unit_affine_conversion{degC -> K}
end function
```

Callers (`x_k = c_to_k(x_c)`) are checked against the `degC → K`
signature — clean and typed — while the single conversion line is
*verified* once. Inline use at a one-off site is also fine.

### 8.7 Edge cases

1. **General affine vs same-factor.** The law is fully general
   (`a*≠1`); the implementation handles `degF`'s `a*=5/9` already.
   The shipped default table has **no `degF` entry**, so the
   non-`a*=1` path is untested in practice — adding `degF` + a
   fixture is the easy way to exercise it.
2. **`src == tgt`** (identity): valid but pointless; a `U`-level
   "redundant conversion directive" info would be a nice-to-have.
3. **Unresolvable conversion constant** (e.g. `tk = t +
   some_runtime_var`): `lin` errors → `S003` "cannot verify: non-
   constant coefficient". Correct — an unverifiable claim must not be
   silently trusted.
4. **Directive on a non-conversion statement** (no `S`/`T`
   involvement): `S003` ("affine-conversion directive but the
   statement isn't one").

### 8.8 Interaction with `@unit_assume`

`@unit_assume` and `@unit_affine_conversion` are *both* statement-level
directives covering the same assignment slot. `@unit_assume` is the
trusted-escape lane (irreducible cases): it suppresses the entire RHS
walk and asserts an LHS unit. `@unit_affine_conversion` is the
verified-conversion lane: it walks the RHS for non-scale checks but
*owns* the scale verdict via the verification. A statement carrying
`@unit_assume` does not run the affine-conversion path at all (the
assume branch is checked first in the assignment-statement handler).


## 9. Configuration plumbing

Scale mode is opt-in, threaded from three sources into `_Ctx.scale_mode`:

- **`.dimfort.toml`** — `[scale] enabled = true`
  ([config.py](../../../src/dimfort/config.py)). Persistent per-workspace
  setting.
- **CLI `--scale`** — `dimfort check --scale`, `dimfort interactions
  --scale` ([cli.py](../../../src/dimfort/cli.py)). Per-invocation override; ORs
  with the config value.
- **LSP `scaleMode` initializationOption** — server-side override,
  applied after config in `_on_initialize`
  ([lsp/server.py](../../../src/dimfort/lsp/server.py)). Boolean.

Companion editors expose a tri-state `"auto" | "on" | "off"`: `"auto"`
defers to `.dimfort.toml`, `"on"`/`"off"` force the LSP init option.
The cycle is bound to `:DimFortCycleScale` (and the Emacs / VSCode
equivalents) — see panel-info.md §Commands.

State on the server side lives in `state.scale_mode`
([lsp/state.py](../../../src/dimfort/lsp/state.py)) and is recomputed into
every `_Ctx` construction (one per file check, per panel query, per
expression-tree walk).


## 10. Test corpus

Built and present in [tests/unit/](../../../tests/unit/):

- **Phase 1 (multiplicative)** — `test_ts_checker.py`: S001 fixtures for
  `hPa`/`Pa`, `g/kg`/`kg/kg`, and the untyped-`/100` style finding plus
  the typed-`PARAMETER` reconciliation.
- **Phase 2a (affine)** — `test_ts_checker.py`: boundary
  `offset_mismatch` (`t_k = t_c`), affine-invalid arithmetic (`degC +
  degC`, `2 * degC`, `ΔT − degC`), the legal point − point → ΔT case
  (must NOT fire), and the `+273.15` untyped-literal caveat.
- **Phase 2c (verified directive)** —
  [test_affine_conversion.py](../../../tests/unit/test_affine_conversion.py)
  covers: valid `t_k = t_c + RTT` and reverse `t_c = t_k - RTT`,
  wrong-direction / wrong-constant / wrong-target / non-affine pairs
  (e.g. `{Pa -> hPa}`) / non-linear RHS / multiple-source-operand all
  firing `S003`. Scanner round-trip tests live in `test_annotations.py`.

Coverage gap to fix: no `degF` entry in `default_units.toml`, so the
general `a*≠1` path of the directive has no fixture. Add `degF` + a
`{degF -> K}` valid + wrong-constant fixture when the path is needed
in anger.

Regression gate is unchanged: with `scale_mode` off, the dimension-only
suite and any baseline must be byte-identical to pre-scale behaviour.


## 11. Cross-references

- **[panel-info.md](panel-info.md)** — primary consumer of
  the scale-aware display rule (§7). The `unitNormalized` field, the
  Expression tree unit column, and the hover renderer all gate on
  `scale_mode`.
- **[markers.md](markers.md)** — `S001` / `S002` / `S003`
  participate in `_MARKER_DIAG_CODES` (markers.md §2). A valid
  directive leaves the statement 🟢; an invalid one (S003) shows 🔴.
- **[interaction-points.md](interaction-points.md)** —
  `_is_conflict` is scale-mode-aware: dimension mismatch is always a
  conflict; scale mismatch is a conflict only under `--scale`. (Offset
  mismatch is not currently surfaced as a cross-site conflict.)
- **[symbolic-exponents.md](symbolic-exponents.md)** /
  **[symbolic-logwrap.md](symbolic-logwrap.md)** —
  orthogonal axes (dimension algebra over symbolic exponents, and the
  `LogWrap`/`ExpWrap` deferral). Scale rides on `factor` / `offset` of
  the inner unit and recurses through wrappers in `compare`; the
  symbolic systems are not affected by `scale_mode`.


## 12. Open questions

**Resolved (kept here for reference):**
1. *Diagnostic namespace.* `S0xx`. Future soft-units is a severity +
   provenance layer, not a mismatch-code family, so it does not
   collide. Affine offset stays under `S` (`S002`); the verified
   directive too (`S003`).
2. *Default severity.* `S001`/`S002` warning, `S003` error. All
   overridable via `[diagnostics]`.
3. *Absolute-vs-difference unit modelling.* Design for (B), implement
   the (A) subset first — shipped as Phase 2a. The model law is
   "`offset ≠ 0` ⟺ absolute; a difference is the offset-0 projection";
   the table reserves named delta units for 2b without using them yet.
4. *Documented offset conversion.* `@unit_affine_conversion{src, tgt}`,
   verified (not trusted) — shipped as Phase 2c.

**Open:**
1. **Relational / `max` / `min` emission (Phase 3).** The verdict
   distinguishes scale and offset mismatches at these sites already,
   but `S001`/`S002` are not emitted there. Worth doing in one pass
   for both codes (and likely for dimension too — `p > q` across
   dimensions is also unemitted today). Separate effort; touches the
   relational/comparison walker, not the algebra.
2. **Named difference units (Phase 2b).** `Cdeg`/`delta_degC` in the
   table + an opt-in "prefer the explicit delta unit" advisory.
   Non-breaking addition (2a annotations stay valid and correctly
   typed). Lands when the teaching use-case does.
3. **`@unit_assume` interaction with scale.** As built, an
   `@unit_assume` suppresses the entire RHS walk on its statement,
   including `S001`/`S002`. That is the intended behaviour for the
   trusted-escape lane but means an assume could silently mask a real
   scale boundary. Worth re-examining if the assume-heavy fits in a
   workspace start hiding boundaries that scale mode would otherwise
   catch.
4. **`degF` coverage.** The general-affine path of the directive is
   implemented but not exercised in tests (no `degF` table entry).
   Cheap to add; flush before relying on the °F path.
5. **Documenting a redundant `src == tgt` directive.** Currently a
   pass; a `U`-level info would help nudge the author to drop it. Low
   priority.
