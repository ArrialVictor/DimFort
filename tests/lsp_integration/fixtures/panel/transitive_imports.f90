! Transitive imports surface in dimfort/interactions. The chain
! `density_use_mod` USE `physics_mod` which re-exports `density`
! that originated in `base_mod`. The interactions request on
! `density` should surface the re-export chain (the panel renders
! `density : ? 🟡  via physics_base`).
module base_mod
  implicit none
  real :: density   !< @unit{kg/m^3}
end module base_mod

module physics_mod
  use base_mod, only: density
  implicit none
end module physics_mod

module density_use_mod
  use physics_mod, only: density
  implicit none
contains
  subroutine demo()
    real :: rho_local   !< @unit{kg/m^3}
    rho_local = density
  end subroutine demo
end module density_use_mod
