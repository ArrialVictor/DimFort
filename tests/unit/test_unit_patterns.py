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
