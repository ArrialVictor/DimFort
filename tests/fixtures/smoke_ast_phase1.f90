! Phase 1 AST-only checker fixture.
! Exercises H002 (add/sub mismatch), H003 (dimensionless intrinsic),
! H004 (call arg mismatch), Pow with integer exponent, sqrt as
! transforming intrinsic, and a user-defined subroutine call.
module m_phase1
  implicit none
contains

  function squared(side) result(out)
    real, intent(in) :: side   !< @unit{m}
    real :: out                !< @unit{m^2}
    out = side ** 2            ! Pow with integer exponent.
  end function

  subroutine bump(x, factor)
    real, intent(inout) :: x       !< @unit{m}
    real, intent(in)    :: factor  !< @unit{1}
    x = x * factor
  end subroutine

  subroutine demo
    real :: len    !< @unit{m}
    real :: area   !< @unit{m^2}
    real :: kg_x   !< @unit{kg}
    real :: dim_x  !< @unit{1}

    ! OK: squared(len) returns m^2, area is m^2.
    area = squared(len)

    ! H004: bump() expects a m-unit first arg; kg_x is kg.
    call bump(kg_x, dim_x)

    ! H002: adding kg + m.
    len = len + kg_x

    ! H003: sin() requires dimensionless argument; len is m.
    dim_x = sin(len)

    ! Clean: sqrt(area) is sqrt(m^2) = m, matches len.
    len = sqrt(area)
  end subroutine

end module
