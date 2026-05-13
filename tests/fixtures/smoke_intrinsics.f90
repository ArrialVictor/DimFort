program smoke_intrinsics
  implicit none

  real :: area    !< @unit{m^2}
  real :: side    !< @unit{m}
  real :: angle   !< @unit{1}
  real :: ratio   !< @unit{1}
  real :: length  !< @unit{m}

  ! sqrt halves the dimension - both ok.
  side  = sqrt(area)
  ratio = sin(angle)

  ! sin requires a dimensionless argument -> H003.
  ratio = sin(length)

end program smoke_intrinsics
