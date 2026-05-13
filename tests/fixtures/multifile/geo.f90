module geo
  implicit none
contains

  ! Takes a length, returns an area.
  function box_area(side) result(out)
    real, intent(in) :: side    !< @unit{m}
    real :: out                 !< @unit{m^2}
    out = side * side
  end function

  ! Scales a length by a dimensionless factor.
  subroutine scale(x, factor)
    real, intent(inout) :: x      !< @unit{m}
    real, intent(in)    :: factor !< @unit{1}
    x = x * factor
  end subroutine

end module
