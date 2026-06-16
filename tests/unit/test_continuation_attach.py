"""Coverage for the 0.2.7 per-variable continuation-line attach rule.

Each test in this module corresponds to a row in design note §8
(:doc:`docs/design/shipped/per-variable-continuation-attach`).
Tests run against the production source scanner (``scan_text``) so
the paren-aware end-line is exercised end-to-end rather than via
hand-constructed ``NameSpan`` fixtures.
"""
from __future__ import annotations

from dimfort.core.annotations import scan_text
from dimfort.core.attach import attach


def _attach_source(src: str):
    return attach(scan_text(src))


# §8 #1 — single-line single-name decl
def test_single_line_single_name():
    res = _attach_source(
        "subroutine s\n"
        "  real :: x   !< @unit{m}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"x": "m"}
    assert res.pre_on_multiline == []
    assert res.migration_detections == []


# §8 #2 — single-line multi-name decl
def test_single_line_multi_name():
    res = _attach_source(
        "subroutine s\n"
        "  real :: x, y   !< @unit{m}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"x": "m", "y": "m"}


# §8 #3 — continuation, annotation on first line only (the hard-switch case)
def test_continuation_annotation_on_first_line_only_fires_u025():
    """The migration footgun: an author wrote a single annotation
    thinking it would attach to all names. Under the per-line rule
    only ``a1`` is annotated; ``a2`` / ``a3`` remain unannotated,
    and U025 surfaces the partial-coverage pattern."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: a1, &   !< @unit{kg}\n"
        "          a2, &\n"
        "          a3\n"
        "end subroutine\n"
    )
    assert res.var_units == {"a1": "kg"}
    assert len(res.migration_detections) == 1
    mig = res.migration_detections[0]
    assert mig.unannotated_names == ("a2", "a3")


# §8 #4 — continuation, annotation on every line
def test_continuation_per_line_annotations():
    """Clear per-line intent: every continuation line carries its
    own annotation. Each annotation attaches to the name ending on
    its line. No U025 — the migration pattern doesn't fire when
    every name is annotated."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: a, &   !< @unit{m}\n"
        "          b, &   !< @unit{kg}\n"
        "          c      !< @unit{s}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"a": "m", "b": "kg", "c": "s"}
    assert res.migration_detections == []


# §8 #5 — annotation on some lines, others U005
def test_continuation_partial_per_line_fires_u025():
    """Annotation on middle line attaches to that line's name; the
    later unannotated name triggers U025 (info)."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: a, &\n"
        "          b, &   !< @unit{kg}\n"
        "          c\n"
        "end subroutine\n"
    )
    assert res.var_units == {"b": "kg"}
    assert len(res.migration_detections) == 1


# §8 #6 — annotation on last line only (today's well-supported path)
def test_continuation_annotation_on_last_line_only():
    """Annotation on the last continuation line attaches to the
    name ending on the last line. ``a`` and ``b`` end on earlier
    lines and remain unannotated. (Today's behavior preserved.)
    No U025 — the annotation isn't on a *non-last* line."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: a, &\n"
        "          b, &\n"
        "          c      !< @unit{Pa}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"c": "Pa"}
    assert res.migration_detections == []


# §8 #7 — `!`-as-POST style on continuation lines
def test_plain_bang_post_on_per_line_continuation():
    """Plain ``!`` after the trailing ``&`` is POST under the
    scanner's eligibility rules on declaration lines."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: a, &   ! @unit{m}\n"
        "          b, &   ! @unit{kg}\n"
        "          c      ! @unit{s}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"a": "m", "b": "kg", "c": "s"}


# §8 #8 — `&` inside array bounds (paren-aware boundary via tree-sitter)
def test_amp_inside_array_bounds_spans_correctly():
    """``REAL :: foo(N, &`` continued on next line with ``M)``: the
    declaration tokens for ``foo`` end on the line carrying ``M)``,
    not on the line carrying the first ``&``. Tree-sitter's
    ``sized_declarator`` end_point handles the paren tracking."""
    src = (
        "subroutine s\n"
        "  real :: foo(N, &\n"
        "              M), bar    !< @unit{kg}\n"
        "end subroutine\n"
    )
    res = _attach_source(src)
    # Annotation on the line with `M), bar` attaches to names
    # whose tokens end there — both foo (paren closes here) and
    # bar (declarator ends here).
    assert res.var_units == {"foo": "kg", "bar": "kg"}


# §8 #9 — type spec with explicit bounds
def test_type_spec_with_dimension_attribute():
    """``REAL, DIMENSION(:,:) :: x`` — single-line decl; per-line
    rule degenerates to attach-all."""
    res = _attach_source(
        "subroutine s\n"
        "  real, dimension(:,:) :: arr    !< @unit{m}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"arr": "m"}


# §8 #10 — per-name array bounds with continuation
def test_per_name_array_bounds_with_continuation():
    """``REAL :: foo(:,:), bar(:)`` split across lines: each name's
    bounds end on its own line; per-line annotations attach
    correctly."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: foo(:,:), &   !< @unit{m}\n"
        "          bar(:)        !< @unit{kg}\n"
        "end subroutine\n"
    )
    assert res.var_units == {"foo": "m", "bar": "kg"}


# §8 #11 — PRE on single-line multi-name decl (preserved)
def test_pre_on_single_line_multi_name_attaches_to_all():
    """PRE block above a single-line declaration: unambiguous,
    attaches to every name on that line (today's behavior
    preserved)."""
    res = _attach_source(
        "subroutine s\n"
        "  !> @unit{1}\n"
        "  real :: alpha, beta, gamma\n"
        "end subroutine\n"
    )
    assert res.var_units == {"alpha": "1", "beta": "1", "gamma": "1"}


# §8 #12 — PRE unit annotation on multi-line decl (synthetic; U024)
def test_pre_on_multi_line_decl_emits_u024():
    """Multi-line decl + PRE unit annotation: refused with U024.
    The author is asked to switch to per-line POST. Empirically 0
    sites in the surveyed corpora; the diagnostic is a safety net."""
    res = _attach_source(
        "subroutine s\n"
        "  !> @unit{m}\n"
        "  real :: alpha, &\n"
        "          beta, &\n"
        "          gamma\n"
        "end subroutine\n"
    )
    assert res.var_units == {}
    assert len(res.pre_on_multiline) == 1
    rec = res.pre_on_multiline[0]
    assert "multi-line" in rec.reason


# §8 #13 — PRE comment block above multi-line decl with no unit annotation
def test_pre_block_without_unit_annotation_does_not_fire_u024():
    """Doc-header PRE blocks above multi-line decls don't contain
    a unit annotation — empirically the dominant pattern (224
    union sites, 0 with unit content). U024 must NOT fire."""
    res = _attach_source(
        "subroutine s\n"
        "  !> Section header — module-physics constants.\n"
        "  !> History: 2002-2024.\n"
        "  real :: alpha, &\n"
        "          beta, &\n"
        "          gamma\n"
        "end subroutine\n"
    )
    assert res.pre_on_multiline == []


# §8 #15 — empty continuation line (`&` alone)
def test_empty_continuation_line_is_noop():
    """A continuation with only ``&`` on a line: no annotation, no
    issue."""
    res = _attach_source(
        "subroutine s\n"
        "  real :: a, &\n"
        "          &\n"
        "          b    !< @unit{kg}\n"
        "end subroutine\n"
    )
    # `b` ends on the last line, `a` ends on the first line.
    assert "b" in res.var_units
