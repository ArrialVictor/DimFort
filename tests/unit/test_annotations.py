"""Tests for the Doxygen ``@unit{...}`` comment scanner (stage 1).

The comment scanner is hand-written string-aware text walking — it
needs to know about Fortran string literals so a ``!`` inside
``"foo!"`` isn't a comment marker. Independent of the declaration
scanner (which is tree-sitter-backed).
"""
from dimfort.core.annotations import AnnotationKind, scan_text


def _scan(src: str):
    res = scan_text(src)
    return res.annotations, res.errors


def test_trailing_post_annotation():
    """``!< @unit{...}`` produces one POST annotation on the same line."""
    src = "real :: v   !< @unit{m/s}\n"
    anns, errs = _scan(src)
    assert errs == ()
    assert len(anns) == 1
    a = anns[0]
    assert a.kind is AnnotationKind.POST
    assert a.unit_text == "m/s"
    assert a.line == 1


def test_preceding_block_with_doxygen_arrow():
    """``!>`` opens a PRE block; the annotation attaches to the next decl."""
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
    """``!!`` is equivalent to ``!>`` — both open PRE blocks."""
    src = (
        "!! @unit{Pa}\n"
        "real :: p\n"
    )
    anns, _ = _scan(src)
    assert anns[0].kind is AnnotationKind.PRE
    assert anns[0].unit_text == "Pa"


def test_plain_comment_trailing_on_decl_is_post():
    """Spec §3 / §10 expansion: a plain ``!`` containing the default
    pattern, trailing a declaration line, is now eligible (POST)."""
    src = "real :: v   ! @unit{m/s}\n"
    anns, errs = _scan(src)
    assert errs == ()
    assert len(anns) == 1
    assert anns[0].kind is AnnotationKind.POST
    assert anns[0].unit_text == "m/s"


def test_plain_comment_above_decl_is_pre():
    """Spec §3.2: a plain ``!`` standalone with the very next line a
    declaration is eligible (PRE)."""
    src = "! @unit{kg}\nreal :: v\n"
    anns, errs = _scan(src)
    assert errs == ()
    assert len(anns) == 1
    assert anns[0].kind is AnnotationKind.PRE
    assert anns[0].unit_text == "kg"


def test_plain_at_unit_trailing_on_assignment_orphans():
    """Spec §5 + §8.3: plain-``!`` ``@unit{}`` on an assignment is
    eligible, captured by the scanner, then surfaced by the U023
    emitter as a wrong-statement-kind diagnostic. At scan level the
    annotation IS produced; attach orphans it (no decl matches)."""
    src = (
        "real :: v\n"
        "v = 1.0 ! @unit{m/s}\n"
    )
    res = scan_text(src)
    assert len(res.annotations) == 1
    assert res.annotations[0].line == 2
    # The annotation has nowhere to attach — assignment isn't a decl.
    # multifile reroutes the orphan to U023 (covered by an
    # integration test below).


def test_plain_comment_standalone_with_blank_then_decl_is_skipped():
    """Spec §3.2 strict immediacy: blank line between the standalone
    comment and the declaration disqualifies the comment."""
    src = (
        "! @unit{kg}\n"
        "\n"
        "real :: v\n"
    )
    anns, errs = _scan(src)
    assert anns == ()
    assert errs == ()


def test_plain_at_unit_standalone_above_assignment_orphans():
    """Spec §5: plain-``!`` standalone above an assignment is
    eligible. ``@unit{}`` captures but later surfaces as U023 (the
    target is an assignment, not a declaration)."""
    src = (
        "real :: v\n"
        "! @unit{kg}\n"
        "v = 1.0\n"
    )
    res = scan_text(src)
    assert len(res.annotations) == 1
    assert res.annotations[0].kind is AnnotationKind.PRE


def test_two_plain_comments_only_last_eligible():
    """Spec §3.2: 'no second comment line between'. With two stacked
    plain-``!`` comments above a decl, only the one adjacent to the
    decl is eligible."""
    src = (
        "! first prose only\n"
        "! @unit{Pa}\n"
        "real :: p\n"
    )
    anns, _ = _scan(src)
    assert len(anns) == 1
    assert anns[0].kind is AnnotationKind.PRE
    assert anns[0].unit_text == "Pa"


