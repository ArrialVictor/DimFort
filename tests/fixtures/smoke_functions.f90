module geo
  implicit none
contains

  ! Takes a length, returns an area.
  function box_area(side) result(out)
    real, intent(in) :: side    !< @unit{m}
    real :: out                 !< @unit{m^2}
    out = side * side
  end function

  ! Scales a quantity by a dimensionless factor.
  subroutine scale(x, factor)
    real, intent(inout) :: x      !< @unit{m}
    real, intent(in)    :: factor !< @unit{1}
    x = x * factor
  end subroutine

end module

program main
  use geo
  implicit none

  ! Use declaration-initialisers to avoid bare-literal H001 noise.
  real :: s     = 1.0    !< @unit{m}
  real :: a     = 0.0    !< @unit{m^2}
  real :: bad_a = 0.0    !< @unit{kg}
  real :: v     = 1.0    !< @unit{m}
  real :: r     = 0.5    !< @unit{1}
  real :: bad_r = 1.0    !< @unit{m}

  ! OK: matching units throughout.
  a = box_area(s)

  ! H001 on the assignment: function returns m^2, target is kg.
  bad_a = box_area(s)

  ! OK subroutine call.
  call scale(v, r)

  ! H004: `factor` arg must be dimensionless, bad_r is m.
  call scale(v, bad_r)

end program main
