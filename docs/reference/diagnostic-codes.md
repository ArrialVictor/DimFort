# Diagnostic codes

Every diagnostic DimFort emits carries a stable code that identifies
the rule that fired. Codes are grouped by prefix; the prefix tells you
what kind of problem it is and which pass surfaced it.

| Prefix | Pass | Family |
|---|---|---|
| `H`    | semantic checker      | homogeneity — the math does not balance dimensionally |
| `U`    | annotation pipeline   | annotation / metadata problems |
| `S`    | scale checker (opt-in) | same dimension, different magnitude or zero-point |
| `X`    | cross-site pass       | conflicting unit claims from `dimfort interactions` |
| `P`    | parser                | regions the parser could not read |

Severity meanings: **error** fails the run (exit code 1); **warning**
prints but does not fail; **info** prints and never affects the exit
code.

The unit-algebra rule taxonomy that classifies *why* a homogeneity
diagnostic fired (`D1.1`–`D1.7`) is documented separately in
[unit-algebra.md](unit-algebra.md). When the message reads
`H002 (D1.3)` the H code identifies the surfacing diagnostic; the
D-class identifies the algebra rule violated.

## H-series — homogeneity

| Code  | Severity | When it fires |
|-------|----------|---------------|
| H001  | error    | Assignment LHS unit does not match the RHS unit. Wrapper-arithmetic variants surface as `H001 (D1.2 / D1.3 / D1.5 / D1.6)`. |
| H002  | error    | Additive operands (`+`, `-`), or arguments to a same-unit intrinsic (`min`, `max`, `mod`, `modulo`, `merge`), disagree on unit. |
| H003  | error    | A dimensionless-only intrinsic (`exp`, `log`, `log10`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `sinh`, `cosh`, `tanh`) received a non-dimensionless argument. |
| H004  | error    | User-defined function or subroutine call: an actual argument's unit does not match the formal's annotated unit. Resolved across files. |
| H010  | warning  | Implicit cast or wrapper untag: the expression is accepted but flagged. Covers D1.5 (literal cast against a typed slot) and D1.6 (wrapper-tag untagging). |

## U-series — annotation pipeline

| Code         | Severity | When it fires |
|--------------|----------|---------------|
| U001         | error    | Malformed `@unit{}` or `@unit_assume{}` directive: unclosed brace, empty body, more than one directive on a comment line, missing `:` reason in an `@unit_assume`. |
| U002         | error    | `@unit{}` body could not be parsed as a unit expression. The payload may include a `suggested_rewrite` (e.g. `m2` → `m^2`); editors surface this as a Quick Fix code action. |
| U005         | warning  | A variable is used in a unit-relevant position but has no `@unit{}` annotation. |
| U006         | warning  | Orphan annotation: an `@unit{}` directive does not attach to a known declaration. |
| U007         | error    | The parser could not load the file (read error, encoding problem, or other I/O-level failure). |
| U010         | error    | `!<` Doxygen-trailing marker on an intermediate line of an `&`-continued declaration. The annotation is rejected. |
| U020         | info     | An `@unit_assume{}` was applied at this site — the RHS unit was *asserted*, not derived. Audit note only. |
| U021         | warning  | Two configured comment-delimiter patterns both matched the same comment with different captures. The first listed pattern wins; U021 surfaces the disagreement so the author can resolve the conflict. |
| U023         | warning  | A directive landed on the wrong statement kind (e.g. `@unit_assume` on a declaration, or `@unit{}` on an assignment). The directive is dropped, not silently applied; the message suggests the directive that would attach. |
| U-conflict   | error    | Two annotations on the same variable carry different units (e.g. a leading `!>` block disagrees with a trailing `!<`). |

## S-series — scale (opt-in)

The scale family fires only when `scale_mode` is on: pass
`--scale` on the CLI, or set `[scale] enabled = true` in
`.dimfort.toml`, or `scaleMode: true` in LSP
`initializationOptions`.

| Code  | Severity | When it fires |
|-------|----------|---------------|
| S001  | warning  | Same dimension, different magnitude factor (e.g. `hPa` vs `Pa`, `g/kg` vs `kg/kg`). |
| S002  | warning  | Same dimension and factor, different zero-point (e.g. `degC` vs `K`). |
| S003  | error    | An `@unit_affine_conversion{src -> tgt}` directive whose arithmetic does not perform the stated conversion. |

## X-series — cross-site

Produced only by `dimfort interactions` (CLI) and the
`dimfort/interactions` LSP request — not by the per-statement
`check` pass.

| Code  | Severity | When it fires |
|-------|----------|---------------|
| X001  | error    | Two sites of the same variable make conflicting unit claims that no single statement reveals. See [design/interaction-points.md](../design/shipped/interaction-points.md). |

## P-series — parser

| Code  | Severity | When it fires |
|-------|----------|---------------|
| P001  | info     | A region the parser could not read. DimFort makes no unit claims about the region and skips it. Common on F77 idioms or `.F90` files with active preprocessor conditionals. Silence per-file by setting `[diagnostics] P001 = "off"` in `.dimfort.toml`. See [design/unparsed-regions.md](../design/shipped/unparsed-regions.md). |

## Tuning severity per project

Any code can be remapped to `"error" | "warning" | "info" | "off"` in
the `[diagnostics]` table:

```toml
[diagnostics]
U021 = "info"   # demote pattern-conflict warnings to non-blocking
P001 = "off"    # silence parser-skipped regions in a known-F77 corpus
```

The remap applies uniformly to CLI and LSP output. `info` and `off`
never affect the exit code.
