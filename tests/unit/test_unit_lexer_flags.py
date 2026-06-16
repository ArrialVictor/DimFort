"""Coverage for the 0.2.7 permissive-unit-lexer rewrite-subsystem flags.

Track B.2a — four flags acting at the tokenizer level (codepoint
subs / token aliases / post-token rewrites), every flag default
OFF. Recognition-subsystem flags (Track B.2b) ship in a follow-up
PR with the 28-pair composition audit.

Spec: ``docs/design/shipped/permissive-unit-lexer.md`` §3.1, §3.6,
§3.7, §3.8 for the four landed-here flags.
"""
from __future__ import annotations

from fractions import Fraction

import pytest

from dimfort.config import UnitLexerConfig
from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.units import Exponent, UnitError, parse

# ---------------------------------------------------------------------------
# Strict default — every permissive shape rejects
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "m**2",       # allow_fortran_star_star
        "m·s",        # allow_middot_multiplication
        "m²",         # allow_unicode_superscripts
        "m^{-1}",     # allow_latex_braces
    ],
)
def test_strict_default_rejects_each_permissive_shape(expr):
    """No flags → every permissive lexer shape raises ``UnitError``.

    The default grammar exposes a single canonical exponent operator
    (``^``), no Unicode superscripts, no middot multiplication, no
    LaTeX braces. Projects opt in per shape via the matching
    ``[parser.unit_lexer]`` flag.
    """
    with pytest.raises(UnitError):
        parse(expr)


# ---------------------------------------------------------------------------
# §3.6 — allow_fortran_star_star
# ---------------------------------------------------------------------------


def test_fortran_star_star_flag_on_accepts_basic_form():
    lex = UnitLexerConfig(allow_fortran_star_star=True)
    assert parse("m**2", lexer=lex).dimension[1] == 2


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("m**2", 2),
        ("m**-1", -1),
        ("m**(2)", 2),
        ("m**(-1)", -1),
        ("m**(1/2)", Fraction(1, 2)),
    ],
)
def test_fortran_star_star_accepts_all_four_shapes(expr, expected):
    """Per §3.6: when the flag is on, ``**`` accepts the same four
    integer-exponent shapes the post-§3.0 strict ``^`` accepts."""
    lex = UnitLexerConfig(allow_fortran_star_star=True)
    assert parse(expr, lexer=lex).dimension[1] == expected


def test_fortran_star_star_precedence_matches_caret():
    """``m/s**2`` must bind ``s**2`` first under either alias."""
    lex = UnitLexerConfig(allow_fortran_star_star=True)
    a = parse("m/s**2", lexer=lex).dimension
    b = parse("m/s^2").dimension
    assert a == b


def test_fortran_star_star_composes_with_symbolic_exponents():
    """``Pa**kappa`` and ``Pa^kappa`` produce the same Exponent
    when the flag is on."""
    lex = UnitLexerConfig(allow_fortran_star_star=True)
    a = parse("Pa**kappa", lexer=lex).dimension
    b = parse("Pa^kappa").dimension
    assert a == b


# ---------------------------------------------------------------------------
# §3.7 — allow_unicode_superscripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("m²", 2),
        ("m³", 3),
        ("m⁻¹", -1),
        ("m⁻³", -3),
        ("m⁺²", 2),
    ],
)
def test_unicode_superscripts_accepts_digits_and_signs(expr, expected):
    lex = UnitLexerConfig(allow_unicode_superscripts=True)
    assert parse(expr, lexer=lex).dimension[1] == expected


def test_unicode_superscripts_run_attaches_to_identifier():
    """A superscript run after an identifier implies ``^`` between
    them: ``m⁻¹`` rewrites to ``m^-1``."""
    lex = UnitLexerConfig(allow_unicode_superscripts=True)
    assert parse("m⁻¹", lexer=lex).dimension == parse("m^-1").dimension


def test_unicode_superscripts_off_rejects_even_simple_form():
    with pytest.raises(UnitError):
        parse("m²")


# ---------------------------------------------------------------------------
# §3.8 — allow_middot_multiplication
# ---------------------------------------------------------------------------


def test_middot_multiplication_basic_pair():
    """``m·s`` is the SI typographical convention for ``m*s``."""
    lex = UnitLexerConfig(allow_middot_multiplication=True)
    assert parse("m·s", lexer=lex).dimension == parse("m*s").dimension


def test_middot_multiplication_chained():
    lex = UnitLexerConfig(allow_middot_multiplication=True)
    assert parse("kg·m·s", lexer=lex).dimension == parse("kg*m*s").dimension


