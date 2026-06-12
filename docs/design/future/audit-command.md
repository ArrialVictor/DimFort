# `dimfort audit` — adoption-time codebase survey (FUTURE)

**Status:** future feature, post-0.2.2. Captures the design
direction reached at the close of the 0.2.2 design discussion
when an earlier proposal (a diagnostic — provisionally U024) was
rejected as falling between two stools (too narrow if it only
covered canonical syntax, too false-positive-prone if it tried to
cover everything).

## 1. Problem this solves

A team adopting DimFort on an existing Fortran codebase has to
decide which comment conventions in their code are intended as
unit annotations, and configure `dimfort.toml`'s
`unit_comment_delimiters` accordingly. Doing this by hand on a
500k-line model is hard — the team has to grep their own
source for the patterns they use, sample-validate, and decide
what's load-bearing vs. prose.

DimFort can do this for them, as a **one-shot inspection command,
not a per-edit diagnostic**. The output is a structured report
suggesting concrete additions to `dimfort.toml`, not a wave of
in-editor squiggles.

## 2. Why this is NOT a diagnostic

Catalogued during the 0.2.2 discussion. The shapes a "this looks
like a unit annotation" detector would target:

| Convention | Example | False-positive flavour |
| --- | --- | --- |
| Bracketed inline | `! [m/s]` | `! see chapter (3)` — `3` parses as dim'less |
| Parenthesized inline | `! (m/s)` | `! the (m,n)-th element` — `m`, `n` are known units |
| Labeled prefix | `! UNITS: m/s` | Mostly safe (distinctive literal) |
| Doxygen-with-unit | `!> @param x [m/s] wind` | Bracketed inside Doxygen — same as bracketed |
| Negative-exp SI | `! [m s^-1]` | Same as bracketed |
| Period-product | `! kg.m/s^2` | Any prose mentioning multiple unit symbols |
| Unicode | `! kg·m/s²` | Generally safe (rare in prose) |

A per-edit diagnostic targeting these would either accept the
false-positive floor (training users to ignore the code) or
require an explicit project-level "watch for these" list (which
the user can't easily fill in without already knowing what's in
their source — a chicken-and-egg). Both failure modes erode trust
in the diagnostic surface.

An audit command tolerates the false-positive floor because the
output is a one-shot report a human reviews, not 200 squiggles
the workflow has to silence.

## 3. Sketch of behaviour

```
$ dimfort audit src/
Scanned 312 files. Comment shapes found (confidence ≥ 0.7):

  Bracketed [unit]:        1,847 occurrences across 89 files
    Sample:
      src/dynamics/geometry.f90:43       ! [m]
      src/physics/large_scale_clouds.f90:128 ! [kg/kg]
      src/dynamics/timestep_diag.f90:53  ! [day]
    Suggestion:
      Add to dimfort.toml:
        [parser]
        unit_comment_delimiters = [
          { open = "@unit{", close = "}" },
          { open = "[",      close = "]" },
        ]

  Labeled "UNITS:":            0 occurrences
  Period-product (kg.m/s):     8 occurrences across 4 files
    (uncertain — review samples before configuring)
    Sample:
      src/phylmd/foobar.f90:122  ! kg.m / s^2 (gravity)
      ...
    Suggestion:
      Period-product is not a delimiter; consider whether
      `*` substitution is appropriate in your codebase.
      No automatic config change suggested.

Recommended next step: run with --apply to write the suggested
config block to dimfort.toml (will not overwrite existing keys
unless --force).

Aggregate summary: 1,855 candidate annotations found across
89 files. Re-run `dimfort check src/` after configuring to see
which become load-bearing.
```

## 4. Detection mechanism

For each comment in eligible position (same §3 / §5 eligibility
as the scanner uses):

1. Apply the **convention catalog** (§5 below) against the comment
   body — each pattern carries a "shape detector" function.
2. For each matching shape, extract the candidate inner text.
3. Try to parse the candidate as a unit expression using the
   project's unit table.
4. Confidence score per match:
   - `1.0`: parses cleanly to a known dimension (e.g.
     `[m/s]` → `m/s` → length·time⁻¹).
   - `0.7`: parses with one rewrite-detector substitution
     (e.g. `[m2]` → `m^2`).
   - `0.5`: contains a known unit symbol but doesn't parse as
     a complete expression. Low signal.
   - `0`: doesn't parse and contains no known unit symbol.
5. Report only matches at or above a confidence threshold
   (default 0.7).

The whole sweep runs **once per `dimfort audit` invocation**, not
on every check. Cost is one extra scanner pass over the
workspace — measured against the cold-cache 32 s baseline, this
adds about the same as the scanner's existing comment-loop cost
(roughly 100 ms across 2139 files), which is irrelevant in an
inspection-command context.

