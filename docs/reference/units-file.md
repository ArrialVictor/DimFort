# Project units file

DimFort ships with a default catalog of SI units, prefixes, and
common derived units. A project that needs additional units — domain
conventions like `hPa`, `bar`, `g/kg`, `day`, `percent`, or a custom
named ratio — extends the catalog with a project-local TOML file
referenced from `dimfort.toml`:

```toml
[units]
file = "etc/project-units.toml"
```

The path is resolved relative to `dimfort.toml`. User entries are
**merged on top** of the shipped defaults — a collision-detection
pass runs at load time and rejects duplicate names.

The default catalog lives in
[`src/dimfort/core/default_units.toml`](https://github.com/ArrialVictor/DimFort/blob/main/src/dimfort/core/default_units.toml).
Copy any of its entries verbatim as templates.

## Schema

A units file has up to four sections. Every section is optional —
extend only what you need.

### `[base]`

Maps a base-unit name to one of the seven SI dimension slots:

| Slot | Dimension |
|---|---|
| `M`     | mass |
| `L`     | length |
| `T`     | time |
| `Theta` | thermodynamic temperature |
| `I`     | electric current |
| `N`     | amount of substance |
| `J`     | luminous intensity |

Defaults: `kg → M`, `m → L`, `s → T`, `K → Theta`, `A → I`,
`mol → N`, `cd → J`. You'll rarely override this — the seven SI
bases are universal.

```toml
[base]
# (almost always empty in user files)
```

### `[prefixes]`

Maps a prefix character to a multiplicative factor. Values are
**exact**: either an integer or a rational string `"p/q"`. Floats
are rejected because binary floats can't represent decimal
fractions like `0.01` exactly. TOML allows underscores inside
integer literals for readability (`1_000_000`) and DimFort treats
the underscored form identically to the unsuffixed one.

```toml
[prefixes]
G = 1_000_000_000
M = 1_000_000
k = 1_000
d = "1/10"
c = "1/100"
m = "1/1000"
```

### `[derived]`

The bulk of a typical extension file. Each entry names a derived
unit and gives its expression in terms of already-defined units,
optionally with a `factor` (for scaled units like `hPa`) or an
`offset` (for affine units like `degC`).

```toml
[derived]
N    = { expr = "kg*m/s^2" }
Pa   = { expr = "N/m^2" }
hPa  = { expr = "Pa", factor = 100 }              # 1 hPa = 100 Pa
bar  = { expr = "Pa", factor = 100_000 }
degC = { expr = "K", offset = "273.15" }          # 0 °C = 273.15 K

# Domain-specific examples a project might add:
percent  = { expr = "1", factor = "1/100" }       # dimensionless ratio
day      = { expr = "s", factor = 86_400 }
g_per_kg = { expr = "g/kg" }                      # tracer mixing ratios
```

Per-entry fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `expr`         | string  | required *(compact form)* | Unit expression in terms of already-defined units. Same grammar as `@unit{...}` bodies. Project-local convenience. |
| `dim`          | string  | required *(catalog form)* | SI slot product form (e.g. `"M*L^-1*T^-2"`). Resolves immediately with no dependency on other units. Used by the shipped catalog. |
| `factor`       | int or `"p/q"` | `1` | Multiplicative scale relative to `expr` / `dim`. Used by `S001` scale checking. |
| `offset`       | string  | `"0"` | Zero-point shift relative to `expr` / `dim`. Triggers affine handling (`S002`, `@unit_affine_conversion`). String to keep the rational exact. |
| `quantitykind` | string  | unset | Semantic-vocabulary tag (e.g. `"Pressure"`, `"ThermodynamicTemperature"`). Ignored at load; surfaces in future vocabulary tooling. |
| `aliases`      | `list[string]` | `[]` | Alternate names registered as pointers to the same `Unit`. Useful for corpus spellings (`degC` / `celsius`, `Pa` / `pascal`). |
| `prefixable`   | bool    | `false` | If `true`, the unit accepts standard SI prefixes (`khPa`, `mbar`, …). Most derived units do **not** want this. |

An entry uses **either** `expr` (compact form) **or** `dim`
(catalog form) — mixing the two on one entry is an error.

Entries are resolved in **dependency order** — an entry can refer
to any unit defined earlier in the merged catalog (the shipped
defaults plus every user entry listed above it).

### `[doxygen]`

Optional, project-rare. Lets you redirect DimFort's annotation
parser to a different Doxygen-command name in case `@unit{...}`
collides with an existing custom command. Most projects leave this
alone.

## Merge semantics

When DimFort loads `dimfort.toml` and finds `[units] file = ...`:

1. The shipped `default_units.toml` loads first.
2. Your project file loads on top.
3. **Layered-override gate.** The three sections are protected at
   different levels:
   - `[base]` — the seven SI base units are fixed by the standard
     and cannot be extended or redefined. Any user entry here is
     **rejected** at load with a clear message.
   - `[prefixes]` — new entries are allowed; **redefining a shipped
     prefix is rejected**. Prevents silent drift on the SI prefix
     ladder.
   - `[derived]` — a user entry whose name collides with a shipped
     one **emits a warning and takes precedence** (compact `expr`
     form's convenience over strict lockdown). To silence, either
     rename the user entry or register the shipped name as an
     `aliases` entry on the user's definition.
4. References in `expr` strings resolve against the **merged**
   catalog, so user-defined units can compose with shipped ones
   freely.

## Sharing across projects

The units file is plain TOML with no DimFort version coupling. A
team that maintains several Fortran projects in the same physical
domain (climate modelling, plasma physics, …) can keep one units
file under version control and reference it from every project's
`dimfort.toml` via a relative path.

## Validation

To check that your units file parses cleanly without running a
full check, run `dimfort check` on any source in a directory where
the `dimfort.toml` references it — `dimfort` logs a config-load
warning at `WARNING` level for any malformed entry. A clean run
means the file loaded without complaint.
