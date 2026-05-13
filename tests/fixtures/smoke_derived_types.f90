program smoke_derived_types
  implicit none

  type :: particle
    real :: m       !< @unit{kg}
    real :: q       !< @unit{C}
    real :: v(3)    !< @unit{m/s}
  end type

  type(particle) :: b
  real :: mass    = 1.0     !< @unit{kg}
  real :: charge  = 1.0     !< @unit{C}
  real :: badmass = 1.0     !< @unit{m}

  ! OK: kg = kg.
  b%m = mass

  ! H001: target kg, value m.
  b%m = badmass

  ! OK: reading b%q (Coulombs) into charge (Coulombs).
  charge = b%q

end program