def test_bang_inside_string_is_not_a_comment():
    """A ``!<`` appearing inside a string literal must not start an annotation."""
    src = "character(20) :: s = '!< @unit{m/s}'\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert errs == ()


def test_escaped_quote_inside_string():
    """Doubled quotes (``''``) inside a string don't close it.

    The ``!<`` after the closing quote is a genuine comment and the
    annotation in it is real.
    """
    src = "character(20) :: s = 'it''s ok' !< @unit{1}\n"
    anns, _ = _scan(src)
    assert len(anns) == 1
    assert anns[0].unit_text == "1"


def test_complex_unit_text_preserved():
    """Operators and parens inside ``{...}`` survive verbatim.

    Unit-algebra parsing is the parser's job, not the scanner's.
    """
    src = "real :: f !< @unit{(kg*m)/s^2}\n"
    anns, _ = _scan(src)
    assert anns[0].unit_text == "(kg*m)/s^2"


def test_whitespace_inside_braces_stripped():
    """Surrounding spaces inside ``{...}`` are trimmed so ``{ m/s }`` matches ``{m/s}``."""
    src = "real :: f !< @unit{   m/s   }\n"
    anns, _ = _scan(src)
    assert anns[0].unit_text == "m/s"


def test_empty_braces_emit_error():
    """``@unit{}`` is malformed: no annotation, one error tagged 'empty'."""
    src = "real :: v !< @unit{}\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert len(errs) == 1
    assert "empty" in errs[0].reason


def test_unclosed_brace_emits_error():
    """``@unit{m/s`` (no closing brace before EOL) is malformed: error tagged 'unclosed'."""
    src = "real :: v !< @unit{m/s\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert len(errs) == 1
    assert "unclosed" in errs[0].reason


def test_multiple_unit_on_one_line_drops_all_flags_each():
    """Two ``@unit{...}`` on one comment line: ambiguous intent. No
    annotation attaches (the variable surfaces as unannotated and
    fires U005 downstream if used in math). Every capture site is
    flagged with U001 so the user sees the full extent."""
    src = "real :: v !< @unit{m} @unit{s}\n"
    anns, errs = _scan(src)
    assert anns == ()
    assert len(errs) == 2
    assert all("more than one" in e.reason for e in errs)


def test_multi_line_block_collects_each_annotation():
    """A multi-line PRE block (``!>`` followed by ``!!``) yields the embedded ``@unit{...}``.

    Only the ``@unit{}`` lines emit annotations; the prose lines
    (``!> Compute the force.``, ``!! Continuation...``) do not.
    """
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
    """The reported column points at the ``@`` of ``@unit``, in 1-based indexing."""
    src = "real :: v !< @unit{m}\n"
    anns, _ = _scan(src)
    assert anns[0].column == src.index("@unit") + 1


def test_pre_and_post_in_same_file():
    """A file with one PRE and one POST annotation returns both with correct kinds and lines."""
    src = (
        "!> @unit{m}\n"
        "real :: x\n"
        "real :: y !< @unit{kg}\n"
    )
    anns, _ = _scan(src)
    assert [a.kind for a in anns] == [AnnotationKind.PRE, AnnotationKind.POST]
    assert anns[0].line == 1
    assert anns[1].line == 3


# ---------------------------------------------------------------------------
# @unit_affine_conversion scanner (Phase 2c)
# ---------------------------------------------------------------------------


def test_affine_arrow_form():
    """``!< @unit_affine_conversion{degC -> K}`` yields one record with the
    src/tgt split on the arrow."""
    res = scan_text("tk = tc + r  !< @unit_affine_conversion{degC -> K}\n")
    assert len(res.affine_conversions) == 1
    a = res.affine_conversions[0]
    assert (a.src, a.tgt) == ("degC", "K")
    assert a.line == 1


def test_affine_comma_synonym():
    """The comma form is an accepted synonym for the arrow."""
    res = scan_text("tk = tc + r  !< @unit_affine_conversion{degC, K}\n")
    assert (res.affine_conversions[0].src, res.affine_conversions[0].tgt) == (
        "degC", "K",
    )


