! Shared physical constants used by every other file in this demo.
! All values annotated; this module should report 100% coverage on
! its own.
module constants_mod
  implicit none

  ! Standard gravitational acceleration at Earth's surface.
  real, parameter :: g = 9.80665  !< @unit{m/s^2}

  ! Specific gas constant for dry air.
  real, parameter :: r_dry = 287.058  !< @unit{J/kg/K}

  ! Isobaric specific heat capacity of dry air.
  real, parameter :: c_p = 1004.0  !< @unit{J/kg/K}

  ! Reference temperature (0 °C in Kelvin).
  real, parameter :: t_ref = 273.15  !< @unit{K}

  ! Reference pressure at mean sea level.
  real, parameter :: p_ref = 101325.0  !< @unit{Pa}
end module constants_mod
