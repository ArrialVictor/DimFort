module a_mod
  implicit none
  real :: x   !< @unit{m}
  real :: bad !< @unit{s}
contains
  subroutine compute()
    ! H001 site: `bad : s` assigned `x : m`. Without folder_a's
    ! dimfort.toml override (H001 = off), this fires H001. The
    ! multi-folder posture-pin test asserts H001 is NOT in the
    ! diagnostics — only true when folder_a's config is applied.
    bad = x
  end subroutine compute
end module a_mod
