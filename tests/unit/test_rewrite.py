"""Tests for the U002 rewrite detector (spec §12)."""
from dimfort.core import unit_config  # noqa: F401  populate DEFAULT_TABLE
from dimfort.core.rewrite import suggest_rewrite


def test_digit_suffix_to_caret_m2():
    assert suggest_rewrite("m2") == "m^2"


def test_digit_suffix_to_caret_inside_compound():
    assert suggest_rewrite("kg/m3") == "kg/m^3"


def test_digit_suffix_to_caret_multiple_matches():
    assert suggest_rewrite("m2/s2") == "m^2/s^2"


def test_no_rewrite_when_already_well_formed():
    """``m^2`` is already parseable — no suggestion."""
    assert suggest_rewrite("m^2") is None


def test_no_rewrite_when_input_parses():
    """``m/s`` parses cleanly today; suggestion would be redundant."""
    assert suggest_rewrite("m/s") is None


def test_no_rewrite_when_transformed_still_unparseable():
    """``zz9`` → ``zz^9`` but ``zz`` isn't a known unit symbol → no
    suggestion."""
    assert suggest_rewrite("zz9") is None


def test_idempotent_pipeline():
    """Applying the pipeline twice yields the same result."""
    once = suggest_rewrite("m2")
    assert once == "m^2"
    # Re-running on the suggestion produces no further change.
    assert suggest_rewrite(once) is None


def test_empty_input_returns_none():
    assert suggest_rewrite("") is None
