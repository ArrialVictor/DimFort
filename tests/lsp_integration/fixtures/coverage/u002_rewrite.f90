! Regression: 0.2.3 #cache-serde-u002-rewrite-lost. U002 fires here
! with a suggested_rewrite payload (`kg2` → `kg^2`). Warm-restart
! cache reads must preserve that payload — without the fix, the
! "did you mean?" hint vanished on warm runs.
module u002_rewrite_mod
  implicit none
  real :: bogus  !< @unit{kg2}
end module u002_rewrite_mod
