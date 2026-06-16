program smoke
  implicit none

  ! Inline POST
  real :: vel             !< @unit{m/s}

  ! PRE block (Doxygen Fortran style) on a single-line decl
  !> Air mass per cell.
  !> @unit{kg}
  real :: mass

  ! Declaration list with POST (apply-to-all on a single line)
  real :: x, y, z         !< @unit{m}

  ! Continued declaration — per-line POST on each continuation line.
  ! The 0.2.7 per-line attach rule binds an annotation to the names
  ! whose declaration tokens end on the annotation's physical line.
  real :: pressure, &     !< @unit{Pa}
          temperature, &  !< @unit{Pa}
          density         !< @unit{Pa}

  ! Same shape with a different convention: each variable still gets
  ! its own annotation; the unit happens to be the same across all
  ! three but doesn't have to be.
  real :: a1, &           !< @unit{kg}
          a2, &           !< @unit{kg}
          a3              !< @unit{kg}

  ! Single-line PRE block above a multi-name SINGLE-line declaration
  ! (PRE on a multi-line decl is refused with U024 — see the design
  ! note; the per-line POST shape above is the right pattern there).
  !> @unit{1}
  real :: alpha, beta, gamma

end program smoke
