module hover_basic_mod
  implicit none
  real :: c_sound  !< @unit{m/s}
  real :: t        !< @unit{s}
contains
  subroutine demo()
    real :: dist  !< @unit{m}
    dist = c_sound * t
  end subroutine demo
end module hover_basic_mod
