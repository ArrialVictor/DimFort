! Completion fixture. The comment `!< @unit{` triggers unit-name
! completion. Outside a comment, the `@unit{` shape is a parse
! error and completion is guarded (0.2.3 #completion-LSP-scoping
! regression).
module completion_site_mod
  implicit none
  real :: target_var  !< @unit{
end module completion_site_mod
