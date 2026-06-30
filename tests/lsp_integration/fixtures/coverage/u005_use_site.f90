! Regression: 0.2.6 #coverage-u005-propagation — U005 unannotated-use
! sites weren't propagated to coverage; removing an annotation
! showed green instead of yellow at the use site. Test asserts the
! use line (`bad = something`) carries yellow when `bad` is
! unannotated.
module u005_use_site_mod
  implicit none
  real :: src_m   !< @unit{m}
contains
  subroutine demo()
    real :: bad        ! Deliberately unannotated.
    ! Use site: must paint yellow (U005-propagated).
    bad = src_m
  end subroutine demo
end module u005_use_site_mod
