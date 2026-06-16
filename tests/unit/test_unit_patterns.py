from dimfort.config import StructuredPatternEntry, UnitPatternEntry
from dimfort.core.unit_patterns import (
    PatternMatch,
    StructuredPattern,
    UnitPattern,
    compile_structured_patterns,
    compile_unit_patterns,
    select_match,
)

# ---------------------------------------------------------------------------
# UnitPattern
# ---------------------------------------------------------------------------


def test_unit_pattern_default_at_unit_braces():
    p = UnitPattern(open="@unit{", close="}")
    text = " horizontal wind speed @unit{m/s} (model grid)"
    [m] = p.find(text)
    assert m.unit_text == "m/s"
    assert m.payload is None
    assert text[m.start : m.end] == "@unit{m/s}"


def test_unit_pattern_bracket_delimiter():
    p = UnitPattern(open="[", close="]")
    [m] = p.find("desc [m/s] tail")
    assert m.unit_text == "m/s"


def test_unit_pattern_strips_inner_whitespace():
    p = UnitPattern(open="@unit{", close="}")
    [m] = p.find("@unit{  m/s  }")
    assert m.unit_text == "m/s"


def test_unit_pattern_empty_inner_still_match():
    """An empty capture is a Match — the scanner decides on malformed."""
    p = UnitPattern(open="@unit{", close="}")
    [m] = p.find("@unit{}")
    assert m.unit_text == ""


def test_unit_pattern_unclosed_no_match():
    p = UnitPattern(open="@unit{", close="}")
    assert p.find("@unit{m/s tail") == []


def test_unit_pattern_no_open_no_match():
    p = UnitPattern(open="@unit{", close="}")
    assert p.find("plain comment") == []


def test_unit_pattern_multiple_non_overlapping():
    p = UnitPattern(open="[", close="]")
    matches = p.find("[m/s] then [kg]")
    assert [m.unit_text for m in matches] == ["m/s", "kg"]


def test_unit_pattern_empty_delimiter_no_match():
    """Defensive: an entry with empty open or close yields nothing
    rather than infinite-looping. Config-level validation should
    have rejected it earlier."""
    assert UnitPattern(open="", close="}").find("@unit{m/s}") == []
    assert UnitPattern(open="@unit{", close="").find("@unit{m/s}") == []


# ---------------------------------------------------------------------------
# StructuredPattern
# ---------------------------------------------------------------------------


def test_structured_pattern_assume_default():
    p = StructuredPattern(open="@unit_assume{", close="}", sep=":")
    [m] = p.find("@unit_assume{ m^2 : Andreas 1989 }")
    assert m.unit_text == "m^2"
    assert m.payload == "Andreas 1989"


def test_structured_pattern_affine_arrow():
    p = StructuredPattern(open="[", close="]", sep="->")
    [m] = p.find("sea-surface T conversion [degC -> K]")
    assert m.unit_text == "degC"
    assert m.payload == "K"


def test_structured_pattern_skip_when_sep_missing():
    """A pair without sep is not a structured match; scanner can
    decide separately whether to flag it."""
    p = StructuredPattern(open="[", close="]", sep=":")
    assert p.find("[m/s]") == []


def test_structured_pattern_finds_match_after_skipped_sepless_pair():
    """When the first delimiter pair lacks sep, scanning continues
    so a later well-formed pair is still found."""
    p = StructuredPattern(open="[", close="]", sep=":")
    [m] = p.find("see [m/s] but really [m^2 : empirical]")
    assert m.unit_text == "m^2"
    assert m.payload == "empirical"


def test_structured_pattern_sep_first_occurrence_splits():
    """A payload that itself contains ``sep`` keeps everything after
    the first sep as payload."""
    p = StructuredPattern(open="@unit_assume{", close="}", sep=":")
    [m] = p.find("@unit_assume{ kg/m^3 : ratio kg:kg corrected }")
    assert m.unit_text == "kg/m^3"
    assert m.payload == "ratio kg:kg corrected"


