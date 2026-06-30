module usage_mod
  use defs_mod, only: shared_speed
  implicit none
contains
  subroutine demo()
    real :: x   !< @unit{m/s}
    ! Goto-definition on `shared_speed` here must jump to defs.f90.
    x = shared_speed
  end subroutine demo
end module usage_mod