def test_affine_missing_separator_is_error():
    """No ``->`` or ``,`` ⇒ a malformed-scan error, no record."""
    res = scan_text("tk = tc + r  !< @unit_affine_conversion{degC K}\n")
    assert res.affine_conversions == ()
    assert len(res.errors) == 1


def test_affine_does_not_collide_with_unit_or_assume():
    """``@unit_affine_conversion`` must not be picked up by the ``@unit`` or
    ``@unit_assume`` scanners (distinct directives)."""
    res = scan_text("tk = tc + r  !< @unit_affine_conversion{degC -> K}\n")
    assert res.annotations == ()
    assert res.assumes == ()
    assert len(res.affine_conversions) == 1


# ---------------------------------------------------------------------------
# Configured patterns (0.2.2 — spec §2)
# ---------------------------------------------------------------------------


def test_bracket_pattern_trailing_on_decl():
    """A user-configured ``[``/``]`` pattern recognises ``[m/s]`` as
    a trailing unit on a decl line."""
    from dimfort.core.unit_patterns import UnitPattern
    src = "real :: v   ! horizontal wind speed [m/s]\n"
    res = scan_text(
        src,
        unit_patterns=(
            UnitPattern(open="@unit{", close="}"),
            UnitPattern(open="[", close="]"),
        ),
    )
    assert len(res.annotations) == 1
    assert res.annotations[0].kind is AnnotationKind.POST
    assert res.annotations[0].unit_text == "m/s"


def test_first_listed_pattern_wins():
    """Spec §8.1: with both ``@unit{}`` and ``[``/``]`` configured,
    ``@unit{}`` wins regardless of position in the comment."""
    from dimfort.core.unit_patterns import UnitPattern
    src = "real :: v   !< wind speed [m/s] @unit{kg}\n"
    res = scan_text(
        src,
        unit_patterns=(
            UnitPattern(open="@unit{", close="}"),
            UnitPattern(open="[", close="]"),
        ),
    )
    assert len(res.annotations) == 1
    assert res.annotations[0].unit_text == "kg"
    # The bracket capture differs → recorded as a pattern conflict
    # for the future U021 emitter.
    assert len(res.pattern_conflicts) == 1
    c = res.pattern_conflicts[0]
    assert c.directive == "@unit"
    assert c.first_unit_text == "kg"
    assert c.second_unit_text == "m/s"


def test_identical_captures_no_conflict():
    """Spec §8.2: identical text across patterns is silent."""
    from dimfort.core.unit_patterns import UnitPattern
    src = "real :: v   !< @unit{m/s} also [m/s]\n"
    res = scan_text(
        src,
        unit_patterns=(
            UnitPattern(open="@unit{", close="}"),
            UnitPattern(open="[", close="]"),
        ),
    )
    assert len(res.annotations) == 1
    assert res.annotations[0].unit_text == "m/s"
    assert res.pattern_conflicts == ()


def test_structured_pattern_assume_via_brackets():
    """A bracket-configured assume pattern works on a plain ``!``
    trailing an assignment statement (the directive's target kind)."""
    from dimfort.core.unit_patterns import StructuredPattern
    src = (
        "real :: tracer_eff, ratio, area\n"
        "tracer_eff = ratio * area   ! eff. surface ratio [m^2: Andreas 1989]\n"
    )
    res = scan_text(
        src,
        assume_patterns=(
            StructuredPattern(open="@unit_assume{", close="}", sep=":"),
            StructuredPattern(open="[", close="]", sep=":"),
        ),
    )
    assert len(res.assumes) == 1
    assert res.assumes[0].unit_text == "m^2"
    assert res.assumes[0].reason == "Andreas 1989"


def test_structured_pattern_affine_via_brackets():
    from dimfort.core.unit_patterns import StructuredPattern
    src = (
        "real :: sst_k, sst_c\n"
        "sst_k = sst_c + 273.15   !< [degC -> K]\n"
    )
    res = scan_text(
        src,
        affine_patterns=(
            StructuredPattern(
                open="@unit_affine_conversion{", close="}", sep="->"
            ),
            StructuredPattern(open="[", close="]", sep="->"),
        ),
    )
    assert len(res.affine_conversions) == 1
    assert res.affine_conversions[0].src == "degC"
    assert res.affine_conversions[0].tgt == "K"


