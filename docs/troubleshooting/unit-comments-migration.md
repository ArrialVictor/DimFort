# `[parser.unit_comments]` migration (0.2.7)

The 0.2.7 release moves the three flat unit-comment delimiter keys
under a nested `[parser.unit_comments]` table and adds matching
`nonunit*` drop filters. The flat keys still parse — they warn and are
ignored — so the upgrade path is a one-line rewrite per project.

## What changed

### Flat → nested rename

| Pre-0.2.7 key (under `[parser]`)     | 0.2.7 key (under `[parser.unit_comments]`) |
|---|---|
| `unit_comment_delimiters`            | `unit`         |
| `unit_assume_comment_delimiters`     | `unit_assume`  |
| `unit_affine_comment_delimiters`     | `unit_affine`  |

The entry shape for each is unchanged. `unit` entries still take
`{open, close}`; `unit_assume` and `unit_affine` entries still take
`{open, close, sep}`.

### New `nonunit*` drop filters

Three new keys ship under the same nested table:

| Key               | Entry shape                       | Default |
|---|---|---|
| `nonunit`         | `{open, close, regex?}`           | three shipped patterns (see below) |
| `nonunit_assume`  | `{open, close, sep?, regex?}`     | empty   |
| `nonunit_affine`  | `{open, close, sep?, regex?}`     | empty   |

Each `nonunit*` list defines drop zones: a candidate from the matching
STRUCT family (`unit` / `unit_assume` / `unit_affine`) whose span
overlaps a drop zone is silently filtered out before reaching the
lexer. Filters are **per-family** — `nonunit` only filters `unit`
candidates, `nonunit_assume` only filters `unit_assume` candidates,
etc.

The conceptual model: `STRUCT \ nonSTRUCT` — what DimFort actually
sees is the unit candidates minus the drop zones.

### Default `nonunit` patterns

Three patterns ship enabled by default — they target shapes that
empirically appear in surveyed corpora and almost never represent real
unit annotations:

| Pattern                                           | Targets                            |
|---|---|
| `{open="@nonunit{", close="}"}`                   | Per-site author marker             |
| `{open="(see ", close=")"}`                       | Citation prefix `(see Smith 2002)` |
| `{open="(", close=")", regex="^\\d{4}$"}`         | Year-only parens `(2002)`          |

Override by setting `nonunit = []` (opt out of all defaults) or
declaring your own list. The shipped defaults only fire if a project
configures parens / brackets as `unit` delimiters, so projects on the
canonical `@unit{...}` form keep current behaviour bit-for-bit.

## Migration cookbook

### Step 1 — rename the flat keys

Before:

```toml
[parser]
unit_comment_delimiters = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume_comment_delimiters = [
  { open = "@unit_assume{", close = "}", sep = ":" },
]
unit_affine_comment_delimiters = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
```

After:

```toml
[parser.unit_comments]
unit = [
  { open = "@unit{", close = "}" },
  { open = "[",      close = "]" },
]
unit_assume = [
  { open = "@unit_assume{", close = "}", sep = ":" },
]
unit_affine = [
  { open = "@unit_affine_conversion{", close = "}", sep = "->" },
]
```

The flat keys are now warn-and-ignored. You'll see a one-line warning
pointing at this page until they're removed.

### Step 2 (optional) — tune the new `nonunit` filters

If your project does NOT use parens or brackets as a `unit` delimiter,
the defaults have no observable effect — leave them in place.

If your project DOES use parens (e.g. `{open="(", close=")"}` in
`unit`), the shipped `(see ...)` and year regex will drop those
shapes silently. If you instead want them surfaced as U002 (unit parse
failure), opt out:

```toml
[parser.unit_comments]
nonunit = []  # explicit opt-out of the three shipped defaults
```

### Step 3 (optional) — declare your own drop patterns

Suppress an inline note that the lexer would otherwise read as a unit:

```toml
[parser.unit_comments]
nonunit = [
  { open = "@nonunit{", close = "}" },              # canonical per-site marker
  { open = "(", close = ")", regex = "^[A-Z]{2}$" }, # ignore parens with country codes
]
```

The `regex` field is matched against the inner content
(whitespace-stripped for `nonunit`, full-content for the structured
filters).

## Why the change

- One namespace for unit-comment configuration instead of three top-
  level keys.
- A single home for the new `nonunit*` filters that mirrors the STRUCT
  entries (set subtraction).
- A hard break is cheap: beta release line, ~zero external users with
  persisted `dimfort.toml` files.
