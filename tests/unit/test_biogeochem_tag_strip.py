"""Coverage for the 0.2.7 biogeochem-tag-strip preprocessor (Track B.3).

The strip turns ``mol(C)/m^2(canopy)`` into ``mol/m^2`` — a lossy
pre-tokenization rewrite that discards species + spatial-domain
metadata before the lexer sees the unit string. Context-anchored
(``(`` must follow an identifier-with-optional-exponent), so math
grouping (``(m*s)``), citation parens (``(see X)``) and similar
shapes survive.

Spec: ``docs/design/shipped/permissive-unit-lexer.md`` §3.9 (the
sibling pre-processor option to the 8 lexer flags). Config lives
under ``[parser.unit_preprocess]`` because this is a pre-pass, not
a token-recognition rule — keeps the 8-flag lexer composition
contract clean.
"""
from __future__ import annotations

import pytest

from dimfort.config import UnitLexerConfig, UnitPreprocessConfig
from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.units import (
    UnitError,
    _apply_biogeochem_tag_strip,
    parse,
)

# ---------------------------------------------------------------------------
# String-level helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expr,expected,desc",
    [
        ("mol(C)/m^2(canopy)", "mol/m^2", "classic tracer-tag shape"),
        ("mol(C)", "mol", "simple single tag"),
        ("kg(N)/m^2/s", "kg/m^2/s", "kg+N tag"),
        ("m^-2(canopy)", "m^-2", "caret signed-int suffix preserved"),
        ("m^2(canopy)", "m^2", "caret unsigned-int suffix preserved"),
        ("m**-2(canopy)", "m**-2", "starstar suffix preserved"),
        ("m-3(canopy)", "m-3", "bare-int suffix preserved"),
        ("(m*s)", "(m*s)", "math grouping — preserved"),
        ("(see Schmidt)", "(see Schmidt)", "citation parens — preserved"),
        ("(2002)", "(2002)", "year-only parens — preserved"),
        ("1/m(canopy)", "1/m", "tag after digit-slash boundary"),
        ("mol(C(stable))", "mol", "nested tag — collapses to outermost"),
        ("Pa", "Pa", "no parens — unchanged"),
        ("Pa^kappa", "Pa^kappa", "symbolic exponent without tag — unchanged"),
    ],
)
def test_strip_string_level(expr, expected, desc):
    """The string-level helper applies context-anchored substitution
    and iterates to a fixed point for nested tags."""
    assert _apply_biogeochem_tag_strip(expr) == expected, desc


def test_strip_with_exceptions_preserves_listed_tags():
    """``biogeochem_tag_exceptions=("K",)`` keeps ``(K)`` tags
    intact while stripping every other tag. Forward-looking knob
    for the documented Kelvin-vs-potassium ambiguity case."""
    assert _apply_biogeochem_tag_strip("mol(K)", ("K",)) == "mol(K)"
    assert _apply_biogeochem_tag_strip("mol(C)", ("K",)) == "mol"
    # Mixed input — only the C tag strips, K survives.
    assert (
        _apply_biogeochem_tag_strip("mol(C)/m^2(K)", ("K",))
        == "mol/m^2(K)"
    )


def test_strip_off_does_not_touch_input():
    """Default empty config (the parse-time fallback) leaves the
    input unchanged — the helper is only invoked when the flag is
    on."""
    expr = "mol(C)/m^2(canopy)"
    # Direct call still strips (the helper has no internal flag);
    # this test verifies the helper is what we expect, and the
    # parse() / _tokenize() path is what gates it (covered below).
    assert _apply_biogeochem_tag_strip(expr) != expr


# ---------------------------------------------------------------------------
# End-to-end via parse()
# ---------------------------------------------------------------------------


def test_strip_off_rejects_unknown_tag():
    """Without the flag, ``mol(C)`` reaches the lexer as-is and
    fails to parse (``C`` is not a valid unit factor inside
    parens after ``mol``)."""
    with pytest.raises(UnitError):
        parse("mol(C)")


def test_strip_on_parses_biogeochem_canonical():
    """With the flag on, ``mol(C)/m^2(canopy)`` strips to
    ``mol/m^2`` and parses cleanly."""
    cfg = UnitPreprocessConfig(strip_biogeochem_tags=True)
    expected = parse("mol/m^2")
    assert parse("mol(C)/m^2(canopy)", preprocess=cfg).dimension == expected.dimension


def test_strip_on_parses_signed_suffix_with_tag():
    """``m^-2(canopy)`` strips to ``m^-2``."""
    cfg = UnitPreprocessConfig(strip_biogeochem_tags=True)
    expected = parse("m^-2")
    assert parse("m^-2(canopy)", preprocess=cfg).dimension == expected.dimension


def test_strip_does_not_eat_math_grouping():
    """``(kg*m)^2`` is math grouping — ``(`` not preceded by an
    identifier. Strip must NOT touch it."""
    cfg = UnitPreprocessConfig(strip_biogeochem_tags=True)
    assert parse("(kg*m)^2", preprocess=cfg).dimension == parse("(kg*m)^2").dimension


