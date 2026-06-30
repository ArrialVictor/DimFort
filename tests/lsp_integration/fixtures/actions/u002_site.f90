! Fires U002 with a suggested_rewrite ('kg2' -> 'kg^2'). Code action
! on the diagnostic line offers "Replace with 'kg^2'" as a
! WorkspaceEdit (no command delegation; server applies directly).
module u002_site_mod
  implicit none
  real :: bogus  !< @unit{kg2}
end module u002_site_mod
