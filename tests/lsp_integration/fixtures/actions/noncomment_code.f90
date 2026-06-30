! Fixture for the completion guard test. Asking for completion on
! the assignment line (line 7, `i = 0`) must return None — there's
! no `@unit{` brace context anywhere on that line, so the server
! must guard against firing unit-name completion. The 0.2.3
! #completion-LSP-scoping regression was that completion fired
! everywhere; the fix scopes it to inside `@unit{` braces.
module noncomment_code_mod
  implicit none
contains
  subroutine demo()
    integer :: i
    i = 0
  end subroutine demo
end module noncomment_code_mod
