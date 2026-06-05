"""Tests for the unit-completion trigger heuristics."""
from dimfort.lsp.completion import _comment_active, _inside_string_literal


def test_inside_string_double_quoted():
    assert _inside_string_literal('print *, "hello @unit{')


def test_inside_string_single_quoted():
    assert _inside_string_literal("print *, 'see @unit{")


def test_not_inside_string_after_close():
    assert not _inside_string_literal('print *, "done" then @unit{')


def test_not_inside_string_when_no_quotes():
    assert not _inside_string_literal("real :: x  ! @unit{")


def test_comment_active_after_bang():
    assert _comment_active("real :: x  ! @unit{")


def test_comment_inactive_before_bang():
    assert not _comment_active("real :: x  @unit{")


def test_bang_inside_string_does_not_activate_comment():
    """A ``!`` inside a string is part of the literal, not a comment
    delimiter — completion should not fire."""
    assert not _comment_active('print *, "no ! here" @unit{')
