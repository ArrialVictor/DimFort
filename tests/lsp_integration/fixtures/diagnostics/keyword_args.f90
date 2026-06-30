! Regression: 0.2.3 #keyword-args-silent-miss — keyword-argument call
! sites weren't being walked, so a unit mismatch at `call f(b=x)` went
! silent. Test asserts H004 fires on this construction.
module keyword_args_mod
  implicit none
contains
  subroutine receiver(a, b)
    real, intent(in) :: a   !< @unit{m}
    real, intent(in) :: b   !< @unit{s}
  end subroutine receiver

  subroutine caller()
    real :: bad   !< @unit{kg}
    real :: x_m   !< @unit{m}
    ! H004 expected: `b` expects s, got kg.
    call receiver(a=x_m, b=bad)
  end subroutine caller
end module keyword_args_mod
