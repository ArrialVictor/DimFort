! Scale-aware hover. With [scale] enabled, the unit display includes
! the multiplicative factor — e.g. `100·kg·m⁻¹·s⁻²` (hPa = 100 Pa),
! not just `kg·m⁻¹·s⁻²`. Pins the 0.2.1 #scale-mode-display-uniform
! contract — the scale factor must appear consistently in hover.
module hover_scale_mod
  implicit none
  ! hPa is 100 Pa; with scale on, hover unit display includes the
  ! 100 factor (regression: 0.2.1 #scale-mode-display-uniform).
  real :: pressure   !< @unit{hPa}
contains
  subroutine demo()
    real :: p_local   !< @unit{hPa}
    p_local = pressure
  end subroutine demo
end module hover_scale_mod
