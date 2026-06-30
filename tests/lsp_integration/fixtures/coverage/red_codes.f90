! Regression: 0.2.4 #h-codes-missing-red-tier — H020/H021/H022/H023
! (polymorphism) plus S003 and U002 were missing from the red tier
! codeset; lines firing these errors painted green or yellow
! instead of red. Test asserts at least one of the polymorphism
! H-codes correctly tier-classifies as red.
module red_codes_mod
  implicit none
contains
  subroutine f(x, c, y)
    real, intent(in)  :: x   !< @unit{'a}
    real, intent(in)  :: c   !< @unit{kg}
    real, intent(out) :: y   !< @unit{'a}
    ! H023 fires here — must paint RED.
    y = x + c
  end subroutine f
end module red_codes_mod
