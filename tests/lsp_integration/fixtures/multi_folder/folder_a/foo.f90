module a_mod
  implicit none
  real :: x  !< @unit{m}
  real :: y  !< @unit{s}
contains
  subroutine compute()
    real :: bad
    ! This would normally fire H001 (m + s mismatch), but folder_a's
    ! dimfort.toml turns H001 off. The multi-folder posture-pin test
    ! asserts the server uses THIS config, not folder_b's defaults.
    bad = x + y
  end subroutine compute
end module a_mod
