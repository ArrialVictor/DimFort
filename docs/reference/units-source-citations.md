# DimFort default unit table — source citations

Per-entry provenance for `src/dimfort/core/default_units.toml` and the
discipline templates under `docs/adoption/`.

**Authoritative sources** (values transcribed by hand):

- BIPM SI Brochure 9th ed. (2019, rev 2026) — base, derived, accepted non-SI
- CODATA 2022 Recommended Values — eV, Da, atomic constants
- IAU 2012 / BIPM 2018 — astronomical unit
- UNESCO PSS-78 (1981) — practical salinity unit
- IOC/UNESCO TEOS-10 (2010) — absolute salinity
- CF Conventions 1.10 — climate metadata spellings

**Cross-validation references** (consulted only, no data copied):

- QUDT 3.3.0 vocabulary (CC BY 4.0) — <https://qudt.org/>
- Pint `default_en.txt` (BSD-3-Clause) — <https://github.com/hgrecco/pint/>
- UCUM essence (Regenstrief Institute) — <https://ucum.org/>
- GNU Units `units.dat` (GPL-3.0) — <https://www.gnu.org/software/units/>

---

## Base units

**Source:** BIPM SI Brochure 9th ed., §2.3 (2019 SI redefinition).

| symbol | name | slot | QUDT cross-ref |
|---|---|---|---|
| `kg` | kilogram | M | `unit:KiloGM` |
| `m` | meter | L | `unit:M` |
| `s` | second | T | `unit:SEC` |
| `K` | kelvin | Theta | `unit:K` |
| `A` | ampere | I | `unit:A` |
| `mol` | mole | N | `unit:MOL` |
| `cd` | candela | J | `unit:CD` |

All seven defined in BIPM §2.3.1 by reference to the seven defining
constants (h, c, e, k_B, N_A, ΔνCs, K_cd) per the 2019 redefinition.

---

## SI prefixes

**Source:** BIPM SI Brochure 9th ed., Table 7.

DimFort ships 20 of the 24 SI prefixes — ASCII-only. The SI symbol μ
(micro) ships as `u`. The 2022 BIPM additions Q/R/q/r (quetta, ronna,
quecto, ronto) are skipped as not yet observed in climate / engineering
practice; user projects may add them locally.

| symbol | name | factor | symbol | name | factor |
|---|---|---|---|---|---|
| `Y` | yotta | 10²⁴ | `d` | deci | 10⁻¹ |
| `Z` | zetta | 10²¹ | `c` | centi | 10⁻² |
| `E` | exa | 10¹⁸ | `m` | milli | 10⁻³ |
| `P` | peta | 10¹⁵ | `u` | micro | 10⁻⁶ |
| `T` | tera | 10¹² | `n` | nano | 10⁻⁹ |
| `G` | giga | 10⁹ | `p` | pico | 10⁻¹² |
| `M` | mega | 10⁶ | `f` | femto | 10⁻¹⁵ |
| `k` | kilo | 10³ | `a` | atto | 10⁻¹⁸ |
| `h` | hecto | 10² | `z` | zepto | 10⁻²¹ |
| `da` | deka | 10¹ | `y` | yocto | 10⁻²⁴ |

Prefix factors are exact integers or rational strings — no floating-point.

---

## SI derived units with special names

**Source:** BIPM SI Brochure 9th ed., Tables 3 + 4.

Table 3 covers the two angular units. Table 4 covers the remaining 20
named-derived units.

