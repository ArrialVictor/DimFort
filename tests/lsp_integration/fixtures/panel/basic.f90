! Basic fixture for inlay + panelInfo shape tests.
module panel_basic_mod
  implicit none
  real :: c_sound  !< @unit{m/s}
  real :: t        !< @unit{s}
contains
  subroutine demo()
    real :: dist  !< @unit{m}
    real :: bare        ! No annotation — inlay should be empty for this one.
    dist = c_sound * t
    bare = dist
  end subroutine demo
end module panel_basic_mod
