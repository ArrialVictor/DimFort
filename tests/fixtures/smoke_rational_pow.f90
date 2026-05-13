program smoke_rational_pow
  implicit none

  real :: area    = 4.0    !< @unit{m^2}
  real :: side    = 0.0    !< @unit{m}
  real :: bad     = 0.0    !< @unit{kg}

  ! OK: m^2 ** 0.5 = m.
  side = area ** 0.5

  ! H001: m^2 ** 0.5 = m, but target is kg.
  bad = area ** 0.5

end program
