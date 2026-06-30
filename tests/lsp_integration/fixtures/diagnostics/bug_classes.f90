! Triggers multiple diagnostic codes in one file. Used by
! test_didopen_expected_codes to assert each known bug class fires.
module bug_classes_mod
  implicit none
  real :: m_val   !< @unit{m}
  real :: s_val   !< @unit{s}
  real :: bad     !< @unit{??}              ! U002: invalid unit syntax
  real :: out_val !< @unit{m}
contains
  subroutine accepts_meters(arg)
    real, intent(in) :: arg                  !< @unit{m}
  end subroutine accepts_meters
  subroutine demo()
    real :: implicit_var
    out_val = m_val + s_val                  ! H002: + with mismatched units
    out_val = m_val + 5.0                    ! H010: literal 5.0 takes the m unit
    implicit_var = m_val * 1.0               ! U005: implicit_var has no @unit
    out_val = s_val                          ! H001: assignment unit mismatch
  end subroutine demo
end module bug_classes_mod
