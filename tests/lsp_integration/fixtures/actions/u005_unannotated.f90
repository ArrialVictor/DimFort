! Unannotated declaration -> code action offers Add @unit{} snippet
! with $0 cursor placement between the braces. Pin the 0.2.1
! #snippet-cursor-placement regression: $0 must land BETWEEN the
! braces so the user's typing immediately goes inside @unit{}.
module u005_unannotated_mod
  implicit none
  real :: missing
end module u005_unannotated_mod
