! Hover on intrinsics — log/sqrt/abs. Each must render the same
! root-plus-immediate-children tree shape as a user call, per the
! 0.2.1 #hover-tree-shape-unified regression.
module hover_intrinsics_mod
  implicit none
  real :: p1   !< @unit{Pa}
  real :: p2   !< @unit{Pa}
  real :: area !< @unit{m^2}
  real :: t    !< @unit{s}
contains
  subroutine demo()
    real :: lp   !< @unit{LOG(Pa)}
    real :: side !< @unit{m}
    real :: dur  !< @unit{s}
    lp = log(p1)
    side = sqrt(area)
    dur = abs(t)
  end subroutine demo
end module hover_intrinsics_mod
