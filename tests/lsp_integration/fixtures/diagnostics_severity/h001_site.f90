! H001 site — verifies that the dimfort.toml override to "info" wins
! (the 0.2.3 #info-severity-override-silent-reject regression).
module severity_mod
  implicit none
  real :: m_val   !< @unit{m}
  real :: s_val   !< @unit{s}
  real :: out_val !< @unit{m}
contains
  subroutine demo()
    out_val = s_val   ! H001 — severity must be INFO per dimfort.toml
  end subroutine demo
end module severity_mod
