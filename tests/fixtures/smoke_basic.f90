program smoke
  implicit none

  ! Inline POST
  real :: vel             !< @unit{m/s}

  ! PRE block (Doxygen Fortran style)
  !> Air mass per cell.
  !> @unit{kg}
  real :: mass

  ! Declaration list with POST (apply-to-all)
  real :: x, y, z         !< @unit{m}

  ! Continued declaration - POST on the LAST line (form B)
  real :: pressure, &
          temperature, &
          density         !< @unit{Pa}

  ! Continued declaration - POST on the FIRST line (form C, ends with &)
  real :: a1, &           !< @unit{kg}
          a2, &
          a3

  ! Continued declaration - PRE block before it (form A)
  !> @unit{1}
  real :: alpha, &
          beta, &
          gamma

end program smoke
