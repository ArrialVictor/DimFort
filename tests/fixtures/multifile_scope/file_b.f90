! File B: also has a local `w`, but annotated as KG (not m/s).
! In the absence of proper file-level scoping, file A's check would
! see B's annotation and emit a bogus H001 on its own clean
! assignment. With proper scoping, both files check independently.
module m_b
  implicit none
contains
  subroutine s_b
    real :: w     !< @unit{kg}
    real :: mass  !< @unit{kg}
    w = mass    ! Clean on B's own terms.
  end subroutine
end module
