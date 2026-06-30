module file_b_mod
  implicit none
  real :: b_kg  !< @unit{kg}
  real :: b_m   !< @unit{m}
contains
  subroutine demo()
    real :: bad  !< @unit{kg}
    bad = b_m   ! H001 in file_b
  end subroutine demo
end module file_b_mod
