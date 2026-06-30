! H004 arg mismatch site. Hover on the call must show the
! ``(expected <unit>)`` trailer naming the formal's expected unit
! — the 0.2.0 #panel-rule-ids-dropped regression replaced the
! debug-noise ``(R4.2)`` with the user-readable expected-unit form.
module hover_arg_mismatch_mod
  implicit none
contains
  subroutine accepts_kg(weight)
    real, intent(in) :: weight   !< @unit{kg}
  end subroutine accepts_kg

  subroutine caller()
    real :: m_val   !< @unit{m}
    ! Hover on the call must show `expected kg` trailer somewhere.
    call accepts_kg(m_val)
  end subroutine caller
end module hover_arg_mismatch_mod
