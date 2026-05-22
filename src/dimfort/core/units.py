"""Unit-expression parser and algebra.

- 7-slot dimension vector over SI base dimensions (M, L, T, Theta, I, N, J).
- Scalar prefactor (``Fraction``) capturing prefixes/conversions.
- Grammar: ``unit = term ((*|/) term)*``; ``term = factor (^ exp)?``;
  ``factor = ident | (unit) | 1``; ``exp = int | (int/int) | -exp``.
- ``/`` is left-associative, same precedence as ``*``. When a ``/`` is followed
  at the same paren depth by another ``*`` or ``/``, ``UnitAmbiguityWarning``
  is emitted.

Ported from the V4 prototype's ``homogeneity.units``; see
``others/Homemade/V4/decisions.md`` for design rationale.
"""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from fractions import Fraction

Number = int | Fraction
# ``Dim`` historically meant ``tuple[Number, Number, ...]`` (7 plain
# scalar exponents). Post symbolic-exponents Step 2, each slot is an
# ``Exponent`` after construction; ``Dim`` keeps the legacy spelling
# as a convenience so callers may still pass ``Number`` slots —
# ``Unit.__post_init__`` promotes them.
Dim = tuple[
    "Number | Exponent", "Number | Exponent", "Number | Exponent",
    "Number | Exponent", "Number | Exponent", "Number | Exponent",
    "Number | Exponent",
]


# ---------------------------------------------------------------------------
# Exponent — linear form over rationals with named opaque generators
# ---------------------------------------------------------------------------
#
# An ``Exponent`` represents a value of the form
#
#     q_1 * x_1 + q_2 * x_2 + ... + q_n * x_n + c
#
# where ``q_i ∈ Q``, ``x_i`` are named opaque "symbols" (Fortran
# identifiers used as power exponents — typically dim'less constants
# whose runtime value isn't known statically, like the Exner ``kappa``),
# and ``c ∈ Q`` is a constant term.
#
# The motivating use case is power-rule unit resolution where the
# exponent is a statically-unknown but runtime-constant value:
#
#     p ** kappa            -> base unit ^ Exponent({"kappa": 1}, 0)
#     p ** (1 - kappa)      -> base unit ^ Exponent({"kappa": -1}, 1)
#     2./7.                 -> Exponent({}, 2/7)   (pure constant)
#
# Cancellation works through structural identity: same ``terms`` and
# same ``constant`` ⇒ equal Exponents, regardless of how each was
# constructed. ``Pa^kappa * Pa^(1 - kappa)`` reduces to ``Pa^1 = Pa``
# because the resulting Exponent ``{"kappa": 0}, 1`` canonicalizes to
# ``Exponent({}, 1)`` via the smart constructor (zero-coefficient terms
# dropped).
#
# The algebra is deliberately **linear**: products of two
# Exponents-with-symbols (e.g. ``kappa * lambda``) are not supported.
# The Exner / Tetens patterns observed in real climate-model code
# never produce such products; restricting to the linear case keeps
# the algebra tractable (constant-coefficient linear forms are a
# decidable, normalisable structure).


