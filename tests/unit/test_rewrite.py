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


# ---------------------------------------------------------------------------
# Layer 3a — flag-paired rewrite rules
# (design `permissive-unit-lexer.md` §3, handover Track C Layer 3a)
# ---------------------------------------------------------------------------


def test_layer_3a_unicode_superscripts_to_caret():
    """``m⁻¹`` → ``m^-1`` when ``allow_unicode_superscripts`` is OFF."""
    assert suggest_rewrite("m⁻¹") == "m^-1"


def test_layer_3a_middot_to_star():
    """``m·s`` → ``m*s`` when ``allow_middot_multiplication`` is OFF."""
    assert suggest_rewrite("m·s") == "m*s"


def test_layer_3a_star_star_to_caret():
    """``m**2`` → ``m^2`` when ``allow_fortran_star_star`` is OFF."""
    assert suggest_rewrite("m**2") == "m^2"


def test_layer_3a_latex_braces_to_parens():
    """``m^{-1}`` → ``m^(-1)`` when ``allow_latex_braces`` is OFF."""
    assert suggest_rewrite("m^{-1}") == "m^(-1)"


def test_layer_3a_integer_suffix_exp_to_caret():
    """``s-1`` → ``s^-1`` when ``allow_integer_suffix_exp`` is OFF.

    The 14-symbol guard means the rule only fires on known units;
    the post-rewrite parse confirms it's a valid suggestion.
    """
    assert suggest_rewrite("s-1") == "s^-1"


def test_layer_3a_dot_multiplication_to_star():
    """``J.kg`` → ``J*kg`` when ``allow_dot_multiplication`` is OFF."""
    assert suggest_rewrite("J.kg") == "J*kg"


def test_layer_3a_implicit_product_to_star():
    """``kg m`` → ``kg*m`` when ``allow_implicit_product`` is OFF."""
    assert suggest_rewrite("kg m") == "kg*m"


def test_layer_3a_combined_latex_dot():
    """``J.kg^{-1}`` exercises braces + dot-mult in one pipeline pass.

    Pipeline order (design §4.3): ``**`` alias → brace rewrite →
    integer-suffix → dot-mult → implicit-product. The brace rewrite
    runs before dot-mult so the final canonical is ``J*kg^(-1)``.
    """
    assert suggest_rewrite("J.kg^{-1}") == "J*kg^(-1)"


def test_layer_3a_combined_implicit_product_int_suffix():
    """udunits2-canonical ``kg m-3`` exercises implicit-product +
    integer-suffix together. The integer-suffix rule fires before
    the implicit-product rule so the final form is ``kg*m^-3``."""
    assert suggest_rewrite("kg m-3") == "kg*m^-3"


def test_layer_3a_unicode_plus_middot():
    """``m·s⁻¹`` exercises unicode superscripts + middot together."""
    assert suggest_rewrite("m·s⁻¹") == "m*s^-1"


def test_layer_3a_star_star_with_braces():
    """``m**{2}`` exercises ``**`` alias + brace rewrite together —
    the ``**`` normalises to ``^`` first, then the brace rewrite
    converts ``^{2}`` to ``^(2)``."""
    assert suggest_rewrite("m**{2}") == "m^(2)"


def test_layer_3a_integer_suffix_preserves_symbolic_linear_form():
    """Regression for the 14-symbol guard. ``kappa-1`` is NOT a
    unit-prefix-plus-signed-int — the rule must not fire on
    arbitrary letters inside a symbolic exponent. The pre-existing
    digit-suffix rule also leaves it alone."""
    # ``kappa-1`` itself doesn't parse as a unit (``kappa`` isn't a
    # known unit identifier), so the suggestion path returns None.
    # The point of this test is to verify the rule does not silently
    # produce ``kappa^-1``.
    assert suggest_rewrite("kappa-1") is None


def test_layer_3a_no_rewrite_for_already_canonical():
    """Canonical ``m^-1`` already parses — no suggestion."""
    assert suggest_rewrite("m^-1") is None


def test_zero_division_in_candidate_returns_none():
    """A candidate that triggers ``ZeroDivisionError`` during parse
    (e.g. ``m^(2/0)``) must not escape — the suggester is best-effort
    and any failure should resolve to "no useful suggestion"."""
    # The pipeline converts ``m2`` to ``m^2``; we craft a candidate that
    # only exercises the parse-failure path so we don't depend on which
    # rule produced it.
    assert suggest_rewrite("kg2/m^(2/0)") is None
