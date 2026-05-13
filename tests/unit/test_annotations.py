"""Tests for the Doxygen ``@unit{...}`` scanner (stage 1)."""
from dimfort.core.annotations import AnnotationKind, scan_text


def _scan(src: str):
    res = scan_text(src)
    return res.annotations, res.errors


def test_trailing_post_annotation():
    src = "real :: v   !< @unit{m/s}\n"
    anns, errs = _scan(src)
    assert errs == ()
    assert len(anns) == 1
    a = anns[0]
    assert a.kind is AnnotationKind.POST
    assert a.unit_text == "m/s"
    assert a.line == 1


def test_preceding_block_with_doxygen_arrow():
    src = (
        "!> @unit{kg}\n"
        "real :: m\n"
    )
    anns, errs = _scan(src)
    assert errs == ()
    assert [a.kind for a in anns] == [AnnotationKind.PRE]
    assert anns[0].line == 1
    assert anns[0].unit_text == "kg"


def test_preceding_block_with_double_bang():
    src = (
        "!! @unit{Pa}\n"
        "real :: p\n"
    )
    anns, _ = _scan(src)
    assert anns[0].kind is AnnotationKind.PRE
    assert anns[0].unit_text == "Pa"


def test_plain_comment_is_ignored():
    src = "real :: v   ! @unit{m/s}\n"  # missing > < or !
    anns, errs = _scan(src)
    assert anns == ()
    assert errs == ()


def test_bang_inside_string_is_not_a_comment():
    src = "character(20) :: s = '!< @unit{m/s}'\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert errs == ()


def test_escaped_quote_inside_string():
    # The doubled '' is an escape; the !< after it is genuinely a comment.
    src = "character(20) :: s = 'it''s ok' !< @unit{1}\n"
    anns, _ = _scan(src)
    assert len(anns) == 1
    assert anns[0].unit_text == "1"


def test_complex_unit_text_preserved():
    src = "real :: f !< @unit{(kg*m)/s^2}\n"
    anns, _ = _scan(src)
    assert anns[0].unit_text == "(kg*m)/s^2"


def test_whitespace_inside_braces_stripped():
    src = "real :: f !< @unit{   m/s   }\n"
    anns, _ = _scan(src)
    assert anns[0].unit_text == "m/s"


def test_empty_braces_emit_error():
    src = "real :: v !< @unit{}\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert len(errs) == 1
    assert "empty" in errs[0].reason


def test_unclosed_brace_emits_error():
    src = "real :: v !< @unit{m/s\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert len(errs) == 1
    assert "unclosed" in errs[0].reason


def test_multiple_unit_on_one_line_keeps_first_flags_rest():
    src = "real :: v !< @unit{m} @unit{s}\n"
    anns, errs = _scan(src)
    assert len(anns) == 1
    assert anns[0].unit_text == "m"
    assert len(errs) == 1
    assert "more than one" in errs[0].reason


def test_multi_line_block_collects_each_annotation():
    src = (
        "!> Compute the force.\n"
        "!> @unit{kg*m/s^2}\n"
        "!! Continuation of the doc.\n"
        "real :: f\n"
    )
    anns, _ = _scan(src)
    assert len(anns) == 1
    assert anns[0].kind is AnnotationKind.PRE
    assert anns[0].line == 2


def test_column_is_one_based_at_at_unit():
    # `real :: v !< @unit{m}` — column of the '@' should be 14 (1-based).
    src = "real :: v !< @unit{m}\n"
    anns, _ = _scan(src)
    assert anns[0].column == src.index("@unit") + 1


def test_pre_and_post_in_same_file():
    src = (
        "!> @unit{m}\n"
        "real :: x\n"
        "real :: y !< @unit{kg}\n"
    )
    anns, _ = _scan(src)
    assert [a.kind for a in anns] == [AnnotationKind.PRE, AnnotationKind.POST]
    assert anns[0].line == 1
    assert anns[1].line == 3