@dataclass(frozen=True, eq=False)
class Exponent:
    """Linear combination of opaque generators with rational coefficients
    plus a rational constant. See module docstring for motivation.

    Equality with a plain ``Number`` (``int``/``Fraction``) returns
    True iff this Exponent is pure-constant and its constant equals the
    Number. This keeps the migration ergonomic: legacy code that
    compares a dimension slot against a literal still works.
    """
    terms: tuple[tuple[str, Fraction], ...]
    constant: Fraction

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Exponent):
            return self.terms == other.terms and self.constant == other.constant
        if isinstance(other, (int, Fraction)):
            return self.is_constant() and self.constant == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.terms, self.constant))

    def __post_init__(self) -> None:
        # Defensive: the smart constructor below should be the only path
        # for non-trivial construction. Allow direct construction with
        # already-canonical input (used in tests and after-arithmetic).
        # Validate that terms are sorted by name and have no zero coeffs;
        # otherwise the equality contract breaks.
        for name, coeff in self.terms:
            if coeff == 0:
                raise ValueError(
                    f"Exponent.terms contains a zero-coefficient entry "
                    f"({name!r}); use Exponent.build(...) to canonicalize"
                )
        names = [n for n, _ in self.terms]
        if names != sorted(names):
            raise ValueError(
                "Exponent.terms must be sorted by name; "
                "use Exponent.build(...) to canonicalize"
            )

    @classmethod
    def build(
        cls,
        terms: dict[str, Number] | tuple[tuple[str, Number], ...] = (),
        constant: Number = 0,
    ) -> Exponent:
        """Smart constructor: canonicalize a raw term mapping.

        Drops zero coefficients, promotes ``int`` to ``Fraction``,
        and sorts entries by name. Always use this for new Exponents
        unless you already have a canonical input.
        """
        items = list(terms.items()) if isinstance(terms, dict) else list(terms)
        # Aggregate duplicate keys (defensive — callers may pass either
        # a dict or a sequence of pairs; sequences can repeat).
        agg: dict[str, Fraction] = {}
        for name, coeff in items:
            f = Fraction(coeff) if not isinstance(coeff, Fraction) else coeff
            if name in agg:
                agg[name] = agg[name] + f
            else:
                agg[name] = f
        # Drop zero, sort, freeze.
        cleaned = tuple(
            (n, agg[n]) for n in sorted(agg) if agg[n] != 0
        )
        c = Fraction(constant) if not isinstance(constant, Fraction) else constant
        return cls(terms=cleaned, constant=c)

    @classmethod
    def from_value(cls, value: Number) -> Exponent:
        """Promote a literal rational to an Exponent (no symbol terms)."""
        return cls.build(constant=value)

    @classmethod
    def from_symbol(cls, name: str, coefficient: Number = 1) -> Exponent:
        """Build an Exponent representing ``coefficient * name``."""
        return cls.build(terms={name: coefficient})

    # ---- queries ---------------------------------------------------------

    def is_constant(self) -> bool:
        """``True`` iff this Exponent has no symbol terms."""
        return len(self.terms) == 0

    def is_zero(self) -> bool:
        """``True`` iff this Exponent is the additive identity (0)."""
        return len(self.terms) == 0 and self.constant == 0

    def is_one(self) -> bool:
        """``True`` iff this Exponent is the multiplicative identity (1)."""
        return len(self.terms) == 0 and self.constant == 1

    def as_fraction(self) -> Fraction | None:
        """If pure-constant, return the rational value; else ``None``."""
        return self.constant if self.is_constant() else None

    # ---- arithmetic ------------------------------------------------------

    def __add__(self, other: Exponent | Number) -> Exponent:
        if isinstance(other, Exponent):
            agg: dict[str, Fraction] = dict(self.terms)
            for name, coeff in other.terms:
                agg[name] = agg.get(name, Fraction(0)) + coeff
            return Exponent.build(agg, self.constant + other.constant)
        if isinstance(other, (int, Fraction)):
            return Exponent.build(dict(self.terms), self.constant + other)
        return NotImplemented

    def __radd__(self, other: Number) -> Exponent:
        return self.__add__(other)

    def __sub__(self, other: Exponent | Number) -> Exponent:
        if isinstance(other, Exponent):
            return self + (-other)
        if isinstance(other, (int, Fraction)):
            return Exponent.build(dict(self.terms), self.constant - other)
        return NotImplemented

    def __rsub__(self, other: Number) -> Exponent:
        return (-self) + other

    def __neg__(self) -> Exponent:
        return Exponent.build(
            {name: -coeff for name, coeff in self.terms},
            -self.constant,
        )

    def __mul__(self, other: Number | Exponent) -> Exponent:
        """Scalar multiplication only.

        ``Exponent * scalar`` and ``scalar * Exponent`` are linear and
        always defined. ``Exponent * Exponent`` is defined *only* when
        one side is pure-constant — otherwise the product would be
        quadratic in the symbols, outside the linear algebra this type
        represents. Raises ``UnitError`` in the non-linear case (the
        resolver catches this and falls back to ``D1.4``).
        """
        if isinstance(other, (int, Fraction)):
            return Exponent.build(
                {name: coeff * other for name, coeff in self.terms},
                self.constant * other,
            )
        if isinstance(other, Exponent):
            if other.is_constant():
                return self * other.constant
            if self.is_constant():
                return other * self.constant
            raise UnitError(
                f"Exponent×Exponent product is non-linear: "
                f"({self}) × ({other}) — both sides carry symbols"
            )
        return NotImplemented

    def __rmul__(self, other: Number) -> Exponent:
        return self.__mul__(other)

    # ---- presentation ----------------------------------------------------

    def __str__(self) -> str:
        if self.is_zero():
            return "0"
        # Build a list of (sign, magnitude_text) so we can join cleanly
        # without leaving stray operators or empty cells. The first
        # entry's "+" is dropped; subsequent ones use " + " / " - ".
        pieces: list[tuple[str, str]] = []
        for name, coeff in self.terms:
            if coeff >= 0:
                sign = "+"
                mag = coeff
            else:
                sign = "-"
                mag = -coeff
            if mag == 1:
                pieces.append((sign, name))
            else:
                pieces.append((sign, f"{mag}·{name}"))
        if self.constant != 0:
            if self.constant >= 0:
                pieces.append(("+", str(self.constant)))
            else:
                pieces.append(("-", str(-self.constant)))
        # First piece: render without a leading "+" but keep "-" if neg.
        first_sign, first_text = pieces[0]
        out = first_text if first_sign == "+" else f"-{first_text}"
        for sign, text in pieces[1:]:
            out += f" {sign} {text}"
        return out

