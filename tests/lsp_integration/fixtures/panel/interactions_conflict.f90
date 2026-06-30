! Regression: 0.2.1 #interactions-x001-conflict. Cross-site unit
! conflicts weren't detected; only per-statement check ran. The
! `dimfort/interactions` request should detect that `shared_var`
! has conflicting unit requirements across writes / reads here.
module interactions_conflict_mod
  implicit none
  real :: shared_var   !< @unit{m}
  real :: kg_input     !< @unit{kg}
  real :: m_input      !< @unit{m}
contains
  subroutine writer_a()
    ! Site 1: write `shared_var` as kg (mismatches its m annotation).
    shared_var = kg_input
  end subroutine writer_a

  subroutine writer_b()
    ! Site 2: write `shared_var` as m (matches annotation).
    shared_var = m_input
  end subroutine writer_b

  subroutine reader()
    real :: r   !< @unit{m}
    ! Site 3: read `shared_var` expecting m.
    r = shared_var
  end subroutine reader
end module interactions_conflict_mod
