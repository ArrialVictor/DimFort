! Regression: 0.2.3 #lhs-subscript-silent-miss — array subscripts on
! LHS weren't walked, so a mixed-unit index expression went silent.
! Test asserts H002 fires inside the subscript.
module lhs_subscript_mod
  implicit none
contains
  subroutine demo()
    real :: arr(10, 10)
    integer :: i   !< @unit{m}
    integer :: j   !< @unit{s}
    ! H002 expected inside the subscript: (i + j) mixes m and s.
    arr(int(i + j), 1) = 1.0
  end subroutine demo
end module lhs_subscript_mod