# ---------------------------------------------------------------------------
# Multi-var declarations (spec §6 — unified)
# ---------------------------------------------------------------------------


def test_multivar_with_non_canonical_pattern_attaches_to_all():
    """Spec §6 (Q1-unified): a configured `[...]` pattern attaches to
    every name on a multi-variable declaration, same as the canonical
    `@unit{...}` form."""
    from dimfort.core.unit_patterns import UnitPattern
    src = "real :: a, b, c   ! [m/s]\n"
    res = scan_text(
        src,
        unit_patterns=(
            UnitPattern(open="@unit{", close="}"),
            UnitPattern(open="[", close="]"),
        ),
    )
    assert len(res.annotations) == 1
    assert res.annotations[0].unit_text == "m/s"


def test_multivar_with_canonical_pattern_still_attaches_to_all():
    """The legacy behavior is preserved: `! @unit{m/s}` on
    `real :: a, b, c` attaches to all three."""
    src = "real :: a, b, c   ! @unit{m/s}\n"
    res = scan_text(src)
    assert len(res.annotations) == 1
    assert res.annotations[0].unit_text == "m/s"


def test_multivar_two_patterns_on_one_line_fires_more_than_one():
    """Per §6 safety-net: writing two captures of the same pattern
    on one line is ambiguous — no annotation attaches (every variable
    on the line surfaces as unannotated), and both capture sites are
    flagged 'more than one … on one line' so the author sees the
    full extent of the ambiguity."""
    from dimfort.core.unit_patterns import UnitPattern
    src = "real :: a, b   ! [m] [s]\n"
    res = scan_text(
        src,
        unit_patterns=(
            UnitPattern(open="@unit{", close="}"),
            UnitPattern(open="[", close="]"),
        ),
    )
    assert res.annotations == ()
    assert sum(1 for e in res.errors if "more than one" in e.reason) == 2


# ---------------------------------------------------------------------------
# Wrong-statement-kind + U023 (spec §8.3)
# ---------------------------------------------------------------------------


def test_assume_on_declaration_dropped_and_recorded():
    """A trailing ``!< @unit_assume{...}`` on a declaration is dropped
    (declarations don't host an RHS to suppress) and recorded for the
    U023 emitter."""
    src = "real :: x   !< @unit_assume{m/s: legacy fit}\n"
    res = scan_text(src)
    assert res.assumes == ()
    assert len(res.wrong_statement_kinds) == 1
    wsk = res.wrong_statement_kinds[0]
    assert wsk.directive_found == "@unit_assume"
    assert wsk.landed_on == "declaration"
    assert wsk.expected_directive == "@unit"


def test_affine_on_declaration_dropped_and_recorded():
    src = "real :: x   !< @unit_affine_conversion{degC -> K}\n"
    res = scan_text(src)
    assert res.affine_conversions == ()
    assert len(res.wrong_statement_kinds) == 1
    assert res.wrong_statement_kinds[0].directive_found == "@unit_affine_conversion"


def test_assume_pre_above_declaration_dropped():
    """Spec §8.3 applies to the PRE position too — a standalone
    ``!> @unit_assume{...}`` immediately above a decl is wrong-kind."""
    src = "!> @unit_assume{kg: legacy}\nreal :: m\n"
    res = scan_text(src)
    assert res.assumes == ()
    assert len(res.wrong_statement_kinds) == 1


def test_assume_on_assignment_still_captures():
    """The kind is right — the assume attaches and no U023 fires."""
    src = (
        "real :: tracer_eff, ratio\n"
        "tracer_eff = ratio   !< @unit_assume{m/s: legacy}\n"
    )
    res = scan_text(src)
    assert len(res.assumes) == 1
    assert res.wrong_statement_kinds == ()


def test_assignment_line_ranges_populated():
    """The new ScanResult field is non-empty when assignments exist
    so the multifile orphan rerouter can branch correctly."""
    src = "real :: v\nv = 1.0\n"
    res = scan_text(src)
    assert (2, 2) in res.assignment_line_ranges
