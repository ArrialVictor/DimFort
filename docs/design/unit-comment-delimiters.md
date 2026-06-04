# Unit comment delimiters — design spec

Status: **draft** (2026-06-02). Pre-implementation. Targets 0.2.2.

## 1. Problem statement

**In one line:** Make the comment syntax that carries a unit annotation
configurable per project, so codebases with established inline-unit
conventions (e.g. `! description [m/s]`) become typeable without
rewriting every declaration.

Today, DimFort recognises a unit annotation only when it is written as
`@unit{...}` inside a Doxygen-marked comment (`!<`, `!>`, or `!!`). A
legacy Fortran codebase that already documents units in trailing
comments like `real :: ws ! wind speed [m/s]` must mass-edit every
declaration to migrate. That cost is enough to block adoption.

The fix is to let the user declare additional comment patterns that
DimFort also recognises as unit annotations, sharing the same parser,
the same trust model, and the same checker — only the *where to find
the unit string* is widened.

## 2. The unified model

**One scanner. Three pattern lists (one per directive). One
attachment pipeline.**

The three configurable lists, with their default values:

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
```

A `@unit` pattern is an `(open, close)` literal-string pair. A
`@unit_assume` / `@unit_affine_conversion` pattern adds a mandatory
**inner separator** `sep` — `:` for assume (splits `unit : reason`),
`->` for affine (splits `src -> tgt`). The structured separator is what
keeps a `[degC -> K]` assume-shaped entry distinct from a plain `[m/s]`
unit-shaped entry even when they share outer delimiters.

To find a directive on a comment line, DimFort tries each list's
patterns in list order; the first pattern whose `open` appears in the
comment body (followed by its `close`, and for structured directives
its `sep` between them) wins. The substring(s) between the delimiters
are passed to the existing parser unchanged — no semantic change, only
where to find the annotation.

The defaults preserve today's behaviour bit-for-bit. Projects with
author conventions append entries:

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "[", close = "]", sep = ":" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
  { open = "[", close = "]", sep = "->" },
]
```

With the configuration above, every line below is recognised. Free
prose can sit before, after, or both sides of the directive — only
the substring between the delimiters (and across the `sep` for
structured directives) is parsed.

```fortran
real :: ws                     ! [m/s]
real :: ws                     ! horizontal wind speed [m/s]
real :: ws                     ! [m/s] horizontal wind speed
real :: ws                     ! near-surface, horizontal [m/s] (model grid)
real :: tracer_eff             ! eff. surface ratio [m^2: Andreas 1989]
real :: sst_k = sst_c + 273.15 ! sea-surface T conversion [degC -> K]
```

There is **no "relax mode" feature flag.** Configuration is the whole
mechanism.

### 2.1 Why three lists, not one

The three directives carry different semantics:

- `@unit{...}` **claims** a unit. A spurious claim makes the checker
  fire (visible, contained cost).
- `@unit_assume{...}` **suppresses** a fire. A spurious assume is a
  silent loss of safety — the very symptom dimensional checking exists
  to catch becomes invisible.
- `@unit_affine_conversion{...}` **adds a global conversion rule**. A
  spurious entry can ripple through downstream unit math anywhere the
  conversion applies.

Bundling them into one pattern list would make a project that opts
into loose delimiters for `@unit{}` *also* opt in for assume / affine,
even if the project only meant the former. Three lists let projects
opt in directive-by-directive, matching the risk profile.

## 3. Eligible comment positions

Two positions are eligible for pattern matching with a plain `!`
comment:

1. **Trailing on a declaration line.** A `!`-comment that appears
   after a declaration statement, on the same physical line (or on the
   first/last physical line of a `&`-continued statement). Example:

   ```fortran
   real :: ws ! horizontal wind speed [m/s]
   ```

