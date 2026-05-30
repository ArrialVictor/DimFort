! demos/tour.f90
!
! A five-minute DimFort tour. Open this file in an editor with a
! DimFort companion installed to see hovers and inlay hints, or run
!
!     dimfort check --scale demos/tour.f90
!
! to see the diagnostics on the terminal. demos/README.md walks
! through the expected output line by line.

program tour
  implicit none

  ! State variables for a small moist-thermodynamics routine.
  real :: T               !< @unit{K}            ! absolute temperature
  real :: p               !< @unit{Pa}           ! total pressure
  real :: p_hpa           !< @unit{hPa}          ! same dimension, different scale
  real :: rho             !< @unit{kg/m^3}       ! air density
  real :: v               !< @unit{m/s}          ! flow speed
  real :: R_d             !< @unit{J/(kg*K)}     ! specific gas constant for dry air
  real :: e_sat           !< @unit{Pa}           ! saturation vapor pressure
  real :: rho_drop        !< @unit{kg/m^3}       ! droplet bulk density
  real :: r_drop                                 ! droplet radius — left unannotated on purpose

  ! Log-pressure coordinate: a quantity that lives in LOG(Pa) space.
  real :: lnp             !< @unit{LOG(Pa)}      ! log of local pressure
  real :: lnp_ref         !< @unit{LOG(Pa)}      ! log of a reference pressure
  real :: dlnp            !< @unit{1}            ! dimensionless log-ratio
  real :: p_back          !< @unit{Pa}           ! pressure recovered from lnp

  ! ---- R4.4 — pure-literal initialisation autocasts to the LHS unit. ----
  ! No diagnostic fires: the literal RHS adopts the declared LHS unit.
  T = 273.15
  p = 1.01325e5
  R_d = 287.05

  ! ---- Correct: ideal gas law balances to kg/m^3 cleanly. ----
  rho = p / (R_d * T)

  ! ---- S001 — same dimension (pressure), different magnitude factor. ----
  ! Fires only under --scale (or [scale] enabled = true in .dimfort.toml).
  p_hpa = p

  ! ---- H001 — classic homogeneity error: m/s := Pa / (kg/m^3) gives m^2/s^2. ----
  v = p / rho

  ! ---- U005 + U020 — escape hatch for an empirical fit. ----
  ! `r_drop` is read in a checked context but its declaration carries no
  ! @unit{}, so U005 fires on the declaration above. The (...)**(-0.922)
  ! raises a dimensioned quantity to a non-rational exponent, which DimFort
  ! cannot derive (D1.4); @unit_assume{} asserts the result instead and
  ! surfaces as a U020 INFO so the audit trail stays greppable.
  rho_drop = 1.0e3 * 0.178 * (r_drop * 2.0 * 1000.0)**(-0.922)   !< @unit_assume{kg/m^3 : empirical-fit power-law}

  ! ---- LOG / EXP wrapper algebra — no diagnostic. ----
  ! Quantities tagged LOG(Pa) live in log-pressure space. The log
  ! homomorphism collapses a *difference* of two LOG(Pa) values to
  ! dimensionless (LOG(a) − LOG(b) → LOG(a/b), and LOG(1) → 1), and
  ! `exp` applied to a LOG-tagged value cancels back to the inner unit.
  ! DimFort tracks both rewrites automatically — no escape hatch needed.
  dlnp = lnp - lnp_ref     ! LOG(Pa) − LOG(Pa) → 1 (dimensionless)
  p_back = exp(lnp)        ! EXP(LOG(Pa)) → Pa

end program tour