DIM_LEN = 7
# ``ZERO_DIM`` is the dimensionless vector. Historically the slots were
# plain ``Number`` (zero); they now carry ``Exponent`` (zero linear
# form). The Unit constructor auto-promotes via __post_init__ either
# way, so this constant works at every legacy / new call site.
ZERO_DIM: Dim = (Exponent.from_value(0),) * 7


class UnitError(ValueError):
    pass


class UnknownUnitError(UnitError):
    """Raised when an identifier is not in the unit table."""


class UnitAmbiguityWarning(UserWarning):
    pass


@dataclass(frozen=True)
class Unit:
    """Dimension vector (one Exponent per SI base slot) plus a rational
    prefactor.

    The dimension slots historically held ``Number`` (``int | Fraction``).
    As of the symbolic-exponents work, each slot is an ``Exponent``: a
    linear form over Q with named opaque generators. Existing callers
    that pass ``int``/``Fraction`` slots still work — ``__post_init__``
    promotes scalar entries to ``Exponent.from_value`` automatically.
    """
    dimension: tuple[Exponent, ...]
    factor: Fraction

    def __post_init__(self) -> None:
        # Coerce legacy callers passing ``Number`` per slot. After this
        # method runs, every entry in ``dimension`` is an Exponent.
        if len(self.dimension) != DIM_LEN:
            raise UnitError(
                f"Unit.dimension must have {DIM_LEN} slots, "
                f"got {len(self.dimension)}"
            )
        promoted = tuple(
            d if isinstance(d, Exponent) else Exponent.from_value(d)
            for d in self.dimension
        )
        # Always rebind: ``Exponent.__eq__`` returns True against
        # equivalent Numbers, so a tuple-level ``!=`` check can't
        # detect "needs promotion".
        if any(not isinstance(d, Exponent) for d in self.dimension):
            object.__setattr__(self, "dimension", promoted)
        # Coerce factor too.
        if not isinstance(self.factor, Fraction):
            object.__setattr__(self, "factor", Fraction(self.factor))

    def __mul__(self, other: Unit) -> Unit:
        return Unit(
            tuple(a + b for a, b in zip(self.dimension, other.dimension, strict=False)),
            self.factor * other.factor,
        )

    def __truediv__(self, other: Unit) -> Unit:
        return Unit(
            tuple(a - b for a, b in zip(self.dimension, other.dimension, strict=False)),
            self.factor / other.factor,
        )

    def pow(self, exp: Number | Exponent) -> Unit:
        """Raise the unit to a power.

        ``exp`` may be a literal ``Number`` (the legacy path) or an
        ``Exponent`` (symbolic). The result's dimension is the slot-wise
        product of the current Exponent and ``exp``; ``Exponent.__mul__``
        raises ``UnitError`` if the multiplication is non-linear (both
        sides have symbol terms) — the caller (``power(...)`` below)
        converts that into a D1.4 diagnostic.
        """
        scalar: Exponent | Number = (
            exp if isinstance(exp, Exponent) else exp
        )
        new_dim = tuple(a * scalar for a in self.dimension)
        if self.factor == 1:
            new_factor = Fraction(1)
        elif isinstance(exp, int):
            new_factor = self.factor ** exp
        else:
            # Rational or symbolic exponent on a prefixed/scaled factor
            # would generally not stay rational; punt rather than lose
            # precision. Symbolic-exp on a factor==1 unit takes the
            # branch above and is fine.
            raise UnitError(
                f"non-integer exponent on prefixed/scaled unit not supported "
                f"(factor={self.factor}, exp={exp})"
            )
        return Unit(new_dim, new_factor)


# ---------------------------------------------------------------------------
# Wrapper types (Phase B of the unit-algebra spec)
# ---------------------------------------------------------------------------
#
# A "Regular" unit is the ``Unit`` 7-tuple above. ``LogWrap`` and
# ``ExpWrap`` are sibling unit types tagging an inner ``UnitExpr`` as
# "in log space" or "in exp space" respectively. Together they form
# the recursive ``UnitExpr`` tree from spec §1.2.
#
# Canonicalization (R2.1/R2.2/R2.3) is applied eagerly through the
# ``wrap_log`` / ``wrap_exp`` smart constructors — direct LogWrap(...)
# / ExpWrap(...) construction bypasses canonicalization and should be
# used only when you have proved the operand can't trigger a reduction.


