! Polymorphic function with body-check that forces a tyvar binding —
! fires H023. Used by test_polymorphism_payloads_H020_H023.
module polymorphism_mod
  implicit none
contains
  subroutine f(x, c, y)
    real, intent(in)  :: x   !< @unit{'a}
    real, intent(in)  :: c   !< @unit{kg}
    real, intent(out) :: y   !< @unit{'a}
    ! H023: x + c forces 'a to bind to kg in the body — the signature
    ! is not actually polymorphic. Supersedes H002 (which would fire
    ! on a concrete mismatch).
    y = x + c
  end subroutine f
end module polymorphism_mod