def test_middot_multiplication_off_rejects():
    with pytest.raises(UnitError):
        parse("m·s")


# ---------------------------------------------------------------------------
# §3.1 — allow_latex_braces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,canonical",
    [
        ("m^{-1}",     "m^-1"),
        ("kg^{2}",     "kg^2"),
        ("m^{1/2}",    "m^(1/2)"),
        ("Pa^{kappa}", "Pa^kappa"),
        ("Pa^{2*kappa-1/3}", "Pa^(2*kappa-1/3)"),
    ],
)
def test_latex_braces_rewrites_to_canonical(expr, canonical):
    """``^{<content>}`` becomes ``^(<content>)`` before tokenization.

    The strict-grammar post-§3.0 paren widening makes every accepted
    inner content parse uniformly under parens — single rewrite rule
    handles ints, signed ints, rationals, identifiers, and linear
    forms together.
    """
    lex = UnitLexerConfig(allow_latex_braces=True)
    assert parse(expr, lexer=lex).dimension == parse(canonical).dimension


def test_latex_braces_off_rejects():
    """Without the flag, ``{`` / ``}`` are unrecognized characters."""
    with pytest.raises(UnitError):
        parse("m^{-1}")


def test_latex_braces_unmatched_open_falls_through_to_tokenizer():
    """Unmatched ``^{`` (no closing brace) is left untouched by the
    rewrite pass and surfaces as a tokenizer error on the stray
    ``{``."""
    lex = UnitLexerConfig(allow_latex_braces=True)
    with pytest.raises(UnitError):
        parse("m^{2", lexer=lex)


# ---------------------------------------------------------------------------
# Composition within the rewrite subsystem
# ---------------------------------------------------------------------------


def test_compose_middot_and_unicode_superscripts():
    """``m·s⁻¹`` exercises both flags simultaneously — superscripts
    rewrite to ``^-1`` then middot rewrites to ``*``."""
    lex = UnitLexerConfig(
        allow_middot_multiplication=True,
        allow_unicode_superscripts=True,
    )
    assert parse("m·s⁻¹", lexer=lex).dimension == parse("m*s^-1").dimension


def test_compose_starstar_and_latex_braces():
    """``m**{2}`` exercises ``**`` alias + brace rewrite together —
    the operator alias normalises first, then the brace rewrite
    sees the canonical ``^`` regardless of which the author wrote."""
    lex = UnitLexerConfig(
        allow_fortran_star_star=True,
        allow_latex_braces=True,
    )
    assert parse("m**{2}", lexer=lex).dimension == parse("m^2").dimension


def test_compose_all_four_rewrite_subsystem_flags():
    """All four rewrite-subsystem flags on simultaneously — sanity
    that the pipeline order (unicode → middot → braces → POW) is
    confluent on shapes that exercise multiple stages."""
    lex = UnitLexerConfig(
        allow_unicode_superscripts=True,
        allow_middot_multiplication=True,
        allow_fortran_star_star=True,
        allow_latex_braces=True,
    )
    # m·s⁻¹ should still parse the same when all four flags on.
    assert parse("m·s⁻¹", lexer=lex).dimension == parse("m*s^-1").dimension
    # ** alias still works.
    assert parse("Pa**(2)", lexer=lex).dimension == parse("Pa^2").dimension
    # Brace form still works.
    assert parse("J^{-1}", lexer=lex).dimension == parse("J^-1").dimension


# ---------------------------------------------------------------------------
# Composition with the symbolic-exponent surface (B.1 widening)
# ---------------------------------------------------------------------------


def test_latex_braces_with_symbolic_exponent():
    """``Pa^{kappa}`` rewrites to ``Pa^(kappa)`` — accepted by the
    post-B.1 paren'd-identifier exponent grammar."""
    lex = UnitLexerConfig(allow_latex_braces=True)
    assert parse("Pa^{kappa}", lexer=lex).dimension == parse("Pa^kappa").dimension


def test_starstar_with_symbolic_linear_form():
    lex = UnitLexerConfig(allow_fortran_star_star=True)
    assert (
        parse("Pa**(2*kappa-1/3)", lexer=lex).dimension
        == parse("Pa^(2*kappa-1/3)").dimension
    )


# ---------------------------------------------------------------------------
# Error message UX
# ---------------------------------------------------------------------------


def test_strict_starstar_error_mentions_flag_name():
    """The rejection message for ``**`` under strict default
    references the config key, giving the migrator a one-line fix."""
    try:
        parse("m**2")
    except UnitError as exc:
        assert "allow_fortran_star_star" in str(exc)
    else:
        pytest.fail("expected UnitError")


