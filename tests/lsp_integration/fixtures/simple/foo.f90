module simple_mod
  implicit none
  real :: c_sound  !< @unit{m/s}
  real :: t        !< @unit{s}
contains
  subroutine compute(dist)
    real, intent(out) :: dist  !< @unit{m}
    dist = c_sound * t
  end subroutine compute
end module simple_mod