def test_strip_log_wrap_interaction_documented():
    """Documents the known LOG/EXP-wrapper interaction with the
    strip: ``LOG(Pa)`` is a unit wrapper but matches the strip's
    identifier-then-parens pattern, so with strip ON the parens
    content is treated as a tag and discarded. Projects mixing
    LOG/EXP wrappers with strip should add the wrapper argument
    to ``biogeochem_tag_exceptions`` if this bites. The strip's
    documented use case is biogeochem-heavy codes that don't use
    LOG/EXP wrappers in unit annotations; the cleaner fix
    (suppress the strip inside LOG/EXP) is a follow-up if real
    demand surfaces. Test asserts the current behaviour so a
    future change here is visible."""
    cfg = UnitPreprocessConfig(strip_biogeochem_tags=True)
    # With strip on and no exceptions: LOG(Pa) collapses to LOG
    # at the string level, then fails to parse because LOG is not
    # a unit identifier on its own.
    with pytest.raises(UnitError):
        parse("LOG(Pa)", preprocess=cfg)


def test_strip_with_exceptions_preserves_log_arg(tmp_path):
    """With the K exception, ``LOG(K)`` survives the strip and
    parses as the log-wrapped Kelvin unit (rather than collapsing
    to bare ``LOG``)."""
    from dimfort.core.units import LogWrap
    cfg = UnitPreprocessConfig(
        strip_biogeochem_tags=True,
        biogeochem_tag_exceptions=("K",),
    )
    got = parse("LOG(K)", preprocess=cfg)
    assert isinstance(got, LogWrap)
    expected = parse("LOG(K)")
    assert got == expected


def test_strip_composes_with_lexer_flags():
    """``mol(C)·m^-2(canopy)·s^-1`` with strip + middot enabled —
    strip runs FIRST (per pipeline order in `_tokenize`), then
    middot rewrites ``·`` → ``*``."""
    cfg_pre = UnitPreprocessConfig(strip_biogeochem_tags=True)
    cfg_lex = UnitLexerConfig(allow_middot_multiplication=True)
    expected = parse("mol*m^-2*s^-1")
    got = parse(
        "mol(C)·m^-2(canopy)·s^-1",
        lexer=cfg_lex, preprocess=cfg_pre,
    )
    assert got.dimension == expected.dimension


def test_strip_runs_before_lexer_flags_in_pipeline():
    """Verify the pipeline order: preprocess (strip) runs before
    the lexer flags. ``kg(N) m-3`` with both strip and
    implicit_product + integer_suffix_exp enabled becomes:
    1. strip: ``kg m-3`` (parens content discarded)
    2. integer_suffix_exp: ``kg m^-3``
    3. implicit_product: ``kg*m^-3``."""
    cfg_pre = UnitPreprocessConfig(strip_biogeochem_tags=True)
    cfg_lex = UnitLexerConfig(
        allow_implicit_product=True,
        allow_integer_suffix_exp=True,
    )
    expected = parse("kg*m^-3")
    got = parse("kg(N) m-3", lexer=cfg_lex, preprocess=cfg_pre)
    assert got.dimension == expected.dimension


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


CONFIG_FILENAME = "dimfort.toml"


def test_unit_preprocess_default_when_unset(tmp_path):
    """No section → strip is off, exceptions empty."""
    from dimfort.config import load_config
    (tmp_path / CONFIG_FILENAME).write_text("[parser]\n")
    cfg = load_config(tmp_path)
    assert cfg.unit_preprocess.strip_biogeochem_tags is False
    assert cfg.unit_preprocess.biogeochem_tag_exceptions == ()


def test_unit_preprocess_explicit_settings(tmp_path):
    from dimfort.config import load_config
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_preprocess]
strip_biogeochem_tags = true
biogeochem_tag_exceptions = ["K", "Pa"]
""")
    cfg = load_config(tmp_path)
    assert cfg.unit_preprocess.strip_biogeochem_tags is True
    assert cfg.unit_preprocess.biogeochem_tag_exceptions == ("K", "Pa")


def test_unit_preprocess_non_boolean_strip_warns_and_defaults(tmp_path, caplog):
    """Bad strip value → warn + default to False."""
    import logging

    from dimfort.config import load_config
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_preprocess]
strip_biogeochem_tags = "yes"
""")
    with caplog.at_level(logging.ERROR, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_preprocess.strip_biogeochem_tags is False
    assert any("must be a boolean" in r.message for r in caplog.records)


def test_unit_preprocess_unknown_key_warns(tmp_path, caplog):
    """Forward-compat: unknown key warns + ignored."""
    import logging

    from dimfort.config import load_config
    (tmp_path / CONFIG_FILENAME).write_text("""
[parser.unit_preprocess]
strip_biogeochem_tags = true
some_future_knob = "maybe"
""")
    with caplog.at_level(logging.WARNING, logger="dimfort.config"):
        cfg = load_config(tmp_path)
    assert cfg.unit_preprocess.strip_biogeochem_tags is True
    assert any(
        "some_future_knob" in r.message for r in caplog.records
    )
