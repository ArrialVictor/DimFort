! A second file in the same workspace, for tab-switch and didClose tests.
module bar_mod
  implicit none
  real :: temperature  !< @unit{K}
contains
  subroutine compute(out_t)
    real, intent(out) :: out_t  !< @unit{K}
    out_t = temperature
  end subroutine compute
end module bar_mod
