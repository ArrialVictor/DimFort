module file_b_mod
  implicit none
  real :: b_kg  !< @unit{kg}
  real :: b_m   !< @unit{m}
contains
  subroutine demo()
    real :: bad  !< @unit{kg}
    bad = b_m   ! H001: kg ≠ m
  end subroutine demo
end module file_b_mod
