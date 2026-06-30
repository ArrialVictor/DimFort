! Minimal file used by test_rapid_didchange_burst to send 10 edits
! within 100ms. The fixture starts unannotated (silent), the test
! injects/reverts an @unit annotation per edit and asserts only the
! final state's diagnostics arrive.
module burst_mod
  implicit none
  real :: x
  real :: y
contains
  subroutine demo()
    x = y
  end subroutine demo
end module burst_mod