# ---------------------------------------------------------------------------
# §3.2 — allow_dot_multiplication (recognition subsystem, B.2b)
# ---------------------------------------------------------------------------


def test_dot_multiplication_basic_pair():
    lex = UnitLexerConfig(allow_dot_multiplication=True)
    assert parse("J.kg", lexer=lex).dimension == parse("J*kg").dimension


def test_dot_multiplication_does_not_eat_decimal_literals():
    """``.`` between digits must NOT become ``*`` — decimal literals
    like ``0.5`` and ``1.380658E-23`` need to survive intact for
    future numeric-factor support."""
    from dimfort.core.units import _DOT_MULT_RE
    assert _DOT_MULT_RE.sub("*", "0.5") == "0.5"
    assert _DOT_MULT_RE.sub("*", "1.380658E-23") == "1.380658E-23"
    # Sanity: between letters the rule fires.
    assert _DOT_MULT_RE.sub("*", "J.kg") == "J*kg"


def test_dot_multiplication_off_rejects():
    with pytest.raises(UnitError):
        parse("J.kg")


# ---------------------------------------------------------------------------
# §3.3 — allow_implicit_product (recognition subsystem)
# ---------------------------------------------------------------------------


def test_implicit_product_basic_pair():
    lex = UnitLexerConfig(allow_implicit_product=True)
    assert parse("kg m", lexer=lex).dimension == parse("kg*m").dimension


def test_implicit_product_preserves_ms_as_millisecond():
    """``ms`` (no whitespace) stays millisecond regardless of the
    flag — the rule fires on whitespace only, not on identifier
    adjacency."""
    lex = UnitLexerConfig(allow_implicit_product=True)
    assert parse("ms", lexer=lex).dimension == parse("ms").dimension


def test_implicit_product_off_rejects_whitespace_pair():
    with pytest.raises(UnitError):
        parse("kg m")


# ---------------------------------------------------------------------------
# §3.4 — allow_integer_suffix_exp (recognition subsystem)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,canonical",
    [
        ("s-1", "s^-1"),
        ("s+1", "s^+1"),
        ("m-3", "m^-3"),
    ],
)
def test_integer_suffix_exp_rewrites_to_caret(expr, canonical):
    lex = UnitLexerConfig(allow_integer_suffix_exp=True)
    assert parse(expr, lexer=lex).dimension == parse(canonical).dimension


def test_integer_suffix_exp_with_implicit_product_handles_udunits_shape():
    """The udunits2-canonical shape: ``kg m-3``, ``W m-2 K-1``."""
    lex = UnitLexerConfig(
        allow_implicit_product=True,
        allow_integer_suffix_exp=True,
    )
    assert parse("kg m-3", lexer=lex).dimension == parse("kg*m^-3").dimension
    assert (
        parse("W m-2 K-1", lexer=lex).dimension
        == parse("W*m^-2*K^-1").dimension
    )


def test_integer_suffix_exp_off_rejects():
    with pytest.raises(UnitError):
        parse("s-1")


def test_integer_suffix_exp_does_not_touch_caret_canonical():
    """``m^-3`` already has the canonical ``^`` — the rewrite must
    not double-apply to produce ``m^^-3``."""
    lex = UnitLexerConfig(allow_integer_suffix_exp=True)
    assert parse("m^-3", lexer=lex).dimension == parse("m^-3").dimension


# ---------------------------------------------------------------------------
# §3.5 — allow_bare_digit_exp (recognition subsystem, HIGH FP)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,canonical",
    [
        ("m2", "m^2"),
        ("m3", "m^3"),
        ("kg2", "kg^2"),
        ("mol2", "mol^2"),
        ("hPa2", "hPa^2"),
    ],
)
def test_bare_digit_exp_rewrites_known_unit_to_caret(expr, canonical):
    lex = UnitLexerConfig(allow_bare_digit_exp=True)
    assert parse(expr, lexer=lex).dimension == parse(canonical).dimension


def test_bare_digit_exp_rejects_unknown_identifier():
    """``i2`` and ``t2m`` are NOT in the 14-symbol guard list — the
    rule does not fire on them. ``i`` / ``t`` parse separately and
    the resulting ``i2`` / ``t2m`` is unknown."""
    lex = UnitLexerConfig(allow_bare_digit_exp=True)
    with pytest.raises(UnitError):
        parse("i2", lexer=lex)
    with pytest.raises(UnitError):
        parse("t2m", lexer=lex)