@dataclass(frozen=True)
class LogWrap:
    """Unit tagged as residing in log space (spec §1.2)."""
    inner: UnitExpr


@dataclass(frozen=True)
class ExpWrap:
    """Unit tagged as residing in exp space (spec §1.2)."""
    inner: UnitExpr


UnitExpr = Unit | LogWrap | ExpWrap


def is_dimensionless(u: UnitExpr) -> bool:
    """``True`` iff ``u`` is a ``Unit`` with all base exponents zero.

    Wrappers around dim'less never exist post-canonicalization (R2.3),
    so a wrapper is by definition non-dim'less.
    """
    return isinstance(u, Unit) and all(d.is_zero() for d in u.dimension)


def wrap_log(u: UnitExpr) -> UnitExpr:
    """Construct ``LOG(u)`` with canonicalization (R3.1 + R2.1 + R2.3)."""
    from dimfort.core.trace import trace_step
    if isinstance(u, ExpWrap):
        result = u.inner  # R2.1
        trace_step("R2.1", (u,), result)
        return result
    if is_dimensionless(u):
        trace_step("R2.3", (u,), u)  # R2.3 — log of dim'less is dim'less
        return u
    result = LogWrap(u)
    trace_step("R3.1", (u,), result)
    return result


def wrap_exp(u: UnitExpr) -> UnitExpr:
    """Construct ``EXP(u)`` with canonicalization (R3.2 + R2.2 + R2.3)."""
    from dimfort.core.trace import trace_step
    if isinstance(u, LogWrap):
        result = u.inner  # R2.2
        trace_step("R2.2", (u,), result)
        return result
    if is_dimensionless(u):
        trace_step("R2.3", (u,), u)  # R2.3 — exp of dim'less is dim'less
        return u
    result = ExpWrap(u)
    trace_step("R3.2", (u,), result)
    return result


def _u(dim: Dim, factor: Number = 1) -> Unit:
    return Unit(dim, Fraction(factor))


# ---------------------------------------------------------------------------
# Binary unit-algebra dispatch (Phase B sub-steps 3+)
# ---------------------------------------------------------------------------
#
# ``combine(op, a, b, ...)`` is the single entry point for applying a
# binary operator at the unit-algebra level. It centralises the rules
# in §4–§7 of the spec so the checker doesn't have to keep them in
# sync across two recursive walks (_resolve for result, _walk for
# diagnostics).
#
# Diagnostic codes (strings, intentionally simple): None on success;
# otherwise one of 'D1.1' / 'D1.2' / 'D1.3' / 'D1.4'. The caller maps
# the code to a concrete Diagnostic with AST position. (D1.5 — implicit
# literal cast — is handled in the checker, not here, because it
# requires "is this operand a literal" which is an AST property.)
#
# Sub-step 3 implements the Regular and LogWrap rules. ExpWrap and
# cross-cases land in sub-step 4; until then ExpWrap operands return
# ``(None, None)`` (unknown, no diagnostic).


def _logwrap_inner_pow(inner: UnitExpr, k: Number) -> UnitExpr | None:
    """Compute ``inner ^ k`` for use under LogWrap (R5.4 inner side).

    Only defined when ``inner`` is ``Unit``; nested-wrapper inners fall
    through to the caller as ``None`` (sub-step 4 territory).
    """
    if not isinstance(inner, Unit):
        return None
    try:
        return inner.pow(k)
    except Exception:
        return None