2. **Immediately above a single declaration line.** A `!`-comment that
   stands alone on its own line, with the *very next* line being a
   declaration. "Immediately" is strict: no blank line, no other
   statement, no second comment line between. Example:

   ```fortran
   ! horizontal wind speed [m/s]
   real :: ws
   ```

   If you want a multi-line preceding comment block to apply, you must
   use `!>` / `!!` (explicit PRE marker, see §4) — that is the channel
   for block-form preceding comments and remains unchanged.

Comments in any other position — standalone above a statement that
isn't a declaration, standalone above a blank line, standalone followed
by another comment, sitting inside the body of a routine far from any
decl — are not scanned for unit annotations.

## 4. Doxygen markers as explicit position hints

The existing Doxygen markers `!<`, `!>`, `!!` are kept and continue to
work, layered on top of the unified scanner:

- `!<` — explicit trailing-POST. The annotation attaches to the
  declaration on the same physical line (or the surrounding `&`-continued
  statement). Position is unambiguous.
- `!>` / `!!` — explicit PRE block. The annotation attaches to the
  declaration whose `line_start` equals the block's `end + 1`. Multiple
  `!!` / `!>` lines may chain into one block (existing behavior).

The pattern matching logic is identical regardless of marker; the
marker only narrows attachment. So `!< @unit{m/s}`, `! @unit{m/s}` (new
in 0.2.2 — bare `!` is now eligible), and `! [m/s]` (new in 0.2.2,
under config) all flow through the same code path.

## 5. The three directives, on the same eligibility surface

`@unit{...}`, `@unit_assume{ unit : reason }`, and
`@unit_affine_conversion{ src -> tgt }` are all recognised on bare
`!` comments as well as Doxygen-marked ones, on the same eligible
positions as §3 — with "declaration line" generalised to "the line
the directive attaches to" for `@unit_assume` (statement-level) and
`@unit_affine_conversion` (statement-level). The position rules and
the scanner pipeline are identical across the three directives;
attachment differs only in which AST node the directive binds to.

Description text inside the comment, before or after the directive,
remains permitted (already true today via `finditer`-based scanning).
`!< wind speed [horizontal] @unit{m/s}` continues to work; the same
shape on a bare `!` is now also accepted.

## 6. Multi-variable declarations

A unit annotation on a multi-variable declaration applies to **all**
names on the declaration, regardless of which pattern (canonical
`@unit{...}` or a user-configured `[...]`) matched. Treatment is
unified across pattern types — once a project has opted into a
pattern via `.dimfort.toml`, the project-level configuration is
itself the explicit gesture, no weaker than typing `@unit{...}`.

If the author wants different units per name, they write multiple
matches on one line (`! [m] [s]`), which fires the existing
"more than one … on one line" malformed-annotation diagnostic and
asks the author to split the declaration — the same safety net that
already applies to `@unit{...}`.

(Historic note: an earlier draft of this spec emitted U022 to skip
non-canonical patterns on multi-var decls. That was reverted before
0.2.2 ship because the divergence between canonical and configured
patterns confused users with no DimFort history. The unified rule
is safer and simpler: any single annotation on a multi-var decl is
treated as applying to all of them, and an author who meant only
one writes one explicit annotation per variable.)

## 7. Continuation lines

A `&`-continued declaration is treated as a single logical statement:

- Trailing pattern on the **first** or **last** physical line attaches
  to the whole statement.
- Pattern on an **intermediate** physical line of a continuation is
  rejected — same rule as today's U010, and likely the same diagnostic
  reused.

## 8. Precedence and conflict

### 8.1 Pattern-list order

When more than one configured pattern could match a single comment,
the **first pattern in config-list order** that has any match in the
comment wins. Position within the comment text is *not* a tiebreaker —
order in the user's config is.

Example with the default config `[{@unit{,}}, {[,]}]`:

```fortran
real :: ws ! wind speed [m/s] @unit{kg}
```

The `@unit{kg}` pattern is listed first, so `kg` wins. The `[m/s]`
match is suppressed even though it appears earlier in the comment
text. This reflects the rule "explicit syntax wins over loose syntax"
and is deterministic from the config.

