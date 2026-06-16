"""End-to-end filter semantics for ``nonunit`` / ``nonunit_assume`` /
``nonunit_affine`` over :func:`scan_text`.

These exercise the *integrated* drop-zone behaviour — that captures
from the unit / unit_assume / unit_affine extractors are silently
dropped when a matching nonSTRUCT pattern covers their span, and that
per-family isolation holds (``nonunit`` does NOT filter assume / affine
captures, etc.).

Spec: ``docs/design/shipped/unit-comment-markers.md``.
"""
from __future__ import annotations

from dimfort.core.annotations import scan_text
from dimfort.core.unit_patterns import (
    DEFAULT_AFFINE_PATTERNS,
    DEFAULT_ASSUME_PATTERNS,
    DEFAULT_NONUNIT_AFFINE_PATTERNS,
    DEFAULT_NONUNIT_ASSUME_PATTERNS,
    DEFAULT_NONUNIT_PATTERNS,
    DEFAULT_UNIT_PATTERNS,
    NonStructuredPattern,
    NonUnitPattern,
    StructuredPattern,
    UnitPattern,
    compile_nonunit_patterns,
)


def _bracket_unit_patterns():
    return (UnitPattern(open="[", close="]"),)


def _paren_unit_patterns():
    return (UnitPattern(open="(", close=")"),)


# ---------------------------------------------------------------------------
# Default nonunit shipped patterns drop the year / citation / per-site shapes
# ---------------------------------------------------------------------------


def test_default_nonunit_drops_year_only_paren_when_unit_uses_parens():
    """Shipped year regex drops ``(2002)`` when the user configures
    parens as a unit delimiter (common with relax-mode-style configs)."""
    src = (
        "subroutine s\n"
        "  real :: v   ! wind speed (2002)\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=_paren_unit_patterns(),
        nonunit_patterns=DEFAULT_NONUNIT_PATTERNS,
    )
    # `(2002)` is dropped by the year-only nonunit; no annotation emitted.
    assert scan.annotations == ()
    assert scan.errors == ()


def test_default_nonunit_drops_citation_prefix():
    """Shipped citation pattern drops ``(see X)`` content."""
    src = (
        "subroutine s\n"
        "  real :: v   ! wind speed (see Schmidt 2002)\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=_paren_unit_patterns(),
        nonunit_patterns=DEFAULT_NONUNIT_PATTERNS,
    )
    assert scan.annotations == ()
    assert scan.errors == ()


def test_default_nonunit_does_not_drop_real_unit_in_parens():
    """A genuine unit like ``(kg/m^3)`` survives — neither the year
    regex nor the ``(see`` prefix matches."""
    src = (
        "subroutine s\n"
        "  real :: v   ! density (kg/m^3)\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=_paren_unit_patterns(),
        nonunit_patterns=DEFAULT_NONUNIT_PATTERNS,
    )
    assert len(scan.annotations) == 1
    assert scan.annotations[0].unit_text == "kg/m^3"


def test_at_nonunit_marker_drops_overlapping_bracket_match():
    """Author writes ``@nonunit{[m]}`` to suppress an inline bracket
    that would otherwise extract as a unit annotation."""
    src = (
        "subroutine s\n"
        "  real :: v   ! example shape @nonunit{[m]}\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=_bracket_unit_patterns(),
        nonunit_patterns=DEFAULT_NONUNIT_PATTERNS,
    )
    assert scan.annotations == ()
    assert scan.errors == ()


# ---------------------------------------------------------------------------
# Empty nonunit opts out of all default filters
# ---------------------------------------------------------------------------


def test_empty_nonunit_lets_year_match_as_unit():
    """When ``nonunit`` is empty, year-only parens parse as a unit
    candidate (and surface U002 downstream — but at scan_text the
    annotation is emitted)."""
    src = (
        "subroutine s\n"
        "  real :: v   ! year (2002)\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=_paren_unit_patterns(),
        nonunit_patterns=(),
    )
    assert len(scan.annotations) == 1
    assert scan.annotations[0].unit_text == "2002"


# ---------------------------------------------------------------------------
# Per-family isolation: nonunit does NOT filter unit_assume / unit_affine
# ---------------------------------------------------------------------------


def test_nonunit_does_not_filter_unit_assume():
    """A ``nonunit`` matcher targeting ``@unit_assume{`` content does
    NOT drop a real ``@unit_assume`` capture — filters are
    per-family."""
    import re

    # Pretend a project ships an over-broad nonunit that would, if
    # cross-family, also kill assume captures:
    nonunit = (
        NonUnitPattern(open="@unit_assume{", close="}", regex=re.compile(r".+")),
    )
    src = (
        "subroutine s\n"
        "  v = 1.0      ! @unit_assume{m/s: legacy formula}\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=DEFAULT_UNIT_PATTERNS,
        assume_patterns=DEFAULT_ASSUME_PATTERNS,
        nonunit_patterns=nonunit,
    )
    # The assume capture survives — `nonunit` only filters `unit`.
    assert len(scan.assumes) == 1
    assert scan.assumes[0].unit_text == "m/s"


def test_nonunit_assume_drops_assume_capture():
    """``nonunit_assume`` is the per-family filter for ``unit_assume``."""
    import re

    nonunit_assume = (
        NonStructuredPattern(
            open="@unit_assume{", close="}", sep=":",
            regex=re.compile(r"^0\s*:"),
        ),
    )
    src = (
        "subroutine s\n"
        "  v = 1.0      ! @unit_assume{0: literal-zero}\n"
        "end subroutine\n"
    )
    scan = scan_text(
        src,
        unit_patterns=DEFAULT_UNIT_PATTERNS,
        assume_patterns=DEFAULT_ASSUME_PATTERNS,
        nonunit_assume_patterns=nonunit_assume,
    )
    assert scan.assumes == ()
