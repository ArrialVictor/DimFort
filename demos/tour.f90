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

  real :: p_ref           !< @unit{Pa}           ! reference pressure (e.g. 1e5 Pa)
  real :: p_ratio         !< @unit{1}            ! p / p_ref, computed in log space

  ! ---- R4.4 — pure-literal initialisation autocasts to the LHS unit. ----
  ! No diagnostic fires: the literal RHS adopts the declared LHS unit.
  T = 273.15
  p = 1.01325e5
  R_d = 287.05

  ! ---- Correct: ideal gas law balances to kg/m^3 cleanly. ----
  rho = p / (R_d * T)

  ! ---- S001 — same dimension (pressure), different magnitude factor. ----
  ! Fires only under --scale (or [scale] enabled = true in dimfort.toml).
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
  ! Numerically stable computation of a pressure ratio:
  !
  !     exp(log(a) - log(b))  ≡  a / b
  !
  ! DimFort traces the whole chain through three rewrites:
  !   log(p)        : Pa          →  LOG(Pa)         (log homomorphism)
  !   LOG(p) - LOG(p_ref)        →  LOG(Pa / Pa)    (subtraction in log space)
  !   LOG(Pa / Pa)               →  LOG(1) → 1      (dimensionless collapse)
  !   exp(1)                     →  1               (EXP ∘ identity)
  !
  ! The result types as dimensionless, matching the LHS. No annotation
  ! is needed beyond the LHS — the algebra discharges every wrapper on
  ! the way through. Few static checkers cover this; DimFort does it as
  ! a first-class rewrite (see docs/unit-algebra.md §R5).
  p_ratio = exp(log(p) - log(p_ref))

  ! ---- H004 — call-site unit checking. ----
  ! `dyn_p` expects (speed [m/s], density [kg/m^3]) and returns [Pa].
  ! DimFort matches actuals to formals by position and reports the first
  ! mismatch as H004 on the call. Here, `T` [K] is passed where a [m/s]
  ! argument is required — a class of bug intra-statement homogeneity
  ! checking can't catch.
  e_sat = dyn_p(T, rho)

contains

  ! Dynamic pressure: 1/2 * rho * v^2.
  ! Body is dimensionally clean — kg/m^3 * (m/s)^2 = kg/(m*s^2) = Pa.
  function dyn_p(spd, dens) result(p)
    real, intent(in) :: spd        !< @unit{m/s}
    real, intent(in) :: dens       !< @unit{kg/m^3}
    real             :: p          !< @unit{Pa}
    p = 0.5 * dens * spd**2
  end function dyn_p

end program tour
