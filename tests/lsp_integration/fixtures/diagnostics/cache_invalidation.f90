! Minimal fixture for test_didchange_invalidates_cache. A single
! H001 site. The test edits the LHS annotation to match the RHS;
! after the edit the file is fully clean (no H001, no other codes
! depending on the LHS unit). If the cache fails to invalidate,
! H001 stays.
module cache_invalidation_mod
  implicit none
  real :: source_s   !< @unit{s}
  real :: dest       !< @unit{m}
contains
  subroutine demo()
    dest = source_s   ! H001: m ≠ s
  end subroutine demo
end module cache_invalidation_mod