def test_bare_digit_exp_rejects_digits_above_nine():
    """Design §3.5 strict rule: digits ≥10 are NOT eligible (only
    4 real corpus sites, ~1000+ FPs). ``m10`` stays unparseable
    even with the flag on."""
    lex = UnitLexerConfig(allow_bare_digit_exp=True)
    with pytest.raises(UnitError):
        parse("m10", lexer=lex)


def test_bare_digit_exp_composes_with_implicit_product():
    """``kg m2 s-3`` — the udunits2-shape with bare-digit + signed
    suffix + implicit product all enabled together."""
    lex = UnitLexerConfig(
        allow_implicit_product=True,
        allow_integer_suffix_exp=True,
        allow_bare_digit_exp=True,
    )
    assert (
        parse("kg m2 s-3", lexer=lex).dimension
        == parse("kg*m^2*s^-3").dimension
    )


def test_bare_digit_exp_off_rejects():
    with pytest.raises(UnitError):
        parse("m2")


# ---------------------------------------------------------------------------
# 28-pair composition audit (design §4.4)
# ---------------------------------------------------------------------------
#
# Each flag pair gets a representative input that exercises both,
# plus a canonical equivalent the strict baseline accepts. The
# composition contract (design §4.1) requires the parse output to
# match the canonical form regardless of which order the flags'
# rewrite passes ran in.

_ALL_FLAGS = [
    "allow_unicode_superscripts",
    "allow_middot_multiplication",
    "allow_fortran_star_star",
    "allow_latex_braces",
    "allow_dot_multiplication",
    "allow_implicit_product",
    "allow_integer_suffix_exp",
    "allow_bare_digit_exp",
]


# Map from frozenset({flag_a, flag_b}) → (permissive_input, canonical)
# Selected to genuinely exercise both flags simultaneously; pairs
# without a natural combined input use single-flag inputs to
# confirm the unused flag doesn't perturb the other.
_PAIRWISE_INPUTS: dict[frozenset[str], tuple[str, str]] = {
    # 1. unicode_superscripts × middot — canonical SI typeset
    frozenset({"allow_unicode_superscripts",
               "allow_middot_multiplication"}): ("m·s⁻¹", "m*s^-1"),
    # 2. unicode_superscripts × fortran_star_star
    frozenset({"allow_unicode_superscripts",
               "allow_fortran_star_star"}): ("m²", "m^2"),
    # 3. unicode_superscripts × latex_braces
    frozenset({"allow_unicode_superscripts",
               "allow_latex_braces"}): ("m^{2}", "m^2"),
    # 4. unicode_superscripts × dot_mult
    frozenset({"allow_unicode_superscripts",
               "allow_dot_multiplication"}): ("J.kg⁻¹", "J*kg^-1"),
    # 5. unicode_superscripts × implicit_product
    frozenset({"allow_unicode_superscripts",
               "allow_implicit_product"}): ("kg m⁻³", "kg*m^-3"),
    # 6. unicode_superscripts × integer_suffix_exp
    frozenset({"allow_unicode_superscripts",
               "allow_integer_suffix_exp"}): ("m⁻³", "m^-3"),
    # 7. unicode_superscripts × bare_digit_exp
    frozenset({"allow_unicode_superscripts",
               "allow_bare_digit_exp"}): ("m²", "m^2"),
    # 8. middot × fortran_star_star
    frozenset({"allow_middot_multiplication",
               "allow_fortran_star_star"}): ("m·s**-1", "m*s^-1"),
    # 9. middot × latex_braces
    frozenset({"allow_middot_multiplication",
               "allow_latex_braces"}): ("m·s^{-1}", "m*s^-1"),
    # 10. middot × dot_mult — pair-test only (both add product
    # operators, semantically same target). Use middot-only input.
    frozenset({"allow_middot_multiplication",
               "allow_dot_multiplication"}): ("J·kg", "J*kg"),
    # 11. middot × implicit_product
    frozenset({"allow_middot_multiplication",
               "allow_implicit_product"}): ("kg·m", "kg*m"),
    # 12. middot × integer_suffix_exp
    frozenset({"allow_middot_multiplication",
               "allow_integer_suffix_exp"}): ("m·s-1", "m*s^-1"),
    # 13. middot × bare_digit_exp
    frozenset({"allow_middot_multiplication",
               "allow_bare_digit_exp"}): ("kg·m2", "kg*m^2"),
    # 14. star_star × latex_braces
    frozenset({"allow_fortran_star_star",
               "allow_latex_braces"}): ("m**{2}", "m^2"),
    # 15. star_star × dot_mult
    frozenset({"allow_fortran_star_star",
               "allow_dot_multiplication"}): ("J.kg**-1", "J*kg^-1"),
    # 16. star_star × implicit_product
    frozenset({"allow_fortran_star_star",
               "allow_implicit_product"}): ("kg m**-3", "kg*m^-3"),
    # 17. star_star × integer_suffix_exp — semantically same
    # surface; the int-suffix flag has no effect on ``**`` shapes.
    frozenset({"allow_fortran_star_star",
               "allow_integer_suffix_exp"}): ("m**-3", "m^-3"),
    # 18. star_star × bare_digit_exp — bare-digit has no ``**``
    # surface; pair-test single-flag exercise.
    frozenset({"allow_fortran_star_star",
               "allow_bare_digit_exp"}): ("m**2", "m^2"),
    # 19. latex_braces × dot_mult
    frozenset({"allow_latex_braces",
               "allow_dot_multiplication"}): ("J.kg^{-1}", "J*kg^-1"),
    # 20. latex_braces × implicit_product
    frozenset({"allow_latex_braces",
               "allow_implicit_product"}): ("kg m^{-3}", "kg*m^-3"),
    # 21. latex_braces × integer_suffix_exp — disjoint shapes
    frozenset({"allow_latex_braces",
               "allow_integer_suffix_exp"}): ("m^{-3}", "m^-3"),
    # 22. latex_braces × bare_digit_exp — disjoint shapes
    frozenset({"allow_latex_braces",
               "allow_bare_digit_exp"}): ("m^{2}", "m^2"),
    # 23. dot_mult × implicit_product
    frozenset({"allow_dot_multiplication",
               "allow_implicit_product"}): ("J.kg m", "J*kg*m"),
    # 24. dot_mult × integer_suffix_exp
    frozenset({"allow_dot_multiplication",
               "allow_integer_suffix_exp"}): ("J.kg-1", "J*kg^-1"),
    # 25. dot_mult × bare_digit_exp
    frozenset({"allow_dot_multiplication",
               "allow_bare_digit_exp"}): ("kg.m2", "kg*m^2"),
    # 26. implicit_product × integer_suffix_exp — the canonical
    # udunits2 shape (highest empirical value of any pair).
    frozenset({"allow_implicit_product",
               "allow_integer_suffix_exp"}): ("kg m-3", "kg*m^-3"),
    # 27. implicit_product × bare_digit_exp
    frozenset({"allow_implicit_product",
               "allow_bare_digit_exp"}): ("kg m2", "kg*m^2"),
    # 28. integer_suffix_exp × bare_digit_exp — use ``*`` between
    # the two halves so the canonical canonical-form parse stays
    # under the strict default (no implicit_product needed).
    frozenset({"allow_integer_suffix_exp",
               "allow_bare_digit_exp"}): ("m2*s-3", "m^2*s^-3"),
}


