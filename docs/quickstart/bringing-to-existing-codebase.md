# Bringing DimFort to an existing codebase

Real Fortran projects rarely greet DimFort with a clean `@unit{...}`
slate — most have years of author convention in inline comments
already (`! [m/s]`, `! [m^2: empirical]`, …). DimFort can be
configured to read those existing conventions so they become
first-class annotations without rewriting every declaration.

The mechanism is a single `[parser.unit_comments]` table in
`dimfort.toml` with six pattern-list keys — three **positive**
directive families and their three **`nonunit`-prefixed** filters:

| `[parser.unit_comments]` key | Directive | Default |
| --- | --- | --- |
| `unit` | `@unit{...}` (unit claim) | `[{open="@unit{", close="}"}]` |
| `nonunit` | Filter for `unit` (per-site marker + citation-style noise) | three shipped patterns (see the design note) |
| `unit_assume` | `@unit_assume{...:...}` (escape hatch) | `[{open="@unit_assume{", close="}", sep=":"}]` |
| `nonunit_assume` | Filter for `unit_assume` | `[]` |
| `unit_affine` | `@unit_affine_conversion{...->...}` (verified frame change) | `[{open="@unit_affine_conversion{", close="}", sep="->"}]` |
| `nonunit_affine` | Filter for `unit_affine` | `[]` |

The three positive lists are deliberately independent:
`@unit_assume{}` suppresses a fire (a wrong assume silently loses
safety) and `@unit_affine_conversion{}` adds a global conversion
rule (rippling through downstream math), so projects opt into
loose delimiters per directive, not all at once.

## Minimal recipe

Most adopters only need to extend `unit`:

```toml
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
```

Each list **replaces** its default; to keep canonical syntax
alongside a custom form, list both (as above). Setting any
positive list to `[]` is treated as a configuration error and
falls back to the default — an empty positive list would silently
disable that directive family, almost certainly a typo. The
`nonunit*` filter lists are the exception: `nonunit = []` is a
valid explicit opt-out of all filter patterns.

## Aggressive recipe

A project that also wants bracket-shaped assumes and verified
affine conversions:

```toml
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume = [
  { open = "@unit_assume{", close = "}", sep = ":" },
  { open = "[",             close = "]", sep = ":" },
]
unit_affine = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
  { open = "[",                        close = "]", sep = "->" },
]
```

With this config, all of the following are recognised:

```fortran
real :: ws                     ! [m/s]
real :: ws                     ! horizontal wind speed [m/s]
real :: tracer_eff             ! eff. surface ratio [m^2: Andreas 1989]
real :: sst_k = sst_c + 273.15 ! sea-surface T conversion [degC -> K]
```

## What to expect on the first run

A burst of new diagnostics — many of them real bugs that have been
hiding behind doc-only annotations. Two codes in particular surface
configuration-time issues:

- **U021 — conflicting unit comment patterns.** Two configured
  patterns matched the same comment with disagreeing captures.
  The first-listed pattern wins (deterministic from config order);
  the message asks you to clarify by removing one form.
- **U023 — directive on wrong statement kind.** `@unit_assume` on
  a declaration, `@unit{}` on an assignment, and similar
  mismatches. The directive is dropped (not silently applied); the
  message names the directive that would attach correctly.

If the volume is overwhelming on a first pass, the `[diagnostics]`
table accepts severity overrides for any code:

```toml
[diagnostics]
U021 = "info"     # demote to non-blocking until the team triages
U023 = "info"
```

## See also

- [reference/dimfort-toml.md](../reference/dimfort-toml.md) — full
  config-file reference.
- [reference/diagnostic-codes.md](../reference/diagnostic-codes.md)
  — every code, severity, and trigger.
- [design/shipped/unit-comment-delimiters.md](../design/shipped/unit-comment-delimiters.md)
  — the spec, including the `@unit{...}` rewrite detector that
  adds "did you mean …?" suggestions to U002.
