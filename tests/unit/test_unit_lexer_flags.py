"""Coverage for the 0.2.7 permissive-unit-lexer rewrite-subsystem flags.

Track B.2a — four flags acting at the tokenizer level (codepoint
subs / token aliases / post-token rewrites), every flag default
OFF. Recognition-subsystem flags (Track B.2b) ship in a follow-up
PR with the 28-pair composition audit.

Spec: ``docs/design/future/permissive-unit-lexer.md`` §3.1, §3.6,
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
# Suppress unused-import warning for Exponent (referenced via .dimension)
# ---------------------------------------------------------------------------

_ = Exponent  # keep import for downstream test additions
