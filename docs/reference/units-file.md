# Project units file

DimFort ships with a default catalog of SI units, prefixes, and
common derived units. A project that needs additional units — domain
conventions like `hPa`, `bar`, `g/kg`, `day`, `percent`, or a custom
named ratio — extends the catalog with a project-local TOML file
referenced from `.dimfort.toml`:

```toml
[units]
file = "etc/project-units.toml"
```

The path is resolved relative to `.dimfort.toml`. User entries are
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
fractions like `0.01` exactly.

```toml
[prefixes]
G = 1000000000
M = 1000000
k = 1000
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
hPa  = { expr = "Pa", factor = 100 }            # 1 hPa = 100 Pa
bar  = { expr = "Pa", factor = 100000 }
degC = { expr = "K", offset = "273.15" }        # 0 °C = 273.15 K

# Domain-specific examples a project might add:
percent = { expr = "1", factor = "1/100" }      # dimensionless ratio
day     = { expr = "s", factor = 86400 }
g_per_kg = { expr = "g/kg" }                    # tracer mixing ratios
```

Per-entry fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `expr`         | string  | required | Unit expression in terms of already-defined units. Same grammar as `@unit{...}` bodies. |
| `factor`       | int or `"p/q"` | `1` | Multiplicative scale relative to `expr`. Used by `S001` scale checking. |
| `offset`       | string  | `"0"` | Zero-point shift relative to `expr`. Triggers affine handling (`S002`, `@unit_affine_conversion`). String to keep the rational exact. |
| `prefixable`   | bool    | `false` | If `true`, the unit accepts standard SI prefixes (`khPa`, `mbar`, …). Most derived units do **not** want this. |

Entries are resolved in **dependency order** — an entry can refer
to any unit defined earlier in the merged catalog (shipped defaults
+ all user entries above it).

### `[doxygen]`

Optional, project-rare. Lets you redirect DimFort's annotation
parser to a different Doxygen-command name in case `@unit{...}`
collides with an existing custom command. Most projects leave this
alone.

## Merge semantics

When DimFort loads `.dimfort.toml` and finds `[units] file = ...`:

1. The shipped `default_units.toml` loads first.
2. Your project file loads on top.
3. **Collisions are an error**: a user `[derived]` entry whose name
   matches a shipped one is rejected at load time with a clear
   message. To override a shipped definition, raise an issue —
   silent override would make tracking unit drift across projects
   impossible.
4. References in `expr` strings resolve against the **merged**
   catalog, so user-defined units can compose with shipped ones
   freely.

## Sharing across projects

The units file is plain TOML with no DimFort version coupling. A
team that maintains several Fortran projects in the same physical
domain (climate modelling, plasma physics, …) can keep one units
file under version control and reference it from every project's
`.dimfort.toml` via a relative path.

## Validation

To check that your units file parses cleanly without running a
full check, run `dimfort check` on any source in a directory where
the `.dimfort.toml` references it — `dimfort` logs a config-load
warning at `WARNING` level for any malformed entry. A clean run
means the file loaded without complaint.
