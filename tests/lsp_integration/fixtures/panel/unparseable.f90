! Regression: 0.2.0 #panel-scope-recover. The panel Scope section
! used to blank entirely if any statement in the routine couldn't
! be parsed (tree-sitter wrapped the whole routine in ERROR). The
! fix made Scope recover line-based so variables declared before
! the parse error still appear. This fixture has a deliberately
! broken statement; the variables before it must still surface.
module unparseable_mod
  implicit none
contains
  subroutine demo()
    real :: x   !< @unit{m}
    real :: y   !< @unit{s}
    ! Deliberately invalid Fortran (extra parenthesis, etc.):
    @@@ broken syntax here @@@
    x = y
  end subroutine demo
end module unparseable_mod
