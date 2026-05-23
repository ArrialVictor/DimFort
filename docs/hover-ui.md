# DimFort hover UI

Specification of the markdown DimFort renders in LSP hovers. Six layouts
total: three *surfaces* (function call, subroutine call, expression),
each with a *Short* and *Detailed* variant chosen per-surface in the
extension settings.

This document covers presentation only — the rules behind the rendered
units live in [unit-algebra.md](unit-algebra.md).


## Notation

| Glyph | Meaning |
|---|---|
| `:` | separates an expression (name / source text) from its unit |
| `◂` | separates a target slot (formal param / assignment LHS) from a value flowing into it (actual arg / RHS) — points from value to target |
| `🟢` | known and consistent |
| `🟡` | known partially / contains an unannotated leaf |
| `🔴` | known but inconsistent (unit mismatch) |

Header is always `**{marker} DimFort**` followed by a blank line and the
body. The body uses a fenced code block (` ``` `) so monospace
alignment is preserved.

The header marker aggregates the per-row markers in the body:
**🔴** if any row is 🔴, else **🟡** if any row is 🟡, else **🟢**.
A 🔴 deeper in a sub-tree propagates up — every ancestor whose unit
is `?` because of that violation is also tagged 🔴 (a 🟡 leaf is just
unknown, but a 🔴 leaf forces every operator above it to fail too).


## Surfaces and settings

| Setting key | Default | Triggers when |
|---|---|---|
| `dimfort.hover.functionCalls` | `Short` | cursor is on the callee identifier of a function call |
| `dimfort.hover.subroutineCalls` | `Short` | cursor is on the callee identifier of a `call` |
| `dimfort.hover.expressions` | `Short` | cursor is inside an assignment, call argument, IF/ELSEIF/WHERE condition, DO loop bound, SELECT CASE selector, or on a bare identifier |

`dimfort.trace.enabled` is the master switch: when on (the default),
every hover surface left at `Short` is shown as `Detailed`. Turn it off
to use the per-surface `dimfort.hover.*` levels (which default to
`Short`, raised to `Detailed` individually). Because the clients always
send the per-surface keys, the upgrade keys off the *value*
(`Short` → `Detailed`), not whether the key was provided.


## Conflict resolution

When multiple surfaces would fire at the same cursor position, the
**most-specific node wins**, matching standard LSP behavior:

- Cursor on a bare identifier → identifier hover (even inside an
  assignment or call argument).
- Cursor on the callee identifier of a call → call hover.
- Cursor on whitespace, operators, or punctuation inside an expression
  → the enclosing expression hover.
- Cursor inside a call argument expression but not on the callee →
  expression hover for that argument.


## Layout: function call

### Short (`functionCalls = "Short"`)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/hover-call-short_dark.png">
  <img width="640" src="img/hover-call-short_light.png" alt="Short call hover">
</picture>

```
log : ?

     Signature      Call
  🟢  x : Pa    ◂  p1 : Pa
```

Header `log : <ret>` shows the function name and the formal return unit.
The return is checked at the *enclosing expression* (the slot the call
result flows into), not here — this layout reports what the callable
promises.

Each row: 🟢/🟡/🔴 marker, formal name and unit, `◂`, actual expression
text and resolved unit. Header marker aggregates: 🔴 if any row is 🔴;
else 🟡 if any row is 🟡; else 🟢.


### Detailed (`functionCalls = "Detailed"`)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/hover-call-detailed_dark.png">
  <img width="640" src="img/hover-call-detailed_light.png" alt="Detailed call hover with sub-trees">
</picture>

Same as Short, plus a sub-tree under any non-trivial actual argument
showing how its unit was derived. Bare identifiers and literals do not
expand (the row already shows everything). Sub-tree rows carry their
own 🟢/🟡/🔴 marker, right-aligned after the resolved unit.

```
foo : Pa

     Signature      Call
  🟢  a : Pa    ◂  p1 : Pa
  🟢  b : Pa    ◂  p2 + p1 : Pa
      ├── p2  :  Pa   🟢
      └── p1  :  Pa   🟢
```


## Layout: subroutine call

Identical to function call, with two differences:

- Header is `name:` with no return unit (subroutines have none).
- The aggregate marker reflects only the arg pairing.

### Short

```
update_winds:

     Signature      Call
  🟢  klon : 1   ◂  klon : 1
  🟢  klev : 1   ◂  klev : 1
  🟡  t    : K   ◂  t_local : ?
  🟡  u    : m/s ◂  u_local : ?
  🟢  d_t  : K   ◂  dt_out  : K
```

### Detailed

As above, with sub-trees under any computed actual.


## Layout: expression

The expression surface covers six cursor positions:

1. Bare identifier
2. Binary operator (`+`, `-`, `*`, `/`, `**`) — local check on its
   parent math expression. `+` / `-` are homogeneity-checked
   (operands must match); `*` / `/` / `**` aren't and report the
   sub-expression's resolved unit.
3. Assignment `=` token, or whitespace inside the assignment
4. Relational expression (`<`, `<=`, `==`, `/=`, `>`, `>=`) — has no
   resulting unit, but its two operands must be homogeneous
5. Computed sub-expression (call arg, IF/ELSEIF/WHERE condition body, DO
   loop bound, SELECT CASE selector)
6. Numeric literal


### Short

**Bare identifier**

```
🟢 DimFort

paprs : kg/(m×s²)
```

Header marker: 🟢 if annotated, 🟡 if unannotated.

**Binary operator** (cursor on `+`, `-`, `*`, `/`, `**`)

For `+` and `-` — one-line homogeneity check on the operator's two
operands (the same shape as the assignment hover, since both rules
require unit equality):

```
🟢 DimFort

a : K   ◂   b : K
```

For `*`, `/`, `**` — there's no homogeneity requirement, so the
hover just reports the resolved unit of the whole sub-expression:

```
🟢 DimFort

a * b : K×m
```

**Assignment** (cursor on `=` or whitespace inside the statement)

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/hover-expression-short-assignment_dark.png">
  <img width="640" src="img/hover-expression-short-assignment_light.png" alt="Short assignment hover">
</picture>

A homogeneity violation in the same shape — Pa²/s² vs m/s² (real finding
from a reference workspace trial):

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/hover-expression-short-mismatch_dark.png">
  <img width="640" src="img/hover-expression-short-mismatch_light.png" alt="Short assignment hover with a homogeneity mismatch">
</picture>

```
🟢 DimFort

x : K   ◂   a + b : K
```

One-line homogeneity check. Marker: 🟢 equal, 🔴 mismatch, 🟡 either
side unresolved.

**Initialization autocast (R4.4).** When the entire RHS is a numeric
literal (or unary-minus literal, or arithmetic of literals), it's an
initialization — the literal takes on the LHS's unit and the hover
shows 🟢, e.g. `t : s   ◂   2.0 : s`. No diagnostic fires. This differs
from a literal *inside* a compound expression (`t = c + 2.0`), which
still triggers the D1.5 implicit-cast warning. The assignment marker
comes from `ts_checker._assignment_homogeneity` — the same source of
truth the diagnostic checker and the side panel use, so the hover and
the Problems panel never disagree.

In the detailed-tree view and the side panel, the assignment row shows
**no unit column** (`label  marker`, not `label : unit  marker`) — an
assignment is a statement, not an expression, so it has no unit of its
own; only the homogeneity marker is meaningful.

**Relational expression** (cursor on `<`, `<=`, `==`, `/=`, `>`, `>=`)

```
🟢 DimFort

p : Pa   ◂   0.0 : 1
```

Same homogeneity-check shape as the assignment hover. The relation
itself has no unit; only its two operands' agreement matters. Marker:
🟢 equal, 🔴 mismatch, 🟡 either side unresolved.

**Computed sub-expression**

```
🟢 DimFort

p1 + p2 : kg/(m×s²)
```

Just the resolved unit of the enclosing expression. Marker: 🟢 fully
resolved, 🟡 any leaf unknown.

**Numeric literal**

```
🟢 DimFort

3.0 : 1
```

Numeric literals are dimensionless (`1`). Marker always 🟢.


### Detailed

Cursor on a bare identifier behaves the same as Short — there's nothing
to expand.

For the other three cursor positions, the body is the unit-algebra rule
chain rendered as an ASCII tree. Each row carries a per-node marker in
a right-aligned column so the reader can scan vertically for trouble:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/hover-expression-detailed-clean_dark.png">
  <img width="640" src="img/hover-expression-detailed-clean_light.png" alt="Detailed expression hover, all rows green">
</picture>

```
🟢 DimFort

x = log(p1) + log(p2)
├── x                  :  LOG(Pa²)   🟢
└── log(p1) + log(p2)  :  LOG(Pa²)   🟢   (R4.1)
    ├── log(p1)        :  LOG(Pa)    🟢   (R5.1)
    │   └── p1         :  Pa         🟢
    └── log(p2)        :  LOG(Pa)    🟢   (R5.1)
        └── p2         :  Pa         🟢
```

A violation example — `+` on two different units propagates 🔴 up the
spine:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="img/hover-expression-detailed-violation_dark.png">
  <img width="640" src="img/hover-expression-detailed-violation_light.png" alt="Detailed expression hover with a homogeneity violation propagating up the tree">
</picture>

```
🔴 DimFort

0.5 * (a + b) * c  :  ?  🔴
├── 0.5            :  1  🟢
├── a + b          :  ?  🔴   (R4.1)
│   ├── a          :  m²/s²  🟢
│   └── b          :  m/s²   🟢
└── c              :  ?  🟡
```

Root row is the whole assignment / condition / argument. Each branch is
a sub-expression; rule IDs (R3.1, R4.1, R5.1, …) annotate each rule
fire so the reader can map the trace to
[unit-algebra.md](unit-algebra.md).

**Per-row marker semantics:**

- 🟢 — this node resolved to a unit.
- 🔴 — *local* homogeneity check failed (a `+` / `-` / relational with
  two known-but-different operand units), *or* a 🔴 descendant
  propagated upward through `*` / `/` / a call etc. — anywhere the
  parent's unit is `?` because of the deeper violation.
- 🟡 — the node's unit is `?` for some other reason: an unannotated
  identifier, an intrinsic outside the supported set, a partial
  resolution where one operand is unknown.


## Examples by cursor position

These ground the rules above with concrete cursor placements.

### `r = log(p1) + log(p2)`

| Cursor on | Surface | Short body | Detailed body |
|---|---|---|---|
| `r` | identifier | `r : LOG(Pa²)` | (same as Short) |
| `=` | assignment | `r : LOG(Pa²)   ◂   log(p1) + log(p2) : LOG(Pa²)` | tree |
| `+` | binary operator | `log(p1) : LOG(Pa)   ◂   log(p2) : LOG(Pa)` (homogeneity check on the operands of `+`) | tree |
| `log` (first) | function call | `log : ?` + pairing | + sub-trees |
| `p1` | identifier | `p1 : Pa` | (same as Short) |
| `(`, `)`, spaces | assignment | (same as on `=`) | tree |


### `if (p > 0.0) then`

| Cursor on | Surface | Short body | Detailed body |
|---|---|---|---|
| `p` | identifier | `p : Pa` | (same as Short) |
| `>` | relational | `p : Pa   ◂   0.0 : 1   🔴` (Pa vs dim'less literal — homogeneity violation) | tree |
| `0.0` | numeric literal | `0.0 : 1` | (same as Short) |
| `if`, `then`, `(`, `)` | (no hover) | — | — |


### `call update_winds(p1, p2 + 1.0, t_local)`

| Cursor on | Surface | Short body |
|---|---|---|
| `update_winds` | subroutine call | pairing layout (see above) |
| `p1` | identifier | `p1 : Pa` |
| `p2` | identifier | `p2 : Pa` |
| `+` | binary operator | `p2 : Pa   ◂   1.0 : 1   🔴` (homogeneity violation — Pa vs dim'less literal) |
| `1.0` | numeric literal | `1.0 : 1` |
| `t_local` | identifier | `t_local : ?` (unannotated) |
