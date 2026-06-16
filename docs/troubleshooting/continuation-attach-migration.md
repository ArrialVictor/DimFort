# Per-variable continuation-attach migration (0.2.7)

The 0.2.7 release changes how DimFort matches `@unit{}` annotations
to declarations that span multiple physical lines (the `&`
continuation form). The new rule is local and deterministic: an
annotation on physical line *N* attaches to the variables whose
declaration tokens *end* on line *N*.

This is a hard switch (no opt-in flag). The shape of the change
makes a heuristic permanent baggage — better to migrate once.

## What changed

### Before 0.2.7

The attach rule was:

- POST on any line in `[decl.line_start, decl.line_end]` attaches
  to all of `decl.names`.
- POST strictly between `line_start` and `line_end` was rejected
  with **U010** (the annotation was dropped entirely, no attach).

Both rules ignored per-name position; the entire multi-line
declaration was treated as one indivisible block.

### After 0.2.7

The attach rule is:

- POST on physical line *N* attaches to the variables whose
  declaration tokens **end** on line *N*.
- The previous "intermediate continuation" U010 case is now a
  successful attach to the names ending on that line.
- PRE unit annotation above a multi-line declaration is refused
  with the new **U024** diagnostic (ambiguous under per-line); the
  author is asked to switch to inline POST per-line.
- A new info-level **U025** fires when an annotation sits on a
  non-last continuation line and later continuation-line names
  remain unannotated — the recurring per-line migration footgun.

### Why the change

The pre-0.2.7 rule had two failure modes that prevented adoption
on real codebases:

- **Per-line annotations were rejected.** Real-world Fortran often
  declares related variables on consecutive continuation lines
  with one annotation per variable: this convention was
  empirically dominant in one of the six surveyed corpora (38.8%
  of unit annotations rejected). Under U010, all of those
  annotations were lost.
- **All-or-nothing.** When the rule did attach (annotation on the
  last line), every name in the declaration received the same
  unit — wrong when the author wanted different units per
  variable.

The per-line rule reads what the author wrote and matches it
literally.

## Migration cookbook

### Pattern 1 — annotation on last line, intended for all names

Before:

```fortran
real :: pressure, &
        temperature, &
        density         !< @unit{Pa}
```

Under pre-0.2.7: all three variables received `Pa`. Under 0.2.7:
only `density` (the name ending on the annotation's line)
receives `Pa`. **U025 fires** pointing at the omission.

After (recommended — per-line):

```fortran
real :: pressure, &     !< @unit{Pa}
        temperature, &  !< @unit{Pa}
        density         !< @unit{Pa}
```

After (alternative — collapse to one line):

```fortran
real :: pressure, temperature, density   !< @unit{Pa}
```

### Pattern 2 — annotation on first line, intended for all names

Before:

```fortran
real :: a1, &           !< @unit{kg}
        a2, &
        a3
```

Under pre-0.2.7: U010 rejected the annotation entirely; all three
names were unannotated. Under 0.2.7: `a1` (the name ending on the
annotation's line) is annotated; `a2` and `a3` remain
unannotated. **U025 fires** pointing at `a2`, `a3`.

After (per-line):

```fortran
real :: a1, &           !< @unit{kg}
        a2, &           !< @unit{kg}
        a3              !< @unit{kg}
```

### Pattern 3 — PRE block above a multi-line declaration

Before:

```fortran
!> @unit{1}
real :: alpha, &
        beta, &
        gamma
```

Under pre-0.2.7: all three received the unit. Under 0.2.7:
**U024 fires** and nothing is attached. The PRE block's intent is
ambiguous under the per-line rule.

After (per-line POST):

```fortran
real :: alpha, &        !< @unit{1}
        beta, &         !< @unit{1}
        gamma           !< @unit{1}
```

After (single-line — also valid):

```fortran
!> @unit{1}
real :: alpha, beta, gamma
```

### Pattern 4 — different units per variable

The new rule enables what was previously impossible: per-line
annotations on a single declaration, each carrying its own unit.

```fortran
real :: speed, &        !< @unit{m/s}
        mass, &         !< @unit{kg}
        force           !< @unit{N}
```

Each annotation attaches to the variable on its line. The shape
was rejected under U010 before 0.2.7.

## Running the migration sweep

The U025 migration-detection diagnostic is permanent — it stays
in the codebase forever, catching the footgun every time it
appears. To find every site in your project that needs
migration:

```
dimfort check --only=U025
```

Each finding lists the variables on later continuation lines that
remained unannotated. Fix each site by adding per-line POST
annotations (or by collapsing the declaration to a single line).

The pattern survives the initial migration too: a future author
who hits the same footgun gets the same hand-holding.

## Diagnostics summary

| Code | What it means under 0.2.7 |
|---|---|
| **U010** | _retired_ — the failure mode (POST on intermediate continuation line) is now a successful per-line attach. |
| **U024** | PRE unit annotation above a multi-line declaration — refused; switch to per-line POST. |
| **U025** | Info: an annotation on a non-last continuation line whose later names remain unannotated. The migration footgun. |
| **U006** | Unchanged: generic orphan annotation that doesn't bind to any declaration. (Narrower in practice — the pre-0.2.7 intermediate-continuation U006 noise no longer reaches the orphan list.) |

## Why hard switch (no opt-in flag)

A flag would convert a momentary break into a persistent fork in
the user's mental model. Every future user would have to learn
the rule plus its exception. The cost of a hard switch is bounded
(your project's annotation files get one sweep); the cost of a
heuristic would be permanent.

See [`shipped/per-variable-continuation-attach.md`](../design/shipped/per-variable-continuation-attach.md)
for the full design rationale.
