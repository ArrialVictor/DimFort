"""Suggested-rewrite detector for U002 (spec §12).

When a pattern matches a comment but the captured text fails the
unit parser, this module runs a small pipeline of rewrite rules. If
the final transformed string parses cleanly against the project
unit table, the diagnostic carries it as a ``suggested_rewrite``
payload — surfaced as "did you mean ...?" in the CLI and as a code
action in the LSP.

Rules are applied **in list order**. Each rule's output feeds the
next rule's input. Only the final string is shown to the user — no
provenance.

Design constraints on any new rule (per spec §12.5):
1. Idempotent: ``rule(rule(s)) == rule(s)`` for all ``s``.
2. Preferably commutative with peers (operates on disjoint
   character classes).
3. Explicitly ordered when not commutative, with a comment at the
   rule's spec entry explaining why.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from dimfort.core.units import (
    _DOT_MULT_RE,
    _IMPLICIT_PRODUCT_RE,
    _INTEGER_SUFFIX_EXP_RE,
    _apply_latex_brace_rewrite,
    _apply_unicode_superscript_rewrite,
)

if TYPE_CHECKING:
    from dimfort.core.units import UnitTable


# Spec §12.2 shipped rule. ``([a-zA-Z]+)(\d+)`` → ``\1^\2``. Acts
# only on alphabetic-followed-by-digit substrings, so ``m^2`` is
# untouched and ``kg/m3`` becomes ``kg/m^3``. Idempotent — applying
# it twice yields the same string (the ``^`` between the groups
# prevents re-match). Broader than the flag-paired
# ``allow_bare_digit_exp`` recogniser (no 14-symbol guard, no
# digits-9 cap) — the post-rewrite parse against ``UnitTable``
# filters unknown identifiers (``zz9`` → ``zz^9`` → no suggestion).
_DIGIT_SUFFIX_RE = re.compile(r"([a-zA-Z]+)(\d+)")


def _digit_suffix_to_caret(s: str) -> str:
    """Insert ``^`` between an alphabetic identifier and its trailing digits.

    Implements the spec §12.2 rule. ``kg/m3`` becomes ``kg/m^3``;
    already-correct strings like ``m^2`` are untouched. Idempotent —
    applying twice yields the same result.
    """
    return _DIGIT_SUFFIX_RE.sub(r"\1^\2", s)


# Layer 3a flag-paired rewrite rules — for each permissive-lexer
# flag (``permissive-unit-lexer.md`` §3) that's OFF, suggest the
# canonical form so a U002 emitted on permissive input carries a
# usable "did you mean ...?" payload. All rules produce strict-
# canonical output that parses regardless of which flags are on
# (design principle §2.3 — reading permissive, writing canonical).


def _unicode_superscripts_to_caret(s: str) -> str:
    """``m⁻¹`` → ``m^-1`` (paired with ``allow_unicode_superscripts``)."""
    return _apply_unicode_superscript_rewrite(s)


def _middot_to_star(s: str) -> str:
    """``m·s`` → ``m*s`` (paired with ``allow_middot_multiplication``)."""
    return s.replace("·", "*")


def _star_star_to_caret(s: str) -> str:
    """``m**2`` → ``m^2`` (paired with ``allow_fortran_star_star``)."""
    return s.replace("**", "^")


def _latex_braces_to_parens(s: str) -> str:
    """``m^{-1}`` → ``m^(-1)`` (paired with ``allow_latex_braces``).

    Runs after ``_star_star_to_caret`` so ``m**{2}`` reaches this
    pass as ``m^{2}`` — matches the tokenizer pipeline order in
    ``units.py`` (design §4.3).
    """
    return _apply_latex_brace_rewrite(s)


def _integer_suffix_exp_to_caret(s: str) -> str:
    """``kg m-3`` → ``kg m^-3`` (paired with ``allow_integer_suffix_exp``).

    Uses the same 14-symbol known-unit guard as the lexer rule
    (design §3.4), so symbolic-exponent linear forms like
    ``kappa-1`` are left untouched.
    """
    return _INTEGER_SUFFIX_EXP_RE.sub(r"\1^\2", s)


def _dot_mult_to_star(s: str) -> str:
    """``J.kg`` → ``J*kg`` (paired with ``allow_dot_multiplication``).

    Decimal literals (``0.5``, ``1.380658E-23``) are preserved
    because the lookbehind/ahead require letters on both sides.
    """
    return _DOT_MULT_RE.sub("*", s)


def _implicit_product_to_star(s: str) -> str:
    """``kg m`` → ``kg*m`` (paired with ``allow_implicit_product``)."""
    return _IMPLICIT_PRODUCT_RE.sub("*", s)


# In list order; the pipeline runs each rule on the previous rule's
# output. The Layer 3a additions follow the design §4.3 pipeline
# order (codepoint → operator alias → brace rewrite → recognition-
# subsystem rewrites). The pre-existing ``_digit_suffix_to_caret``
# (spec §12.2) sits first for backward compatibility and is
# disjoint from every Layer 3a rule.
RULES: tuple[Callable[[str], str], ...] = (
    _digit_suffix_to_caret,
    _unicode_superscripts_to_caret,
    _middot_to_star,
    _star_star_to_caret,
    _latex_braces_to_parens,
    _integer_suffix_exp_to_caret,
    _dot_mult_to_star,
    _implicit_product_to_star,
)


def suggest_rewrite(captured: str, table: UnitTable | None = None) -> str | None:
    """Run the rewrite pipeline on ``captured`` and return a usable suggestion.

    Each rule in :data:`RULES` is applied in list order, feeding its
    output to the next rule. A suggestion is emitted only when (a) the
    transformed string differs from the input AND (b) it parses cleanly
    against ``table``.

    Args:
        captured: Raw unit text captured from a unit-pattern comment
            (e.g. the inside of ``@unit{...}``).
        table: Unit table to parse against. ``None`` falls back to
            ``units.DEFAULT_TABLE``; if the default table itself is not
            yet installed (vanishingly rare — would mean ``unit_config``
            was never imported), the function returns ``None`` rather
            than raising.

    Returns:
        Transformed unit string suitable for a "did you mean ...?"
        prompt, or ``None`` if no rule changed the input, the result
        was unchanged, or the result still failed to parse. Parse
        exceptions of any kind (``UnitError``, ``ZeroDivisionError``
        from ``m^(2/0)`` reductions, ``IndexError`` on malformed token
        streams) are swallowed and treated as "no useful suggestion".
    """
    from dimfort.core import units as _units_mod
    active = table if table is not None else _units_mod.DEFAULT_TABLE
    if active is None:
        return None
    transformed = captured
    for rule in RULES:
        transformed = rule(transformed)
    if transformed == captured:
        return None
    # Best-effort path: any parse exception (UnitError, ZeroDivisionError
    # from ``m^(2/0)`` reductions, IndexError on malformed token streams,
    # etc.) means the candidate isn't safely parseable; treat it as
    # "no useful suggestion" rather than letting the failure bubble up
    # into the U002 emission site. Repro: suggest_rewrite('kg2/m^(2/0)')
    # would otherwise raise ZeroDivisionError.
    try:
        _units_mod.parse(transformed, active)
    except Exception:
        return None
    return transformed
