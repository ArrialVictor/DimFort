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


## Surfaces and settings

| Setting key | Default | Triggers when |
|---|---|---|
| `dimfort.hover.functionCalls` | `Short` | cursor is on the callee identifier of a function call |
| `dimfort.hover.subroutineCalls` | `Short` | cursor is on the callee identifier of a `call` |
| `dimfort.hover.expressions` | `Short` | cursor is inside an assignment, call argument, IF/ELSEIF/WHERE condition, DO loop bound, SELECT CASE selector, or on a bare identifier |

`dimfort.trace.enabled` is a legacy master switch: when on, any surface
still at `Short` is upgraded to `Detailed`. Per-surface settings always
win when explicit.


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

Same as Short, plus a sub-tree under any non-trivial actual argument
showing how its unit was derived. Bare identifiers and literals do not
expand (the row already shows everything).

```
foo : Pa

     Signature      Call
  🟢  a : Pa    ◂  p1 : Pa
  🟢  b : Pa    ◂  p2 + p1 : Pa
      ├── p2  :  Pa
      └── p1  :  Pa
```


## Layout: subroutine call

Identical to function call, with two differences:

- Header is `name:` with no return unit (subroutines have none).
- The aggregate marker reflects only the arg pairing.

### Short

```
drag_noro_strato:

     Signature      Call
  🟢  klon : 1   ◂  klon : 1
  🟢  klev : 1   ◂  klev : 1
  🟡  t    : K   ◂  t_seri : ?
  🟡  u    : m/s ◂  u_seri : ?
  🟢  d_t  : K   ◂  d_t_oro : K
```

### Detailed

As above, with sub-trees under any computed actual.


## Layout: expression

The expression surface covers five cursor positions:

1. Bare identifier
2. Assignment statement (cursor not on a bare identifier — on `=`, an
   operator, or whitespace inside the statement)
3. Relational expression (`<`, `<=`, `==`, `/=`, `>`, `>=`) — has no
   resulting unit, but its two operands must be homogeneous
4. Computed sub-expression (call arg, IF/ELSEIF/WHERE condition body, DO
   loop bound, SELECT CASE selector)
5. Numeric literal


### Short

**Bare identifier**

```
🟢 DimFort

paprs : kg/(m×s²)
```

Header marker: 🟢 if annotated, 🟡 if unannotated.

**Assignment** (cursor not on a bare identifier — on `=`, operator,
whitespace inside the statement)

```
🟢 DimFort

x : K   ◂   a + b : K
```

One-line homogeneity check. Marker: 🟢 equal, 🔴 mismatch, 🟡 either
side unresolved.

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
chain rendered as an ASCII tree:

```
🟢 DimFort

x = log(p1) + log(p2)
├── x  :  LOG(Pa²)
└── log(p1) + log(p2)  :  LOG(Pa²)  (R4.1)
    ├── log(p1)  :  LOG(Pa)  (R5.1)
    │   └── p1   :  Pa
    └── log(p2)  :  LOG(Pa)  (R5.1)
        └── p2   :  Pa
```

Root row is the whole assignment / condition / argument. Each branch is
a sub-expression; rule IDs (R3.1, R4.1, R5.1, …) annotate each rule
fire so the reader can map the trace to
[unit-algebra.md](unit-algebra.md).


## Examples by cursor position

These ground the rules above with concrete cursor placements.

### `r = log(p1) + log(p2)`

| Cursor on | Surface | Short body | Detailed body |
|---|---|---|---|
| `r` | identifier | `r : LOG(Pa²)` | (same as Short) |
| `=` | assignment | `r : LOG(Pa²)   ◂   log(p1) + log(p2) : LOG(Pa²)` | tree |
| `+` | assignment | (same) | tree |
| `log` (first) | function call | `log : ?` + pairing | + sub-trees |
| `p1` | identifier | `p1 : Pa` | (same as Short) |
| `(`, `)`, spaces | assignment | (same as on `=`) | tree |


### `if (p > 0.0) then`

| Cursor on | Surface | Short body | Detailed body |
|---|---|---|---|
| `p` | identifier | `p : Pa` | (same as Short) |
| `>` | relational | `p : Pa   ◂   0.0 : 1   🟡` (homogeneity check; literals are dim'less) | tree |
| `0.0` | numeric literal | `0.0 : 1` | (same as Short) |
| `if`, `then`, `(`, `)` | (no hover) | — | — |


### `call drag_noro_strato(p1, p2 + 1.0, t_seri)`

| Cursor on | Surface | Short body |
|---|---|---|
| `drag_noro_strato` | subroutine call | pairing layout (see above) |
| `p1` | identifier | `p1 : Pa` |
| `p2` | identifier | `p2 : Pa` |
| `+` | computed sub-expr | `p2 + 1.0 : Pa` |
| `1.0` | numeric literal | `1.0 : 1` |
| `t_seri` | identifier | `t_seri : ?` (unannotated) |
