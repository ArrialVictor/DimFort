! demos/affine.f90
!
! Scale-family sequel to demos/tour.f90. Three scale codes appear:
!
!   S001 — same dimension, different magnitude factor (Pa vs hPa).
!   S002 — same dimension and factor, different zero-point (degC vs K).
!   S003 — error from `@unit_affine_conversion{src -> tgt}` whose
!          arithmetic doesn't actually perform the stated conversion.
!
! Run:
!     dimfort check --scale demos/affine.f90
!
! S001 has no blessing directive: carry the factor on a typed
! PARAMETER, or pay the warning. S002 *can* be blessed by
! `@unit_affine_conversion`, but DimFort verifies the arithmetic on
! the way in — it is the verified counterpart to `@unit_assume`
! (which is trusted). Wrong arithmetic upgrades the silence into S003.

program affine_tour
  implicit none

  real :: p_pa       !< @unit{Pa}    ! pressure in pascals
  real :: p_hpa      !< @unit{hPa}   ! same dimension, different magnitude
  real :: t_c        !< @unit{degC}  ! temperature in Celsius
  real :: t_k        !< @unit{K}     ! temperature in Kelvin
  real :: t_k_bad    !< @unit{K}     ! target of a deliberately-broken conversion

  ! Typed PARAMETER for the °C→K offset: carries its own unit so
  ! it can't drift into being a magic number elsewhere.
  real, parameter :: RTT = 273.15    !< @unit{K}

  ! ---- S001 — magnitude (factor) mismatch, no offset issue. ----
  p_pa = p_hpa

  ! ---- S002 — un-blessed offset (affine) mismatch. ----
  ! Both K and degC are temperature, so the H-series sees no problem;
  ! but the zero-points disagree, and the scale family flags that.
  t_k = t_c + RTT

  ! ---- Verified blessing — silent. ----
  ! The conversion function below carries the @unit_affine_conversion
  ! directive on its body; DimFort verifies the arithmetic performs the
  ! stated degC → K conversion and emits no diagnostic.
  t_k = c_to_k(t_c)

  ! ---- S003 — verified conversion that isn't. ----
  ! Same directive, wrong arithmetic (subtraction instead of addition):
  ! DimFort rejects it as an error.
  t_k_bad = c_to_k_broken(t_c)

contains

  pure function c_to_k(t) result(out)
    real, intent(in) :: t          !< @unit{degC}
    real             :: out        !< @unit{K}
    real, parameter  :: RTT = 273.15  !< @unit{K}
    out = t + RTT                  !< @unit_affine_conversion{degC -> K}
  end function c_to_k

  pure function c_to_k_broken(t) result(out)
    real, intent(in) :: t          !< @unit{degC}
    real             :: out        !< @unit{K}
    real, parameter  :: RTT = 273.15  !< @unit{K}
    out = t - RTT                  !< @unit_affine_conversion{degC -> K}
  end function c_to_k_broken

end program affine_tour