@pytest.mark.parametrize(
    "flag_a,flag_b",
    [
        (a, b)
        for i, a in enumerate(_ALL_FLAGS)
        for b in _ALL_FLAGS[i + 1:]
    ],
)
def test_pairwise_composition_matches_canonical(flag_a, flag_b):
    """28-pair composition audit (design §4.4).

    Every pair of flags must produce a parse result equal to the
    canonical-form parse when both are enabled simultaneously. The
    inputs are selected per the table in this module; pair #28 plus
    a few others (implicit_product × integer_suffix_exp, dot_mult ×
    integer_suffix_exp) exercise the highest-empirical-volume
    udunits2 shapes.
    """
    pair = frozenset({flag_a, flag_b})
    permissive, canonical = _PAIRWISE_INPUTS[pair]
    # Some pairs require implicit_product in the canonical form
    # too (e.g. ``m2 s-3`` needs implicit_product on both sides
    # for the canonical reference). Enable both target flags on the
    # canonical parse too.
    flags = {flag_a: True, flag_b: True}
    # The canonical reference uses ``^`` / ``*`` only, so it
    # parses under strict default. But pair #28 has a whitespace
    # in the canonical too (``m^2*s^-3`` has no whitespace, but the
    # input ``m2 s-3`` does). The canonical-side parse uses strict
    # default.
    lex = UnitLexerConfig(**flags)
    permissive_result = parse(permissive, lexer=lex)
    canonical_result = parse(canonical)
    assert permissive_result.dimension == canonical_result.dimension, (
        f"pair {sorted(pair)}: {permissive!r} (under flags) should match "
        f"{canonical!r} (canonical), but dimensions differ"
    )


# ---------------------------------------------------------------------------
# Suppress unused-import warning for Exponent (referenced via .dimension)
# ---------------------------------------------------------------------------

_ = Exponent  # keep import for downstream test additions
