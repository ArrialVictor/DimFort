! Mirror of pressure_clean, with three deliberate problems so the
! coverage layer has yellow and red lines to paint:
!
!   - missing_unit:   unannotated intermediate variable → U005 yellow
!   - bad_addition:   adding pressure to gravity → H002 red
!   - sign_mistake:   dropped the minus on the hydrostatic step,
!                     dimensionally fine but physically wrong;
!                     DimFort won't catch this one (no diagnostic),
!                     showing that "green" means dimensionally
!                     verified, not physically correct.
module pressure_broken
  use constants_mod, only: r_dry, g, p_ref
  implicit none
contains

  ! Compute pressure with an unannotated working variable.
  ! DimFort fires U005 on `working`; the propagation rule paints the
  ! assignment line and the final use site yellow even though the
  ! result expression is dimensionally fine.
  subroutine ideal_gas_pressure_u005(rho, t, p)
    real, intent(in)  :: rho     !< @unit{kg/m^3}
    real, intent(in)  :: t       !< @unit{K}
    real, intent(out) :: p       !< @unit{Pa}
    real :: working              ! intentionally unannotated → U005

    working = rho * r_dry
    p = working * t
  end subroutine ideal_gas_pressure_u005

  ! Add gravity to a pressure. The dimensions disagree (Pa + m/s^2),
  ! so this fires H002 and paints the offending line red.
  subroutine bad_addition(p_in, p_out)
    real, intent(in)  :: p_in   !< @unit{Pa}
    real, intent(out) :: p_out  !< @unit{Pa}

    p_out = p_in + g
  end subroutine bad_addition

  ! Hydrostatic step with the wrong sign — physically pressure
  ! decreases with height. DimFort can't catch this (the dimensions
  ! still match), so the line is verified green. Useful reminder of
  ! what dimensional analysis does and doesn't cover.
  subroutine hydrostatic_step_wrong_sign(rho, dz, dp)
    real, intent(in)  :: rho  !< @unit{kg/m^3}
    real, intent(in)  :: dz   !< @unit{m}
    real, intent(out) :: dp   !< @unit{Pa}

    dp = rho * g * dz  ! should be -rho * g * dz
  end subroutine hydrostatic_step_wrong_sign

end module pressure_broken
