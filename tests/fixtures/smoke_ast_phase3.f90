! Phase 3 AST-only checker fixture.
! Exercises chained derived-type access (a%b%c), array element
! (a(i)), array slice (a(:)), and bounded slice (a(1:n)) — all of
! which appear as ``FuncCallOrArray`` in the AST and need the
! variable-fallback resolver.
module m_phase3
  implicit none

  type :: inner
    real :: x   !< @unit{m}
  end type

  type :: outer
    type(inner) :: nest
    real :: y   !< @unit{kg}
  end type

contains

  subroutine demo
    type(outer) :: o
    real    :: a(10)      !< @unit{m/s}
    real    :: r          !< @unit{m}
    real    :: bad        !< @unit{kg}
    integer :: i

    ! OK: o%nest%x is m, r is m.
    r = o%nest%x

    ! H001: o%y is kg, r is m.
    r = o%y

    ! OK: a(i) is m/s, bad is kg → would be H001 — but make it ok with bad := a, only here r expects m/s? Let's stay explicit:
    ! Replace `r` with proper m/s var.

    ! H001: array element a(1) is m/s, r is m.
    r = a(1)

    ! H001: array slice a(:) is m/s, r is m.
    r = a(:)

    ! H001: bounded slice a(1:5) is m/s, r is m.
    r = a(1:5)

    ! H001: array element a(i) is m/s, bad is kg.
    bad = a(i)
  end subroutine

end module