### 8.2 Pattern conflict

If two configured patterns both match a comment line and the captured
text **differs** (after stripping whitespace), DimFort emits a new
warning (§9, **U021**) and applies the first-listed pattern's match.
A conflict suggests either a duplicated annotation or a pattern set
that is too aggressive; the diagnostic tells the user to clarify.

If both patterns match and the captured text is **identical**, no
diagnostic fires. (Redundant but not contradictory; common when a
project keeps `@unit{}` alongside the legacy comment.)

### 8.3 Interaction across the three directive lists

The three directives target **disjoint statement kinds**:

- `@unit{}` attaches to declarations (`real :: x`).
- `@unit_assume{}` attaches to unit-bearing assignments (`x =
  sqrt(...)`).
- `@unit_affine_conversion{}` attaches to conversion-declaring
  assignments (`t_k = t_c + 273.15`).

A line that hosts one kind of statement cannot also host another, so
in practice no comment line ever has more than one directive list's
match in play. Cross-list interaction does not arise.

Within a single directive's list, §8.1 / §8.2 apply (config-list
order; U021 on conflict).

If a directive matches a comment but the attached line is the wrong
statement kind (e.g. a stray `@unit_assume` on a declaration line),
**U023** fires (§9) and the directive is dropped. The existing
orphan-annotation diagnostic still fires for the case where no
candidate statement exists at all.

