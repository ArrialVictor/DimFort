program smoke_check
  implicit none

  real :: mass         !< @unit{kg}
  real :: accel        !< @unit{m/s^2}
  real :: force        !< @unit{kg*m/s^2}
  real :: speed        !< @unit{m/s}

  ! Correct: force = mass * accel.
  force = mass * accel

  ! Wrong: assigning a kg/s value to a m/s variable -> H001.
  speed = mass / accel

end program smoke_check