| symbol | name | quantity | BIPM | QUDT cross-ref |
|---|---|---|---|---|
| `rad` | radian | plane angle | Table 3 | `unit:RAD` |
| `sr` | steradian | solid angle | Table 3 | `unit:SR` |
| `Hz` | hertz | frequency | Table 4 | `unit:HZ` |
| `N` | newton | force | Table 4 | `unit:N` |
| `Pa` | pascal | pressure | Table 4 | `unit:PA` |
| `J` | joule | energy | Table 4 | `unit:J` |
| `W` | watt | power | Table 4 | `unit:W` |
| `C` | coulomb | electric charge | Table 4 | `unit:C` |
| `V` | volt | electric potential | Table 4 | `unit:V` |
| `F` | farad | capacitance | Table 4 | `unit:FARAD` |
| `Ohm` | ohm | electric resistance | Table 4 | `unit:OHM` |
| `S` | siemens | electric conductance | Table 4 | `unit:S` |
| `Wb` | weber | magnetic flux | Table 4 | `unit:WB` |
| `T` | tesla | magnetic flux density | Table 4 | `unit:T` |
| `H` | henry | inductance | Table 4 | `unit:H` |
| `degC` | degree Celsius | temperature (affine) | Table 4 | `unit:DEG_C` |
| `lm` | lumen | luminous flux | Table 4 | `unit:LM` |
| `lx` | lux | illuminance | Table 4 | `unit:LUX` |
| `Bq` | becquerel | activity (radionuclide) | Table 4 | `unit:BQ` |
| `Gy` | gray | absorbed dose | Table 4 | `unit:GRAY` |
| `kat` | katal | catalytic activity | Table 4 | `unit:KAT` |

**Notes:**

- **`Ohm` ASCII spelling.** The SI symbol Ω is non-ASCII; DimFort ships `Ohm`
  to keep source-file portability across encodings. Projects may add `Ω` as
  an alias locally.
- **`Sv` (sievert)** is in BIPM Table 4 but DimFort **reserves the symbol
  `Sv` for sverdrup** (10⁶ m³/s ocean transport) in the climate template.
  Dose-equivalent annotations should use `Gy` (gray) until log-units feature
  lands.
- **`Np` / `B` / `dB`** (BIPM Table 8 logarithmic units) are not shipped —
  they require log-units algebra DimFort does not yet model.

---

## Non-SI units accepted for use with SI

**Source:** BIPM SI Brochure 9th ed., Tables 8-9.

| symbol | name | factor | quantity | source |
|---|---|---|---|---|
| `min` | minute | 60 s | time | Table 8 |
| `hour` | hour | 3600 s | time | Table 8 (sym: h) |
| `day` | day | 86400 s | time | Table 8 (sym: d) |
| `year` | Julian year | 31557600 s | time | informal; 365.25 d |
| `deg` | degree | π/180 rad | plane angle | Table 8 (sym: °) |
| `ha` | hectare | 10⁴ m² | area | Table 8 |
| `L` | liter | 10⁻³ m³ | volume | Table 8 |
| `t` | tonne | 10³ kg | mass | Table 8 |
| `eV` | electronvolt | 1.602176634×10⁻¹⁹ J | energy | Table 9; CODATA 2022 |
| `Da` | dalton | 1.66053906892×10⁻²⁷ kg | mass | Table 9; CODATA 2022 |

**SI symbol notes:**

- BIPM Table 8 lists `h` (hour), `d` (day), `a` (year). DimFort ships only
  the full names because the short symbols collide with prefixes hecto,
  deci, atto. Project configs may add short aliases locally.
- BIPM Table 8 lists arcminute (`'`) and arcsecond (`"`). Not shipped — the
  symbols collide with Fortran string delimiters. Project configs may add
  `arcmin` / `arcsec` aliases.
- **Year value:** 31557600 s = 365.25 d (Julian year). Alternative
  conventions (365 d common, 365.2422 d tropical) may be added in templates.

### CODATA 2022 values

The eV value is exact under the 2019 SI redefinition of the elementary
charge. The Da value is the CODATA 2022 atomic mass constant. Source:
*CODATA Recommended Values of the Fundamental Physical Constants: 2022*
(Mohr et al., 2024).

Future SI revisions or CODATA updates may change these values. DimFort
release notes will document any change.

---

## Pressure non-SI

**Source:** BIPM SI Brochure Table 9 (bar, atm); long-standing meteorology
convention (mbar, hPa, mb).

| symbol | name | factor | source |
|---|---|---|---|
| `bar` | bar | 10⁵ Pa | BIPM Table 9 |
| `mbar` | millibar | 10² Pa | meteorology convention |
| `hPa` | hectopascal | 10² Pa | meteorology convention (= mbar) |
| `mb` | (alias of mbar) | 10² Pa | meteorology shorthand |
| `atm` | standard atmosphere | 101325 Pa | BIPM Table 9 (exact) |

