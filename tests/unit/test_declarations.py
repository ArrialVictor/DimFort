"""Tests for the source-side declaration scanner."""
from dimfort.core.annotations import DeclarationSite, scan_text


def _decls(src: str) -> list[DeclarationSite]:
    return list(scan_text(src).declarations)


def test_single_real():
    decls = _decls("real :: v\n")
    assert decls == [DeclarationSite(1, 1, ("v",))]


def test_declaration_list():
    decls = _decls("real :: a, b, c\n")
    assert decls == [DeclarationSite(1, 1, ("a", "b", "c"))]


def test_integer_with_attributes():
    decls = _decls("integer, parameter :: N = 10\n")
    assert decls == [DeclarationSite(1, 1, ("N",))]


def test_continuation_two_lines():
    src = (
        "real :: a, &\n"
        "        b\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 2, ("a", "b"))]


def test_continuation_three_lines():
    src = (
        "real :: pressure, &\n"
        "        temperature, &\n"
        "        density\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 3, ("pressure", "temperature", "density"))]


def test_continuation_with_trailing_post_comment_on_first_line():
    src = (
        "real :: a1, &           !< @unit{kg}\n"
        "        a2, &\n"
        "        a3\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 3, ("a1", "a2", "a3"))]


def test_continuation_with_trailing_post_comment_on_last_line():
    src = (
        "real :: pressure, &\n"
        "        temperature, &\n"
        "        density   !< @unit{Pa}\n"
    )
    decls = _decls(src)
    assert decls == [DeclarationSite(1, 3, ("pressure", "temperature", "density"))]


def test_type_with_dimension_attribute():
    decls = _decls("real, dimension(3) :: v\n")
    assert decls == [DeclarationSite(1, 1, ("v",))]


def test_derived_type_var():
    decls = _decls("type(particle) :: p\n")
    assert decls == [DeclarationSite(1, 1, ("p",))]


def test_initializer_not_in_name():
    decls = _decls("real :: g = 9.81\n")
    assert decls == [DeclarationSite(1, 1, ("g",))]


def test_initializer_array_with_commas():
    # Commas inside `( /1,2,3/ )` are NOT entity separators.
    decls = _decls("integer :: v(3) = (/1, 2, 3/)\n")
    assert decls == [DeclarationSite(1, 1, ("v",))]


def test_non_declaration_lines_are_ignored():
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
    src = (
        "real :: a\n"
        "integer :: b\n"
        "logical :: c\n"
    )
    decls = _decls(src)
    assert [d.names for d in decls] == [("a",), ("b",), ("c",)]


def test_amp_inside_string_does_not_continue():
    # The `&` is inside a string literal — should NOT trigger continuation.
    src = (
        "character(20) :: s = 'A & B'\n"
        "real :: v\n"
    )
    decls = _decls(src)
    assert decls == [
        DeclarationSite(1, 1, ("s",)),
        DeclarationSite(2, 2, ("v",)),
    ]
