! Pressure-related routines, fully annotated and dimensionally
! consistent. Pulls its constants from constants_mod via a use clause;
! opening this file therefore puts both files in DimFort's workset.
module pressure_clean
  use constants_mod, only: r_dry, g, p_ref
  implicit none
contains

  ! Ideal-gas-law pressure from density and temperature.
  ! p = rho * R_dry * T  →  kg/m^3 * J/kg/K * K = J/m^3 = Pa
  subroutine ideal_gas_pressure(rho, t, p)
    real, intent(in)  :: rho  !< @unit{kg/m^3}
    real, intent(in)  :: t    !< @unit{K}
    real, intent(out) :: p    !< @unit{Pa}

    p = rho * r_dry * t
  end subroutine ideal_gas_pressure

  ! Hydrostatic pressure decrease for a thin layer of thickness dz.
  ! dp = -rho * g * dz  →  kg/m^3 * m/s^2 * m = Pa
  subroutine hydrostatic_step(rho, dz, dp)
    real, intent(in)  :: rho  !< @unit{kg/m^3}
    real, intent(in)  :: dz   !< @unit{m}
    real, intent(out) :: dp   !< @unit{Pa}

    dp = -rho * g * dz
  end subroutine hydrostatic_step

  ! Convert a pressure to a fraction of the reference sea-level pressure.
  ! Dimensionless ratio.
  subroutine pressure_ratio(p, ratio)
    real, intent(in)  :: p      !< @unit{Pa}
    real, intent(out) :: ratio  !< @unit{1}

    ratio = p / p_ref
  end subroutine pressure_ratio

end module pressure_clean