# ---------------------------------------------------------------------------
# Compilation helpers
# ---------------------------------------------------------------------------


def test_compile_unit_patterns_preserves_order():
    entries = [
        UnitPatternEntry(open="@unit{", close="}"),
        UnitPatternEntry(open="[", close="]"),
    ]
    out = compile_unit_patterns(entries)
    assert out == (
        UnitPattern(open="@unit{", close="}"),
        UnitPattern(open="[", close="]"),
    )


def test_compile_structured_patterns_preserves_order():
    entries = [
        StructuredPatternEntry(open="@unit_assume{", close="}", sep=":"),
        StructuredPatternEntry(open="[", close="]", sep=":"),
    ]
    out = compile_structured_patterns(entries)
    assert out == (
        StructuredPattern(open="@unit_assume{", close="}", sep=":"),
        StructuredPattern(open="[", close="]", sep=":"),
    )


# ---------------------------------------------------------------------------
# select_match — spec §8.1 / §8.2
# ---------------------------------------------------------------------------


def test_select_match_no_patterns_no_text_returns_none():
    assert select_match([], "text") is None
    assert select_match([UnitPattern("@unit{", "}")], "plain") is None


def test_select_match_first_listed_wins_even_if_later_in_text():
    """Spec §8.1: config-list order, not text-position order."""
    patterns = [
        UnitPattern(open="@unit{", close="}"),
        UnitPattern(open="[", close="]"),
    ]
    hit = select_match(patterns, "wind speed [m/s] @unit{kg}")
    assert hit is not None
    assert hit.pattern_index == 0
    assert hit.match.unit_text == "kg"
    # The bracket match captures different text → conflict reported.
    assert len(hit.conflicts) == 1
    conflict_idx, conflict_match = hit.conflicts[0]
    assert conflict_idx == 1
    assert conflict_match.unit_text == "m/s"


def test_select_match_identical_captures_no_conflict():
    """Spec §8.2: identical text across patterns is silent."""
    patterns = [
        UnitPattern(open="@unit{", close="}"),
        UnitPattern(open="[", close="]"),
    ]
    hit = select_match(patterns, "@unit{m/s} also [m/s]")
    assert hit is not None
    assert hit.pattern_index == 0
    assert hit.match.unit_text == "m/s"
    assert hit.conflicts == ()


def test_select_match_falls_through_when_first_pattern_unmatched():
    patterns = [
        UnitPattern(open="@unit{", close="}"),
        UnitPattern(open="[", close="]"),
    ]
    hit = select_match(patterns, "wind speed [m/s]")
    assert hit is not None
    assert hit.pattern_index == 1
    assert hit.match.unit_text == "m/s"
    assert hit.conflicts == ()


def test_select_match_structured_and_unit_mix():
    """Structured patterns can be passed alongside unit patterns;
    they obey the same first-match-wins rule."""
    patterns = [
        StructuredPattern(open="@unit_assume{", close="}", sep=":"),
        UnitPattern(open="[", close="]"),
    ]
    hit = select_match(patterns, "@unit_assume{kg:legacy} extra [m/s]")
    assert hit is not None
    assert hit.pattern_index == 0
    assert hit.match.unit_text == "kg"
    assert hit.match.payload == "legacy"


def test_pattern_match_equality():
    """Frozen dataclass — equality by value."""
    a = PatternMatch(unit_text="m/s", payload=None, start=0, end=10)
    b = PatternMatch(unit_text="m/s", payload=None, start=0, end=10)
    assert a == b


# ---------------------------------------------------------------------------
# 0.2.7 nonunit / nonunit_assume / nonunit_affine filter patterns
# ---------------------------------------------------------------------------


def test_nonunit_pattern_no_regex_matches_every_pair():
    from dimfort.core.unit_patterns import NonUnitPattern
    pat = NonUnitPattern(open="@nonunit{", close="}")
    hits = pat.find("foo @nonunit{[m]} bar")
    assert len(hits) == 1
    assert hits[0].unit_text == "[m]"
    assert hits[0].start == 4


