program main
  use geo
  implicit none

  ! Decl initialisers avoid bare-literal H001 noise.
  real :: s     = 1.0    !< @unit{m}
  real :: a     = 0.0    !< @unit{m^2}
  real :: bad_a = 0.0    !< @unit{kg}
  real :: v     = 1.0    !< @unit{m}
  real :: r     = 0.5    !< @unit{1}
  real :: bad_r = 1.0    !< @unit{m}

  ! OK.
  a = box_area(s)

  ! H001: function returns m^2, target is kg.
  bad_a = box_area(s)

  ! OK.
  call scale(v, r)

  ! H004: `factor` arg must be dimensionless, bad_r is m.
  call scale(v, bad_r)

end program