`kPa`, `MPa`, `GPa` etc. resolve via prefix expansion against `Pa`.

---

## Informal ratio family

Dimensionless ratios — not in BIPM Tables but universal in atmospheric
chemistry, ocean biogeochem, and mass-fraction reporting. DimFort
convention; values fix the textual interpretation (10⁻⁶ etc.).

| name | factor | use |
|---|---|---|
| `percent` | 10⁻² | mixing ratios, fractions |
| `permille` | 10⁻³ | salinity, isotope ratios (δ‰) |
| `ppm` | 10⁻⁶ | atmospheric trace species |
| `ppb` | 10⁻⁹ | atmospheric trace species |
| `ppt` | 10⁻¹² | atmospheric trace species |
| `ppmv` | 10⁻⁶ | mole fraction (atmospheric) |
| `ppbv` | 10⁻⁹ | mole fraction (atmospheric) |

`ppmv` and `ppm` differ only in semantic interpretation (volume vs.
generic ratio). DimFort assigns them distinct `quantitykind` tags so the
future soft-units lint can flag mixed use.

---

## Climate template

Activated when a project's `dimfort.toml` uncomments climate entries.
File: `docs/adoption/climate-template.dimfort.toml`.

| symbol | name | factor | quantity | source |
|---|---|---|---|---|
| `sverdrup` | sverdrup | 10⁶ m³/s | ocean volume transport | Stommel 1949; CF Conventions 1.10 |
| `psu` | practical salinity unit | dimensionless | salinity (PSS-78) | UNESCO PSS-78 (1981) |
| `S_A` | absolute salinity | dimensionless | salinity (TEOS-10) | IOC/UNESCO TEOS-10 (2010) |
| `DU` | Dobson unit | 4.4615×10⁻⁴ mol/m² | atmospheric ozone column | Dobson 1968 |
| `langley` | langley | 1 cal/cm² = 4.184×10⁴ J/m² | irradiance integral | climatology convention |
| `PSH` | peak sun hour | 1 kWh/m² = 3.6×10⁶ J/m² | irradiance integral | PV / solar resource convention |
| `kayser` | kayser | 100 m⁻¹ (= 1 cm⁻¹) | atmospheric wavenumber | atomic spectroscopy convention |
| `mWE` | meter water equivalent | 1 m | snow/ice/hydrology depth | hydrology convention |
| `mmWE` | millimeter water equivalent | 10⁻³ m | snow/ice/hydrology depth | hydrology convention |
| `gpm` | geopotential meter | (g/g₀) m | meteorology geopotential height | WMO convention |
| `cbar` | centibar | 10³ Pa | soil-water-potential, ocean depth | engineering convention |
| `degree_day` | degree day | K·d | HVAC, growing-season statistics | engineering convention |
| `degrees_north`/`east`/`true` | CF latitude/longitude/bearing | π/180 rad | CF metadata | CF Conventions 1.10 |
| `yr_BP` / `ka_BP` / `Ma_BP` | years before present | s | paleoclimate calendar | AD 1950 epoch convention |

`dam` (dekameter) is provided by the default `da` prefix on `m`.

---

## Astronomy template

File: `docs/adoption/astronomy-template.dimfort.toml`.

| symbol | name | factor | source |
|---|---|---|---|
| `au` | astronomical unit | 1.495978707×10¹¹ m | IAU 2012 (exact); BIPM 2018 |
| `ly` | light-year | 9.4607×10¹⁵ m | IAU convention |
| `pc` | parsec | 3.0857×10¹⁶ m | IAU 2015 nominal |
| `M_sun`, `R_sun`, `L_sun` | solar mass / radius / luminosity | IAU 2015 nominal values |
| `M_earth`, `R_earth` | Earth mass / radius | IAU 2015 nominal values |
| `M_jup`, `R_jup` | Jupiter mass / radius | IAU 2015 nominal values |
| `Jy` | jansky | 10⁻²⁶ W/(m²·Hz) | radio astronomy convention |
| `sfu` | solar flux unit | 10⁻²² W/(m²·Hz) | space weather convention |
| `bethe` (= `foe`) | supernova energy | 10⁴⁴ J | astrophysics convention |
| `L_bol` | bolometric luminosity | 3.0128×10²⁸ W | IAU 2015 zero-point |