def test_nonunit_pattern_regex_filters_inner():
    import re

    from dimfort.core.unit_patterns import NonUnitPattern
    pat = NonUnitPattern(open="(", close=")", regex=re.compile(r"^\d{4}$"))
    hits = pat.find("see (2002) and (m/s)")
    assert [m.unit_text for m in hits] == ["2002"]


def test_nonunit_pattern_regex_no_match_drops():
    import re

    from dimfort.core.unit_patterns import NonUnitPattern
    pat = NonUnitPattern(open="(", close=")", regex=re.compile(r"^\d{4}$"))
    hits = pat.find("(kg)")
    assert hits == []


def test_nonstructured_pattern_sep_none_degenerates_to_pair():
    from dimfort.core.unit_patterns import NonStructuredPattern
    pat = NonStructuredPattern(open="@unit_assume{", close="}")
    # sep=None means "drop all matching {open, close}" regardless of
    # what's inside — including when there's no `:` separator.
    hits = pat.find("foo @unit_assume{anything_at_all}")
    assert len(hits) == 1
    assert hits[0].unit_text == "anything_at_all"


def test_nonstructured_pattern_sep_required_when_set():
    from dimfort.core.unit_patterns import NonStructuredPattern
    pat = NonStructuredPattern(open="@unit_assume{", close="}", sep=":")
    # sep=":" means "drop only triples whose inner contains ':'".
    no_sep = pat.find("@unit_assume{no_separator}")
    assert no_sep == []
    with_sep = pat.find("@unit_assume{kg : legacy}")
    assert len(with_sep) == 1


def test_dead_ranges_combines_multiple_patterns_sorted():
    from dimfort.core.unit_patterns import (
        NonUnitPattern,
        dead_ranges,
    )
    patterns = (
        NonUnitPattern(open="@nonunit{", close="}"),
        NonUnitPattern(open="(see ", close=")"),
    )
    body = "(see foo) text @nonunit{bar}"
    ranges = dead_ranges(body, patterns)
    assert ranges == ((0, 9), (15, 28))


def test_overlaps_any_basic_cases():
    from dimfort.core.unit_patterns import overlaps_any
    ranges = ((10, 20), (30, 40))
    assert overlaps_any(5, 15, ranges)         # crosses left edge
    assert overlaps_any(15, 25, ranges)        # crosses right edge
    assert overlaps_any(12, 18, ranges)        # contained
    assert overlaps_any(5, 45, ranges)         # encloses
    assert not overlaps_any(0, 5, ranges)      # left of all
    assert not overlaps_any(20, 30, ranges)    # in gap (half-open: 20 == start of nothing)
    assert not overlaps_any(45, 50, ranges)    # right of all


def test_compile_nonunit_patterns_preserves_order_and_regex():
    from dimfort.config import NonUnitPatternEntry
    from dimfort.core.unit_patterns import compile_nonunit_patterns
    entries = (
        NonUnitPatternEntry(open="(", close=")", regex=r"^\d{4}$"),
        NonUnitPatternEntry(open="@nonunit{", close="}"),
    )
    compiled = compile_nonunit_patterns(entries)
    assert compiled[0].open == "("
    assert compiled[0].regex is not None
    assert compiled[0].regex.pattern == r"^\d{4}$"
    assert compiled[1].regex is None


def test_compile_nonstructured_patterns_handles_optional_sep_and_regex():

    from dimfort.config import NonStructuredPatternEntry
    from dimfort.core.unit_patterns import compile_nonstructured_patterns
    entries = (
        NonStructuredPatternEntry(open="@x{", close="}"),
        NonStructuredPatternEntry(
            open="@y{", close="}", sep=":", regex=r"^0\s*:",
        ),
    )
    compiled = compile_nonstructured_patterns(entries)
    assert compiled[0].sep is None
    assert compiled[0].regex is None
    assert compiled[1].sep == ":"
    assert compiled[1].regex.pattern == r"^0\s*:"