def combine(
    op: str,
    a: UnitExpr,
    b: UnitExpr,
    *,
    a_literal: Number | None = None,
    b_literal: Number | None = None,
) -> tuple[UnitExpr | None, str | None]:
    """Apply binary op ``a <op> b`` at the unit level.

    ``op`` is one of ``'+', '-', '*', '/'``. ``*_literal`` carries the
    operand's literal-rational value when the corresponding source-AST
    operand is a pure numeric literal (used by R5.4 to decide
    literal-scalar × LogWrap vs R5.5 runtime-scalar × LogWrap).

    Returns ``(result_unit_or_None, diag_code_or_None)``. A non-None
    diag_code with a None result means the op is undefined (rule error);
    a non-None result with no diag_code is success.
    """
    from dimfort.core.trace import trace_step

    def _ok(rule_id: str, result: UnitExpr) -> tuple[UnitExpr, None]:
        trace_step(rule_id, (a, b), result)
        return result, None

    def _err(rule_id: str, diag: str) -> tuple[None, str]:
        trace_step(rule_id, (a, b), None)
        return None, diag
    # ---- Regular × Regular (§4) ----
    if isinstance(a, Unit) and isinstance(b, Unit):
        if op in ("+", "-"):
            if equal_dim(a, b):
                return _ok("R4.1", a)
            if a_literal is not None and is_dimensionless(a) and not is_dimensionless(b):
                return b, "D1.5"
            if b_literal is not None and is_dimensionless(b) and not is_dimensionless(a):
                return a, "D1.5"
            return _err("R4.1", "D1.1")
        if op == "*":
            return _ok("R4.2", a * b)
        if op == "/":
            return _ok("R4.2", a / b)
        return None, None

    # ---- LogWrap × LogWrap (§5) ----
    if isinstance(a, LogWrap) and isinstance(b, LogWrap):
        if op == "+":
            inner, diag = combine("*", a.inner, b.inner)
            if inner is None:
                return _err("R5.1", diag or "D1.2")
            return _ok("R5.1", wrap_log(inner))
        if op == "-":
            inner, diag = combine("/", a.inner, b.inner)
            if inner is None:
                return _err("R5.2", diag or "D1.2")
            return _ok("R5.2", wrap_log(inner))
        if op in ("*", "/"):
            return _err("R5.6", "D1.2")
        return None, None

    # ---- LogWrap with Regular (commute for + and *) ----
    if isinstance(a, Unit) and isinstance(b, LogWrap):
        if op == "+":
            return combine("+", b, a, a_literal=b_literal, b_literal=a_literal)
        if op == "-":
            if is_dimensionless(a):
                return _ok("R5.3", b)
            return _err("R5.10", "D1.3")
        if op == "*":
            return combine("*", b, a, a_literal=b_literal, b_literal=a_literal)
        if op == "/":
            return _err("R5.9", "D1.2")
        return None, None

    if isinstance(a, LogWrap) and isinstance(b, Unit):
        if op in ("+", "-"):
            if is_dimensionless(b):
                return _ok("R5.3", a)
            return _err("R5.10", "D1.3")
        if op == "*":
            if is_dimensionless(b):
                if b_literal is None:
                    return _err("R5.5", "D1.4")
                new_inner = _logwrap_inner_pow(a.inner, b_literal)
                if new_inner is None:
                    return None, None
                return _ok("R5.4", wrap_log(new_inner))
            return _err("R5.7", "D1.2")
        if op == "/":
            if is_dimensionless(b):
                if b_literal is None:
                    return _err("R5.5", "D1.4")
                if b_literal == 0:
                    return None, None
                inv = (
                    Fraction(1, b_literal)
                    if isinstance(b_literal, int)
                    else Fraction(1) / b_literal
                )
                new_inner = _logwrap_inner_pow(a.inner, inv)
                if new_inner is None:
                    return None, None
                return _ok("R5.4", wrap_log(new_inner))
            return _err("R5.7", "D1.2")
        return None, None

    # ---- ExpWrap × ExpWrap (§6) ----
    if isinstance(a, ExpWrap) and isinstance(b, ExpWrap):
        if op == "*":
            inner, diag = combine("+", a.inner, b.inner)
            if inner is None:
                return _err("R6.1", diag or "D1.1")
            return _ok("R6.1", wrap_exp(inner))
        if op == "/":
            inner, diag = combine("-", a.inner, b.inner)
            if inner is None:
                return _err("R6.2", diag or "D1.1")
            return _ok("R6.2", wrap_exp(inner))
        if op in ("+", "-"):
            return _err("R6.5", "D1.3")
        return None, None

    # ---- ExpWrap with Regular (commute for × and +) ----
    if isinstance(a, Unit) and isinstance(b, ExpWrap):
        if op == "*":
            return combine("*", b, a, a_literal=b_literal, b_literal=a_literal)
        if op == "/":
            if is_dimensionless(a):
                inv_inner = _logwrap_inner_pow(b.inner, -1) if isinstance(b.inner, Unit) else None
                if inv_inner is None:
                    return None, None
                return _ok("R6.3", wrap_exp(inv_inner))
            return _err("R6.7", "D1.2")
        if op == "+":
            return combine("+", b, a, a_literal=b_literal, b_literal=a_literal)
        if op == "-":
            if a_literal is not None and is_dimensionless(a):
                return b, "D1.5"
            return _err("R6.6", "D1.3")
        return None, None

    if isinstance(a, ExpWrap) and isinstance(b, Unit):
        if op == "*":
            if is_dimensionless(b):
                return _ok("R6.3", a)
            return _err("R6.7", "D1.2")
        if op == "/":
            if is_dimensionless(b):
                return _ok("R6.3", a)
            return _err("R6.7", "D1.2")
        if op in ("+", "-"):
            if b_literal is not None and is_dimensionless(b):
                return a, "D1.5"
            return _err("R6.6", "D1.3")
        return None, None

    # ---- LogWrap × ExpWrap (§7) ----
    if (isinstance(a, LogWrap) and isinstance(b, ExpWrap)) or (
        isinstance(a, ExpWrap) and isinstance(b, LogWrap)
    ):
        if op in ("*", "/"):
            return _err("R7.1", "D1.2")
        if op in ("+", "-"):
            return _err("R6.6", "D1.3")
        return None, None

    return None, None


