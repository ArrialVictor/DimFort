! Top-level driver that pulls in every other file in this demo via
! use clauses. Opening driver.f90 in the editor gives DimFort the
! biggest workset of the four — all three modules plus the driver
! itself — so the side-panel "WS:" coverage segment differs visibly
! from the per-file "File:" segment.
program driver
  use constants_mod,   only: t_ref, p_ref
  use pressure_clean,  only: ideal_gas_pressure, hydrostatic_step
  use pressure_broken, only: ideal_gas_pressure_u005
  implicit none

  real :: rho        !< @unit{kg/m^3}
  real :: t          !< @unit{K}
  real :: dz         !< @unit{m}
  real :: p          !< @unit{Pa}
  real :: p_alt      !< @unit{Pa}
  real :: dp         !< @unit{Pa}

  ! Standard-atmosphere starting point.
  rho = 1.225
  t   = t_ref + 15.0
  dz  = 100.0

  ! Two independent pressure computations, one clean and one with a
  ! U005 lurking inside.
  call ideal_gas_pressure(rho, t, p)
  call ideal_gas_pressure_u005(rho, t, p_alt)

  ! Hydrostatic adjustment over the thin layer.
  call hydrostatic_step(rho, dz, dp)

  print *, "Surface pressure (Pa):           ", p
  print *, "Surface pressure (U005 path):    ", p_alt
  print *, "Reference sea-level pressure:    ", p_ref
  print *, "Hydrostatic dp over ", dz, " m:  ", dp

end program driver
