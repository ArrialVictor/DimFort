! Polymorphic function with a 'a-typed return. Hover at the call site
! resolves the return tyvar to the concrete unit passed in (regression:
! 0.2.3.1 #polymorphic-return-unbound — the call site used to show
! the raw tyvar `'a` instead of the bound unit).
module hover_polymorphism_mod
  implicit none
contains
  function identity(x) result(y)
    real, intent(in) :: x   !< @unit{'a}
    real             :: y   !< @unit{'a}
    y = x
  end function identity

  subroutine caller()
    real :: m_val   !< @unit{m}
    real :: result  !< @unit{m}
    ! At hover on `identity(m_val)`, the return unit must be bound
    ! to `m` (the call-site argument), not raw `'a`.
    result = identity(m_val)
  end subroutine caller
end module hover_polymorphism_mod