def power(
    base: UnitExpr,
    exponent_unit: UnitExpr | None = None,
    exponent_value: Number | Exponent | None = None,
) -> tuple[UnitExpr | None, str | None]:
    """Apply ``base ^ exponent`` at the unit level (spec Table 14.4).

    Dispatch is a 4×4 (base × exponent) table that decomposes into two
    gates:

    1. **Exponent type-check (D1.7)** — an exponent must be
       dimensionless. ``base ^ Rn``, ``base ^ Ln``, ``base ^ En``
       all error with D1.7 regardless of ``base``. The mathematical
       reading via ``a^b = exp(b·log(a))`` would give a typed
       ExpWrap result, but in practice ``2.0 ** speed`` style
       expressions are virtually always bugs; the gate surfaces
       them at the power site. (D1.7 defaults to a WARNING, so
       projects that genuinely live in exp-tagged space can opt
       out or be tolerated by default.)

    2. **Base-specific value gate** — once the exponent is known
       to be dim'less, the base determines whether the value
       matters:

         - ``Rd`` (dim'less): result is always ``Rd``. ``0·k = 0``
           for any ``k`` — literal, non-literal, integer, irrational.
           This refinement to R4.3's (Rd, non-literal-k) cell closes
           a class of false positives observed in real-world corpora.
         - ``Rn``: result is ``Rn(k·t)`` if the exponent value is
           a known literal rational; ``D1.4`` if not (classic Exner
           ``p^kappa`` pattern needing OQ4 to resolve precisely).
         - ``Ln``: result is ``Ln`` only for the trivial identity
           ``k = 1`` (R5.9); otherwise ``D1.2``.
         - ``En``: ``ExpWrap(k·U)`` if ``k`` is known literal
           (R6.4); ``D1.4`` if not.

    ``exponent_unit`` is the resolved unit of the exponent expression
    (or ``None`` if the checker couldn't determine it — usually
    because the variable lacks an annotation; U005 surfaces that
    underlying issue, so we do NOT fire D1.7 on unknown-unit
    exponents).

    ``exponent_value`` is the literal rational extracted at the
    AST level, or ``None`` for non-literal expressions.
    """
    from dimfort.core.trace import trace_step

    # ---- Gate 1: exponent must be dimensionless --------------------
    # Unknown exponent unit is treated as "could be dim'less" —
    # don't fire D1.7. The unannotated-declaration warning (U005)
    # is the right diagnostic for the underlying issue; firing
    # D1.7 here would double-flag the same code.
    if exponent_unit is not None and not is_dimensionless(exponent_unit):
        trace_step("R4.3", (base,), None)
        return None, "D1.7"

    # ---- Gate 2: base-specific result ------------------------------
    if isinstance(base, Unit):
        if is_dimensionless(base):
            # Rd ^ anything-dim'less = Rd. ``0·k = 0`` for every k,
            # so the result's dimension is independent of the
            # exponent's value or literalness.
            trace_step("R4.3", (base,), base)
            return base, None
        # Rn base — need the literal value to scale dims.
        if exponent_value is None:
            trace_step("R4.3", (base,), None)
            return None, "D1.4"
        try:
            result = base.pow(exponent_value)
            trace_step("R4.3", (base,), result)
            return result, None
        except Exception:
            return None, None

    if isinstance(base, LogWrap):
        if exponent_value == 1:
            trace_step("R5.9", (base,), base)
            return base, None
        trace_step("R5.9", (base,), None)
        return None, "D1.2"

    if isinstance(base, ExpWrap):
        if exponent_value is None:
            trace_step("R6.4", (base,), None)
            return None, "D1.4"
        if exponent_value == 1:
            trace_step("R6.4", (base,), base)
            return base, None
        new_inner = _logwrap_inner_pow(base.inner, exponent_value)
        if new_inner is None:
            return None, None
        result = wrap_exp(new_inner)
        trace_step("R6.4", (base,), result)
        return result, None

    return None, None


@dataclass(frozen=True)
class UnitTable:
    base: dict[str, Unit]
    derived: dict[str, Unit]
    prefixable: frozenset[str]   # names that allow prefix expansion
    prefixes: dict[str, Fraction]


