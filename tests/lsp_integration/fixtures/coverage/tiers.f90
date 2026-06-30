! Lines of each coverage tier: green (verified OK), yellow (warning),
! red (error), blue (unparsed). Used by test_dimfort_linestatus_
! tier_classifications.
module tiers_mod
  implicit none
  real :: m_val   !< @unit{m}
  real :: s_val   !< @unit{s}
contains
  subroutine demo()
    real :: dist  !< @unit{m}
    real :: dur   !< @unit{s}
    real :: bare        ! No annotation → use site is yellow.
    ! GREEN: m = m, all annotated, no warning.
    dist = m_val
    ! YELLOW: bare unannotated, used in expression.
    bare = m_val
    ! RED: m ≠ s assignment mismatch → H001.
    dur = m_val
  end subroutine demo
end module tiers_mod