Log-family entries (stellar magnitude, AB / ST magnitudes) parked until
log-units feature lands.

---

## Geosciences template

File: `docs/adoption/geosciences-template.dimfort.toml`.

| symbol | name | factor | source |
|---|---|---|---|
| `darcy` | darcy | 9.869233×10⁻¹³ m² | petroleum engineering convention |
| `mD` | millidarcy | 9.869233×10⁻¹⁶ m² | petroleum engineering |
| `bubnoff` | bubnoff unit | 1 μm/year ≈ 3.169×10⁻¹⁴ m/s | erosion rate convention |

Scale entries (NTU, FTU, FNU, FFU, phi) parked until soft-units / log-units
features land.

---

## Biology / medicine template

File: `docs/adoption/biology-medicine-template.dimfort.toml`.

| symbol | name | factor | source |
|---|---|---|---|
| `U` | enzyme unit | μmol/min ≈ 1.667×10⁻⁸ mol/s | enzymology convention |
| `bp` | base pair | dimensionless (count) | molecular biology |
| `svedberg_unit` | svedberg | 10⁻¹³ s | sedimentation rate (renamed to avoid `S` collision with siemens) |
| `BMI` | body mass index | kg/m² | medical convention |
| `clo`, `RSI` | thermal insulance | 0.155 m²·K/W, 1 m²·K/W | clothing / building insulation |

Scale / varies entries (IU, MET, cM, Mohs) parked until soft-units / scales
features land.

---

## Legacy template

File: `docs/adoption/legacy-template.dimfort.toml`. Archaeological-code
compatibility surface; not recommended for new development.

- **CGS units** (catalog §5): erg, dyn, barye, Gal, poise, stokes, gauss,
  oersted, maxwell, stilb, phot, lambert. All sourced from standard
  CGS-to-SI conversions; values are exact.
- **Imperial / US customary** (catalog §6.X(b)): inch, foot, yard, mile,
  nmi, fathom, lb, oz, slug, stone, ton (short / long), lbf, kgf, psi,
  Torr, mmHg, inHg, cal_th, cal_IT, kcal, Btu, hp.
  - Inch and foot are exact (0.0254 m and 0.3048 m).
  - Cal_th (thermochemical, 4.184 J) and cal_IT (international table,
    4.1868 J) are both exact by definition.
  - BTU value uses International Table calorie (1055.05585 J).

**Symbol-collision avoidance** for legacy entries:

- `P` (peta prefix) — poise ships without the `P` symbol alias.
- `Gs` would collide with G+s — gauss ships without the alias.

---

## License and attribution

The values in `default_units.toml` and template files are factual scientific
reference data, derived by hand from BIPM SI Brochure 9th ed. and CODATA
2022. Physical constants of the universe are facts, not subject to copyright.

The following sources were consulted as cross-validation references during
catalog construction. **No data was copied from any of them:**

- **QUDT 3.3.0** — CC BY 4.0 — <https://qudt.org/>
  - Cross-validation only; per-entry `quantitykind` tags follow QUDT
    vocabulary by convention. QUDT attribution recommended in derived works.
- **Pint `default_en.txt`** — BSD-3-Clause — <https://github.com/hgrecco/pint/>
- **UCUM essence** — Regenstrief Institute — <https://ucum.org/>
- **GNU Units `units.dat`** — GPL-3.0 — <https://www.gnu.org/software/units/>
- **CF Conventions 1.10** — community standard — <https://cfconventions.org/>
- **IUPAP SUNAMCO Red Book** — academic publication
- **BODC P06** — NERC open vocabulary

This file and its provenance survey are released under the same license
as DimFort (MIT).