# Populated by :mod:`dimfort.core.unit_config` at import time so callers can
# do ``parse(expr)`` without threading a table through.
DEFAULT_TABLE: UnitTable | None = None


def _resolve_identifier(name: str, table: UnitTable) -> Unit:
    if name in table.base:
        return table.base[name]
    if name in table.derived:
        return table.derived[name]
    for p, factor in table.prefixes.items():
        if name.startswith(p) and len(name) > len(p):
            rest = name[len(p):]
            if rest in table.prefixable:
                base = table.base.get(rest) or table.derived[rest]
                return Unit(base.dimension, factor * base.factor)
    raise UnknownUnitError(f"unknown unit identifier: {name!r}")


_TOKEN_RE = re.compile(
    r"""
    \s+                          |  # whitespace
    (?P<ID>[A-Za-z][A-Za-z0-9]*) |
    (?P<INT>\d+)                 |
    (?P<OP>[*/^()\-])            |
    (?P<BAD>.)
    """,
    re.VERBOSE,
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(expr):
        if m.group("ID") is not None:
            tokens.append(("ID", m.group("ID")))
        elif m.group("INT") is not None:
            tokens.append(("INT", m.group("INT")))
        elif m.group("OP") is not None:
            tokens.append(("OP", m.group("OP")))
        elif m.group("BAD") is not None:
            raise UnitError(f"unexpected character {m.group('BAD')!r} in {expr!r}")
    tokens.append(("END", ""))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], table: UnitTable):
        self.tokens = tokens
        self.i = 0
        self.table = table

    def peek(self) -> tuple[str, str]:
        return self.tokens[self.i]

    def consume(self) -> tuple[str, str]:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise UnitError(f"expected {kind} {value!r}, got {tok}")
        return self.consume()

    def parse_unit(self) -> UnitExpr:
        left = self.parse_term()
        slash_seen = False
        while self.peek() == ("OP", "*") or self.peek() == ("OP", "/"):
            op = self.consume()[1]
            if slash_seen:
                warnings.warn(
                    "ambiguous unit expression: '/' followed by another "
                    "'*' or '/' — add parentheses",
                    UnitAmbiguityWarning,
                    stacklevel=4,
                )
            if op == "/":
                slash_seen = True
            right = self.parse_term()
            if not (isinstance(left, Unit) and isinstance(right, Unit)):
                raise UnitError(
                    "arithmetic between LOG/EXP-wrapped units in @unit{} "
                    "annotations is not supported; only Regular×Regular allowed"
                )
            left = left * right if op == "*" else left / right
        return left

    def parse_term(self) -> UnitExpr:
        f = self.parse_factor()
        if self.peek() == ("OP", "^"):
            self.consume()
            e = self.parse_exp()
            if not isinstance(f, Unit):
                raise UnitError(
                    "power on a LOG/EXP-wrapped unit is not expressible "
                    "in @unit{} annotation syntax"
                )
            f = f.pow(e)
        return f

    def parse_factor(self) -> UnitExpr:
        tok = self.peek()
        if tok == ("OP", "("):
            self.consume()
            inner = self.parse_unit()
            self.expect("OP", ")")
            return inner
        if tok[0] == "INT":
            if tok[1] != "1":
                raise UnitError(f"only the literal '1' is a valid factor, got {tok[1]!r}")
            self.consume()
            return _u(ZERO_DIM)
        if tok[0] == "ID":
            # LOG(...) / EXP(...) wrappers shadow any same-named unit
            # identifier. Case-insensitive per A2 (annotation syntax).
            upper = tok[1].upper()
            if upper in ("LOG", "EXP") and self.tokens[self.i + 1] == ("OP", "("):
                self.consume()  # ID
                self.consume()  # (
                inner = self.parse_unit()
                self.expect("OP", ")")
                return wrap_log(inner) if upper == "LOG" else wrap_exp(inner)
            self.consume()
            return _resolve_identifier(tok[1], self.table)
        raise UnitError(f"expected unit factor, got {tok}")

    def parse_exp(self) -> Number:
        tok = self.peek()
        if tok == ("OP", "-"):
            self.consume()
            return -self.parse_exp()
        if tok == ("OP", "("):
            self.consume()
            num = int(self.expect("INT")[1])
            self.expect("OP", "/")
            den = int(self.expect("INT")[1])
            self.expect("OP", ")")
            return Fraction(num, den)
        if tok[0] == "INT":
            return int(self.consume()[1])
        raise UnitError(f"expected exponent, got {tok}")


