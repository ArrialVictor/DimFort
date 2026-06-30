module file_a_mod
  implicit none
  real :: a_m   !< @unit{m}
  real :: a_s   !< @unit{s}
contains
  subroutine demo()
    real :: bad  !< @unit{m}
    bad = a_s   ! H001 in file_a
  end subroutine demo
end module file_a_mod
