! @unit_assume{...} escape hatch — fires U020 INFO with the assumed
! marker in payload. Used by test_unit_assume_payload.
module unit_assume_mod
  implicit none
contains
  subroutine demo()
    real :: r        !< @unit{m}
    real :: rho      !< @unit{kg/m^3}
    rho = 1.0e3 * 0.178 * (r * 2.0 * 1000.0)**(-0.922)   !< @unit_assume{kg/m^3 : empirical-fit Brandes2007}
  end subroutine demo
end module unit_assume_mod
