! demos/broken.f90
!
! A bug zoo. Each block is a self-contained failure mode. Run:
!     dimfort check demos/broken.f90
!
! Use this as a quick lookup — "what does H002 look like?" — find
! the H002 block, see the message it produces. demos/tour.f90 walks
! the *good* case with narrative; this file is the lookup table for
! the codes that fire when things go wrong.

program broken
  implicit none

  real :: x         !< @unit{m}
  real :: y         !< @unit{s}
  real :: dx_dt     !< @unit{m/s}
  real :: q         !< @unit{kg}
  real :: ratio     !< @unit{1}
  real :: r                              ! unannotated on purpose — fires U005

  ! ---- H001 — assignment LHS unit doesn't match RHS unit. ----
  dx_dt = x                              ! m·s⁻¹ ≠ m

  ! ---- H002 — `+` operands have different dimensions. ----
  dx_dt = x + y                          ! m + s

  ! ---- H003 — strict-dimensionless intrinsic given a unitful arg. ----
  ratio = sin(x)                         ! sin expects dimensionless, got m

  ! ---- H004 — call-site formal/actual mismatch. ----
  call require_seconds(x)                ! formal: s, actual: m

  ! ---- H010 / D1.5 — magic-number literal in a compound expression. ----
  x = x + 2.0                            ! warning: prefer a typed PARAMETER

  ! ---- U005 — `r` is read in a unit-checked context but unannotated. ----
  q = q * r                              ! kg * (unannotated) — fires on r's decl

contains

  subroutine require_seconds(s_arg)
    real, intent(in) :: s_arg   !< @unit{s}
  end subroutine require_seconds

end program broken
