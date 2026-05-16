"""Tests for the declaration scanner.

The scanner is backed by tree-sitter (``core.ts_parser``). These tests
exercise the public ``scan_text`` API; the implementation can be
replaced as long as it produces the same ``DeclarationSite`` records.
"""
from dimfort.core.annotations import DeclarationSite, scan_text


def _decls(src: str) -> list[DeclarationSite]:
    return list(scan_text(src).declarations)


def test_single_real():
    """A bare ``real :: v`` produces one site with the name and 1-based line."""
    decls = _decls("real :: v\n")
    assert decls == [DeclarationSite(1, 1, ("v",))]


def test_declaration_list():
    """Comma-separated names on one line come out in source order."""
    decls = _decls("real :: a, b, c\n")
    assert decls == [DeclarationSite(1, 1, ("a", "b", "c"))]


def test_integer_with_attributes():
    """A ``, parameter`` qualifier and ``= 10`` initializer don't hide the name."""
    decls = _decls("integer, parameter :: N = 10\n")
    assert decls == [DeclarationSite(1, 1, ("N",))]


def test_continuation_two_lines():
    """A ``&``-continued decl spans both physical lines and lists all names.

    This is the headline scenario LFortran 0.63's AST misreports
    (position drift). Tree-sitter must return ``line_start=1, line_end=2``.
    """
    src = (
        "real :: a, &\n"
        "        b\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 2, ("a", "b"))]


def test_continuation_three_lines():
    """Three-line continuation: line_start=1 and line_end=3, all names."""
    src = (
        "real :: pressure, &\n"
        "        temperature, &\n"
        "        density\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 3, ("pressure", "temperature", "density"))]


def test_continuation_with_trailing_post_comment_on_first_line():
    """A trailing ``!<`` on the first line of a continuation doesn't truncate the decl extent."""
    src = (
        "real :: a1, &           !< @unit{kg}\n"
        "        a2, &\n"
        "        a3\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 3, ("a1", "a2", "a3"))]


def test_continuation_with_trailing_post_comment_on_last_line():
    """A trailing ``!<`` on the last line of a continuation doesn't truncate the decl extent."""
    src = (
        "real :: pressure, &\n"
        "        temperature, &\n"
        "        density   !< @unit{Pa}\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 3, ("pressure", "temperature", "density"))]


def test_type_with_dimension_attribute():
    """Identifiers inside ``dimension(n)`` are NOT counted as declared names.

    Only the entity (``v``) is declared; the ``n`` in the attribute is
    a *use* of an existing name and lives under ``type_qualifier``.
    """
    decls = _decls("real, dimension(3) :: v\n")
    assert decls == [DeclarationSite(1, 1, ("v",))]


def test_derived_type_var():
    """``type(particle) :: p`` declares ``p`` (no enclosing type scope opened)."""
    decls = _decls("type(particle) :: p\n")
    assert decls == [DeclarationSite(1, 1, ("p",))]


def test_initializer_not_in_name():
    """``real :: g = 9.81`` yields one name ``g`` — the literal is not collected."""
    decls = _decls("real :: g = 9.81\n")
    assert decls == [DeclarationSite(1, 1, ("g",))]


def test_initializer_array_with_commas():
    """Commas inside ``(/1,2,3/)`` are array-constructor separators, not entity separators."""
    decls = _decls("integer :: v(3) = (/1, 2, 3/)\n")
    assert decls == [DeclarationSite(1, 1, ("v",))]


def test_non_declaration_lines_are_ignored():
    """Only declaration statements produce sites; program headers, ``implicit none``, and assignments do not."""
    src = (
        "program p\n"
        "  implicit none\n"
        "  real :: v\n"
        "  v = 1.0\n"
        "end program\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(3, 3, ("v",))]


def test_multiple_declarations_in_order():
    """Several independent declarations come back in source order."""
    src = (
        "real :: a\n"
        "integer :: b\n"
        "logical :: c\n"
    )
    decls = _decls(src)
    assert [d.names for d in decls] == [("a",), ("b",), ("c",)]


def test_amp_inside_string_does_not_continue():
    """An ``&`` inside a string literal must not be mistaken for a line continuation.

    Without proper string handling, ``character :: s = 'A & B'`` would
    appear to continue onto the next line and would swallow the
    following ``real :: v`` declaration.
    """
    src = (
        "character(20) :: s = 'A & B'\n"
        "real :: v\n"
    )
    decls = _decls(src)
    assert decls == [
        DeclarationSite(1, 1, ("s",)),
        DeclarationSite(2, 2, ("v",)),
    ]


# ---------- type-block tracking --------------------------------------------


def test_field_decl_records_enclosing_type():
    """A decl inside ``type :: NAME ... end type`` carries ``enclosing_type=NAME``.

    Decls after ``end type`` revert to ``enclosing_type=None`` —
    confirming we leave the block scope cleanly.
    """
    src = (
        "type :: particle\n"
        "  real :: m\n"
        "  real :: v(3)\n"
        "end type\n"
        "real :: tot\n"
    )
    decls = _decls(src)
    assert [d.enclosing_type for d in decls] == ["particle", "particle", None]
    assert decls[0].names == ("m",)
    assert decls[1].names == ("v",)
    assert decls[2].names == ("tot",)


def test_type_block_with_attributes():
    """``type, public :: NAME`` still records ``NAME`` as the enclosing scope."""
    src = (
        "type, public :: state\n"
        "  real :: temp\n"
        "end type\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(2, 2, ("temp",), enclosing_type="state")]


def test_type_declaration_as_use_is_not_a_block_open():
    """``type(NAME) :: x`` declares ``x``; it must NOT open a NAME-scoped block.

    Distinguishing "type Foo definition" from "use of Foo as a type
    spec" is the source of subtle bugs in regex-based scanners. Pin it.
    """
    src = "type(particle) :: b\n"
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 1, ("b",), enclosing_type=None)]


def test_declarations_recover_around_syntax_errors():
    """Decls after a syntactically broken statement are still recovered.

    The capability tree-sitter unlocked over the previous scanner: a
    parse error in the middle of a subroutine no longer hides the
    declarations that follow. The old regex scanner already tolerated
    *unknown* statements (it never validated non-decl lines); with
    tree-sitter the tolerance extends to *ungrammatical* Fortran.
    """
    src = (
        "subroutine s\n"
        "  real :: a\n"
        "  *** completely broken ***\n"
        "  real :: c\n"
        "end subroutine\n"
    )
    decls = _decls(src)
    names = [d.names for d in decls]
    assert ("a",) in names
    assert ("c",) in names