## 5. The convention catalog

The "blessed" list of conventions the audit checks for. Each
entry is `(shape, detector_fn, suggested_config_block)`. Initial
catalog (populated from the survey methodology in §6 below):

```
- bracketed:              `[…]`        →  delimiter pattern { open="[", close="]" }
- parenthesized:          `(…)`        →  delimiter pattern { open="(", close=")" }
- labeled-prefix:         `UNITS: …`   →  delimiter pattern { open="UNITS:", close="" }
- doxygen-bracketed:      `@param x […]` →  delimiter pattern with prefix-grab
- siunitx-negexp:         `[m s^-1]`   →  parser hint, not a config change
- period-product:         `…kg.m…`     →  no config change, suggest rewrite rule
```

Adding a convention to the catalog is a small PR: shape
definition + detector + a confidence-calibration test against a
known fixture. The catalog earns growth from real-world adopter
feedback, just like the rewrite rules (see
`rewrite-rules-future.md` §"Guiding principle").

## 6. Evidence model

The audit command is the natural consumer of the **survey
methodology** sketched at the close of the rewrite-rules
discussion. The flow:

1. Survey N representative real-world Fortran codebases —
   spanning climate models, numerical weather prediction,
   community physics packages, and legacy F77 reference code.
   Tally what conventions actually appear.
2. The conventions that appear in ≥ 3 corpora become initial
   catalog entries.
3. Conventions that appear in fewer corpora become *candidate*
   entries with lower default confidence thresholds (the audit
   still finds them but flags them as "uncertain — review").
4. Real-world `dimfort audit` reports from adopters feed back
   into the catalog: a new shape spotted ≥ 5 times across
   independent adopters earns promotion to a first-class entry.

This is the same evidence-driven loop as the rewrite rules,
applied at the convention-catalog level rather than the
single-rule level.

## 7. Open questions

- **Per-file vs. per-workspace report.** A 1,000-file codebase
  with 50 occurrences per file produces 50,000 lines of report.
  Need a summarization strategy (group by file, top-N samples
  per convention, aggregate counts in the header).
- **`--apply` mode.** If we let the audit command write to the
  toml directly, the safety story matters — never overwrite
  existing keys without `--force`, always show a diff first.
  Worth a small dialog with the user before the first
  destructive call.
- **What to do about Doxygen mixed with brackets.** `!> @param x [m/s]
  wind speed` is technically two conventions overlapping. Treat
  as one (Doxygen with bracket grab) or two (Doxygen marker +
  bracket pattern)? Probably one, with `unit_comment_delimiters`
  configured to extract from inside a `@param` wrapper.
- **CI integration.** Could `dimfort audit --strict` exit non-zero
  if unconfigured shapes appear, useful as a CI gate that the
  team's chosen conventions stay aligned with the source? Out of
  v1, but worth keeping the exit-code semantics clean for it.

## 8. What's NOT in the audit command

- **Not a diagnostic.** Audit runs only when explicitly invoked.
  Per-edit feedback continues to be U001 / U005 / etc. as today.
  The audit can't replace the diagnostic surface — it complements
  it for one-shot inspections.
- **Not a "fix" tool.** Audit suggests config changes; it doesn't
  rewrite annotations in the source. Source edits stay in the
  human's hands (and the LSP's `Replace with` quick-fix for
  U002).
- **Not a unit-table extender.** If the audit sees `dyne` in a
  bracket but `dyne` isn't in the unit table, the report flags
  it but doesn't auto-add — that's a `[units]` config change
  the user has to make deliberately.

## 9. Implementation cost estimate

- One new CLI subcommand `audit`, hooked off `dimfort.cli:main`.
- Reuses the scanner's `_classify_plain_comment` for eligibility,
  the unit-table parser for confidence scoring.
- Convention catalog: ~10 entries × (detector + test) ≈ 200 lines.
- Report formatter: ~150 lines.
- `--apply` mode + diff display: another 100 lines + tests.

Total: ~1 week of focused work, with the convention catalog
being the bulk and the rest mostly plumbing. The survey
methodology is the prerequisite — running it identifies the
catalog content before implementation starts.

## 10. Path forward

1. Run the survey (out of 0.2.2; can begin any time).
2. Use survey results to draft the initial convention catalog.
3. Build `dimfort audit` with that catalog, ship as 0.3.0 or
   later (it deserves a minor-version bump because it's a new
   user-facing surface, not a delta on the checker).
4. Iterate the catalog over the first few real adopter
   integrations.
