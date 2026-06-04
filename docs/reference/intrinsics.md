# Fortran intrinsics

DimFort knows the unit semantics of a curated subset of Fortran
intrinsics. Categories below describe how each intrinsic is
checked and what unit its result carries. Unlisted intrinsics
are treated as opaque: DimFort makes no claim about the result's
unit and emits no homogeneity diagnostic for the call.

## Categories

| Category | Intrinsics | Unit semantics |
|---|---|---|
| **Dimensionless** | `exp`, `log`, `log10`, `sin`, `cos`, `tan`, `asin`, `acos`, `atan`, `sinh`, `cosh`, `tanh` | Argument must be **dimensionless** (`1`) or, for `log` / `log10` / `exp`, **appropriately wrapped** (`log(x)` accepts `x : LOG(...)` and returns the inner unit; `exp(x)` accepts `x : LOG(...)` and returns the inner unit; nested cases compose). Bare-dimensioned arguments fire `H003`. |
| **Transforming** | `sqrt`, `abs` | Result is `arg^(1/2)` for `sqrt`, `arg^1` for `abs`. |
| **Transparent** | `floor`, `ceiling`, `nint`, `int`, `real`, `dble`, `sign`, `aimag`, `anint` | Result has the same unit as the first argument. |
| **Same-unit args** | `min`, `max`, `mod`, `modulo`, `merge` | Every argument must share one unit; result is that unit. (`merge` only constrains the first two — the mask is `logical`.) `H002` on mismatch. |
| **Product** | `dot_product`, `matmul` | Result is `arg[0] * arg[1]` (unit-algebra product). |
| **Reduction** | `sum`, `minval`, `maxval` | Result has the element unit. |

## LOG / EXP wrappers

`log`, `log10`, and `exp` interact with `LOG(...)` / `EXP(...)`-tagged
units by inversion: `exp(log_pa)` where `log_pa : LOG(Pa)` unwraps
to `Pa`; `log(pa)` where `pa : Pa` wraps to `LOG(Pa)`. The full
algebra (cancellation, dimensionless collapse, log-of-product,
log-of-power) is in
[unit-algebra.md](unit-algebra.md) §5–§7.

This is why the "Dimensionless" category above isn't strictly
"argument must be dimensionless" — `log` and `exp` legitimately
accept dimensioned values when they're already wrapped, and unwrap
them. Bare dimensioned values (a raw `Pa` not inside a `LOG(...)`
tag) fire `H003`.

## Adding intrinsics

The intrinsic catalog lives in `src/dimfort/core/intrinsics.py`.
Each entry pairs a name with a check / propagation rule. Coverage
grows when a real annotation pass needs an intrinsic that isn't
in the catalog yet — if you hit one, open an issue with the
context and proposed semantics.