The U023 check currently covers two cases: declaration vs.
assignment for all three directive families. The narrower case of
`@unit_affine_conversion{}` on a non-conversion assignment is left
to the existing checker-level S003 path (which fires when the
arithmetic doesn't match the asserted conversion); the U023 surface
only distinguishes statement *kind*, not statement *shape*.

## 9. Diagnostics

### New codes

- **U021 — conflicting unit comment patterns**: WARNING.
  Multiple configured patterns matched the same comment and captured
  different unit text. The first-listed pattern's capture is applied;
  the user should clarify the annotation.
- **U023 — directive on wrong statement kind**: WARNING.
  A directive was found on a comment attached to a statement of a
  kind that directive does not target — for example `@unit_assume`
  on a `real :: x` declaration, `@unit{}` on a regular assignment,
  or `@unit_affine_conversion{}` on a non-conversion assignment. The
  directive is **dropped** (not attached, not silently applied). The
  diagnostic message names the directive found, the statement kind
  it landed on, and the directive that would attach correctly at
  that position. Fires only when a candidate statement of the wrong
  kind exists at the directive's expected position; if no candidate
  exists at all, the existing orphan-annotation diagnostic fires
  instead.

### Reused codes

- **U002 — could not parse unit text**: WARNING (unchanged severity).
  In 0.2.2 the diagnostic payload is extended with an optional
  `suggested_rewrite: str` field set by the rewrite detector (§12).
  When present, the diagnostic text includes "did you mean
  `<suggestion>`?" and the LSP emits a code action that applies the
  rewrite to the source line on user accept.
  Captured text from a configured pattern did not parse as a valid
  unit. The variable is treated as unannotated. Same code that fires
  today for malformed `@unit{}` contents.
- **U010 — `@unit` on intermediate continuation line**: extended to
  cover patterns generally, not only `!<`-marked annotations.

### Configurability

All three codes obey the existing `[diagnostics]` severity overrides
in `.dimfort.toml`. Projects that find U022 noisy on first run can
demote it to `"info"` or `"off"`.

## 10. Backward compatibility

The default value of `parser.unit_comment_delimiters` is
`[{open="@unit{", close="}"}]`. A project that does not set the key
gets exactly today's behavior, **with one expansion**: a bare `!`
comment containing `@unit{...}` (no Doxygen marker) is now also
scanned, and matches against any declaration trailing it or directly
beneath it. The previous code path required `!<` / `!>` / `!!`.

This is a deliberate, accepted expansion. We expect it to be benign
(nobody writes `! @unit{m/s}` and means it as a non-annotation
comment), and the gain — uniformity — is worth it. The pre-merge
backward-compat check (§16) will catch any project where this turns
out to matter.

If a project explicitly clears the list (`unit_comment_delimiters =
[]`), DimFort logs a configuration error and falls back to the
default for that key. An empty list would disable all unit
recognition and is almost certainly an unintended typo; we surface
the problem loudly without crashing the LSP startup or CLI run
(`load_config` is contractually non-raising so a broken config never
takes the whole tool down — see also §14).

## 11. Performance

The scanner today examines only Doxygen-marked comments. After this
change, every plain `!` comment also passes through the pattern loop.
Each pattern application is two `str.find()` calls — C-level fast in
CPython.

Order-of-magnitude estimate on a large legacy codebase (1000s of
files, ~100 comments per file, 2-3 patterns): a few hundred
milliseconds added to a cold-cache full-workspace check. Warm
content-hash cache hits are unaffected (the cache key already
includes the scanner output by hashing the file).

Pre-merge benchmark target: full-workspace check on the validation
workspace within **+10 %** of the current 32 s baseline. Hit on first
run; tune if not.

## 12. Suggested rewrites for unparsable captures

When a pattern matches but the captured text fails the unit parser,
DimFort runs a **rewrite detector** on the captured string before
emitting U002. If the detector produces a candidate that itself
parses cleanly against the project's `UnitTable`, the U002 diagnostic
carries the candidate as a `suggested_rewrite` payload:

- **CLI:** `U002: could not parse unit "m2/s" in comment; did you
  mean "m^2/s"?`
- **LSP:** same message, plus a code action "Replace `m2/s` with
  `m^2/s`" that edits the source line on user accept.

### 12.1 The detector

A list of `RewriteRule` objects. Each rule's contract:

```python
class RewriteRule(Protocol):
    def transform(self, captured: str) -> str: ...
```

Rules are applied **in sequence (pipeline)**, in list order. Each
rule's output becomes the next rule's input. After all rules have
run, the final string is parsed against `table`. If it parses
cleanly, the diagnostic carries it as `suggested_rewrite`. If not,
U002 fires without a suggestion (same behaviour as today).

A rule that doesn't apply to its input returns the input unchanged.
The accumulated effect of multiple disjoint rules — e.g. sep swap +
digit-suffix on `kg.m2/s` → `kg*m^2/s` — is what makes the pipeline
catch composite cases that no single rule could fix alone.

Only the **final result** is shown in the diagnostic; provenance
(which rules contributed) is not surfaced. Users see one suggestion,
not a sequence of transformations.

### 12.2 Rules shipped in 0.2.2

Exactly one rule:

- **Digit-suffix → caret-exponent.** Pattern `([a-zA-Z]+)(\d+)`
  rewrites to `\1^\2` for every match in the captured string.
  Examples: `m2 → m^2`, `kg/m3 → kg/m^3`, `m2/s2 → m^2/s^2`. Applied
  greedily across the whole captured string. Idempotent (no match on
  already-well-formed input like `m^2`); acts only on `[a-zA-Z]+\d+`
  substrings, so it composes safely with future rules in disjoint
  character classes.

Two further rule classes — separator swaps (`kg.m → kg*m`,
`÷ → /`, …) and typo correction against the unit dictionary
(`metre → m`, edit-distance) — are explicitly out of 0.2.2. They
ship as separate `RewriteRule` instances in a later release if
real-world usage shows demand. Each new rule is evidence-driven, not
speculative.

### 12.3 Principles

- **Warning stays.** A suggested rewrite never silences U002. The
  diagnostic remains until the user either accepts the rewrite,
  edits the source by hand, or removes the pattern.
- **No silent rewrite.** The detector only ever suggests. Source
  changes require explicit user action (CLI: manual edit; LSP: code
  action accept). DimFort never edits source on its own.
- **Self-correcting on wrong suggestions.** If a wrong suggestion is
  accepted, the new captured text either fails to parse (U002
  re-fires on the new contents) or parses to a unit that conflicts
  downstream (H001/H002 fires at the use site). One extra diagnostic
  cycle, not silent failure.
- **Phrased as a question.** "Did you mean `<X>`?" not "the unit
  is `<X>`." Linguistic discipline keeps the user in the loop.
- **Pattern-agnostic.** The detector runs identically on explicit
  `@unit{m2/s}` captures and delimiter-extracted `[m2/s]` captures.
  No special path per pattern type.

### 12.4 Performance

The detector runs only on the parser-failure path, which is rare in
practice. The shipped digit-suffix rule is one regex substitution
plus a re-parse attempt. Negligible.

### 12.5 Rule design requirements

Any new `RewriteRule` added in a future release must satisfy:

1. **Idempotency.** `rule.transform(rule.transform(s)) ==
   rule.transform(s)` for all inputs. Required so already-clean
   substrings don't get re-transformed when the pipeline runs.
2. **Commutativity preferred.** Where a new rule operates on
   character classes disjoint from existing rules (digits vs
   punctuation vs alphabetics), ordering doesn't observably matter.
   This is the easiest path and the default to aim for.
3. **Explicit ordering when not commutative.** If a new rule
   genuinely interacts with an existing rule (e.g. a typo rule that
   would split tokens the digit-suffix rule needs to see intact), the
   position in the list documents the intent. Add a comment in the
   spec at the rule's §12.2 entry explaining why it sits where it
   does.

These are rule-design constraints, not user-facing config concerns —
the list order in §12.2 is fixed by the implementation, not
configurable.

## 13. Out of scope for 0.2.2

- **Rewrite detector rule classes 2 and 3** (separator swaps, typo
  correction against the unit dictionary). Evidence-driven follow-up,
  not parked indefinitely — the §12 architecture is built for these
  to slot in trivially.
- **Regex patterns** (full `re` with named groups). Originally
  flagged as the obvious future extension, but on inspection
  delimiters plus the `sep` field cover the full landscape of
  legitimate comment-embedded unit annotations: alternation is just
  multiple list entries, lookaround is covered by custom open
  strings (`unit:[`), character classes are unneeded because
  delimiters accept any literal alphabet, and named groups beyond
  the two-part `sep` case have no use site. Regex is kept as an
  **escape hatch if a concrete use case ever appears**, not as a
  scheduled next step — likely indefinitely deferred. Implementation
  note (still worth following for general hygiene): design the
  scanner around two typed pattern objects —
  `UnitPattern.find(body) -> (capture, span) | None` for `@unit{}`,
  and `StructuredPattern.find(body) -> (cap_left, cap_right, span) |
  None` for `@unit_assume` / `@unit_affine_conversion` (returning
  both sides of the inner separator). Concrete subclasses for 0.2.2:
  `DelimiterPattern(open, close)` and
  `StructuredDelimiterPattern(open, sep, close)`. A hypothetical
  regex variant would slot in as an additional subclass plus a TOML
  discriminator; the scanner loop, precedence rule, conflict
  diagnostic, and attachment pipeline stay unchanged. **Do not let
  raw tuples leak past the config loader** — that discipline is the
  cheap insurance.
- **Tool-enforced `@unit_assume` registry.** A future check that
  cross-references every `@unit_assume` site against a project-level
  registry file (opt-in via a `parser.unit_assume_registry = "..."`
  config key); fires diagnostics for missing or stale entries.
  Requires a small spec for the registry format (Markdown table or
  TOML sidecar). Out of 0.2.2; relevant once any project ships
  user-configured assume delimiters.
- **Inference from variable names or neighbouring literals.** Out of
  scope by principle, not just by version.
- **Per-file opt-out / opt-in of patterns.** Workspace-wide for now.
- **A `dimfort patterns show <file>` inspection command.** Useful but
  not on the critical path; revisit alongside a future LaTeX-export
  inspection tool.

## 14. Configuration schema

Three full key paths, all under `[parser]`:

- `parser.unit_comment_delimiters` — `array of tables`. Each entry:
  `{ open = "<str>", close = "<str>" }`. Both fields required,
  non-empty.
- `parser.unit_assume_comment_delimiters` — `array of tables`. Each
  entry: `{ open = "<str>", close = "<str>", sep = "<str>" }`. All
  three fields required, non-empty.
- `parser.unit_affine_comment_delimiters` — same shape as the assume
  list (with `sep` typically `"->"`).

Validation at config-load time. All violations are logged at
`ERROR` level against the offending entry / list and the loader
falls back to the default value for the affected key (matching
`load_config`'s never-raises contract — see §10):

- Each entry has all required fields, all non-empty strings.
- Each list is non-empty (cleared list is an error, per §10 — applies
  to all three lists).
- Duplicate entries within a list are an error.
- Unknown keys inside an entry are an error (entry is dropped).
- `sep` must not appear inside `open` or `close` (would make matching
  ambiguous).

Defaults (when keys are unset):

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
```

## 15. Migration and adoption guidance

The 0.2.2 release notes and README should say:

1. **Existing users** (`.dimfort.toml` does not set
   `unit_comment_delimiters`): nothing to do. Behavior is preserved
   except that bare `! @unit{...}` is now accepted in addition to
   `!< @unit{...}` (etc.). Hover, panel, diagnostics — all unchanged.
2. **Users with author-convention comments** (e.g. `! description
   [m/s]`): add an entry under `[parser]`. Note that each of the
   three directive lists is independent — opt in only to what your
   author conventions actually use.

   ```toml
   [parser]
   # Unit claims: opt in to the [m/s] convention.
   unit_comment_delimiters = [
     { open = "@unit{", close = "}" },
     { open = "[",      close = "]" },
   ]
   # Assume-style escapes: opt in only if your code has them in
   # comment form (e.g. ! Andreas 1989 polynomial [m^2: empirical]).
   # If not, leave the default — assume must keep its explicit form.
   #
   # unit_assume_comment_delimiters = [
   #   { open = "@unit_assume{", close = "}", sep = ":" },
   #   { open = "[",            close = "]",  sep = ":" },
   # ]
   ```

   On first check, expect a burst of new diagnostics — many of them
   real bugs that have been hiding behind doc-only annotations.
   Triage with `--diagnostic-severities` or the `[diagnostics]` table
   in the config if the volume is overwhelming.
3. **Authors of new code**: prefer `@unit{...}`. It is unambiguous,
   tool-agnostic, and the captured text is checked by the same parser
   as the loose patterns. The relax-pattern path exists for legacy
   compatibility, not as the recommended writing style.

The DimFort website / docs should add a small "Bringing DimFort to an
existing codebase" page covering the steps above.

## 16. Pre-merge backward-compat check

Before merging this feature, run a snapshot regression:

1. Capture the current `var_units_by_scope` map for every annotated
   file in the validation workspace.
2. Re-run after the implementation.
3. Diff. Any change is either a documented expansion (bare `!
   @unit{}` newly accepted) or a regression to investigate.

Same procedure for the diagnostic emission set (codes + counts).

## 17. Open questions

- **Trailing-pattern attachment when a non-decl statement also has a
  trailing comment that happens to match.** Example:
  `x = x + 1 ! [m/s]`. Per §5, eligibility includes assignments now;
  the kind-correctness check in §8.3 catches mismatches as U023.
- **Patterns that overlap by prefix** (e.g. `[` and `[unit:`). With
  literal-string `str.find`, both will match independently and §8.2
  conflict logic applies. Confirm with tests.
- **Should multi-pattern users keep `@unit{}` first by convention?**
  We could lint a config where `@unit{}` is *not* first and warn,
  since first-match-wins makes pattern order important. Probably
  unnecessary — document the rule, trust the user.