def base_symbols(table: UnitTable | None = None) -> tuple[str, ...]:
    """Return the base-unit symbols in dimension-slot order.

    Falls back to the SI slot name (``M``, ``L``, …) for any slot the
    active table doesn't cover.
    """
    if table is None:
        if DEFAULT_TABLE is None:
            raise RuntimeError("DEFAULT_TABLE not initialised — import dimfort.core.unit_config")
        table = DEFAULT_TABLE
    out: list[str | None] = [None] * DIM_LEN
    for name, u in table.base.items():
        for i, e in enumerate(u.dimension):
            if e.is_one() and out[i] is None:
                out[i] = name
                break
    fallback = ("M", "L", "T", "Theta", "I", "N", "J")
    return tuple(name or fallback[i] for i, name in enumerate(out))


_SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "-": "⁻",
}


def _to_super(s: str) -> str:
    return "".join(_SUPERSCRIPTS.get(c, c) for c in s)


def format_unit(u: UnitExpr, *, show_factor: bool = False, table: UnitTable | None = None) -> str:
    """Render ``u`` as a human-readable expression.

    Uses Unicode superscripts (``²``, ``³``, …) for integer exponents
    and ``×`` for multiplication. Rational exponents fall back to
    ``^(p/q)`` since superscript fractions look messy. ``LogWrap`` /
    ``ExpWrap`` print as ``LOG(...)`` / ``EXP(...)`` per spec §9.
    """
    if isinstance(u, LogWrap):
        return f"LOG({format_unit(u.inner, show_factor=show_factor, table=table)})"
    if isinstance(u, ExpWrap):
        return f"EXP({format_unit(u.inner, show_factor=show_factor, table=table)})"
    names = base_symbols(table)
    pos_terms: list[str] = []
    neg_terms: list[str] = []
    for sym, exp in zip(names, u.dimension, strict=False):
        if exp.is_zero():
            continue
        # Pure-constant exponents render through the legacy
        # superscript / fraction path. Symbolic exponents render with
        # the bracketed form ``sym^(2/7·kappa + 1)`` since superscripts
        # can't express linear combinations.
        q = exp.as_fraction()
        if q is not None:
            mag = abs(q)
            sign_positive = q > 0
            if mag == 1:
                term = sym
            elif mag.denominator == 1:
                term = sym + _to_super(str(int(mag)))
            else:
                term = f"{sym}^({mag})"
            (pos_terms if sign_positive else neg_terms).append(term)
        else:
            # Symbolic: print as sym^(<linear form>). Never collapse to
            # the denominator side — we don't know the sign of a symbolic
            # exponent in general.
            term = f"{sym}^({exp})"
            pos_terms.append(term)
    body = "×".join(pos_terms) if pos_terms else "1"
    if neg_terms:
        denom = "×".join(neg_terms)
        if len(neg_terms) > 1:
            denom = f"({denom})"
        body = f"{body}/{denom}"
    if show_factor and u.factor != 1:
        return f"{u.factor}×{body}" if body != "1" else f"{u.factor}"
    return body


def parse(expr: str, table: UnitTable | None = None) -> UnitExpr:
    if table is None:
        if DEFAULT_TABLE is None:
            raise RuntimeError("DEFAULT_TABLE not initialised — import dimfort.core.unit_config")
        table = DEFAULT_TABLE
    tokens = _tokenize(expr)
    p = _Parser(tokens, table)
    u = p.parse_unit()
    if p.peek()[0] != "END":
        raise UnitError(f"unexpected trailing input near {p.peek()} in {expr!r}")
    return u


def equal_dim(a: UnitExpr, b: UnitExpr) -> bool:
    """Structural dimension-equality on the ``UnitExpr`` tree.

    Two ``Unit`` leaves compare on their 7-tuples (factor ignored).
    Two ``LogWrap`` (or two ``ExpWrap``) compare by recursing into
    ``inner``. A wrapper is never dim-equal to a leaf.
    """
    if isinstance(a, Unit) and isinstance(b, Unit):
        return tuple(a.dimension) == tuple(b.dimension)
    if isinstance(a, LogWrap) and isinstance(b, LogWrap):
        return equal_dim(a.inner, b.inner)
    if isinstance(a, ExpWrap) and isinstance(b, ExpWrap):
        return equal_dim(a.inner, b.inner)
    return False


def equal_strict(a: UnitExpr, b: UnitExpr) -> bool:
    """Like :func:`equal_dim` but ``Unit`` leaves also compare factors."""
    if isinstance(a, Unit) and isinstance(b, Unit):
        return equal_dim(a, b) and a.factor == b.factor
    if isinstance(a, LogWrap) and isinstance(b, LogWrap):
        return equal_strict(a.inner, b.inner)
    if isinstance(a, ExpWrap) and isinstance(b, ExpWrap):
        return equal_strict(a.inner, b.inner)
    return False
