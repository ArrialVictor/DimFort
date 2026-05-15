! File A: declares its OWN `w` (m/s). Should never see file B's `w`.
module m_a
  implicit none
contains
  subroutine s_a
    real :: w     !< @unit{m/s}
    real :: dist  !< @unit{m}
    real :: time  !< @unit{s}
    ! Clean: dist/time is m/s, w is m/s.
    w = dist / time
  end subroutine
end module
