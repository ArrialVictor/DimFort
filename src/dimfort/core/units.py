"""Unit-expression parser and algebra.

- 7-slot dimension vector over SI base dimensions (M, L, T, Theta, I, N, J),
  with each slot a symbolic ``Exponent`` (linear form over Q with named
  opaque generators) — supports runtime-constant exponents like the Exner
  ``kappa``.
- Scalar prefactor (``Fraction``) capturing prefixes/conversions, plus an
  affine ``offset`` (``Fraction``) for absolute units like degC
  (``x_base = factor*x + offset``).
- ``tyvars`` carry parametric-polymorphism type-variable exponents
  (Kennedy-style AG-extension) — ``'a^k`` composes with the symbolic
  exponent machinery.
- ``LogWrap`` / ``ExpWrap`` recursively tag a ``UnitExpr`` as residing in
  log/exp space; ``wrap_log`` / ``wrap_exp`` apply R2/R3 canonicalization.
- ``combine`` / ``power`` are the single dispatch engine over the unit
  algebra (R1–R7 of the spec); ``Verdict`` / ``compare`` provide the
  scale-layer comparison (dimension, factor, offset).
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
    """Linear combination of opaque generators with rational coefficients plus a rational constant.

    See the module docstring for motivation. Equality with a plain
    ``Number`` (``int`` / ``Fraction``) returns ``True`` iff this
    Exponent is pure-constant and its constant equals the Number; this
    keeps the migration ergonomic — legacy code comparing a dimension
    slot against a literal still works.

    Attributes:
        terms: Tuple of ``(name, coefficient)`` pairs in canonical form
            (sorted by name, no zero coefficients, coefficients are
            :class:`Fraction`).
        constant: The rational constant ``c`` in the linear form.
    """
    terms: tuple[tuple[str, Fraction], ...]
    constant: Fraction

    def __eq__(self, other: object) -> bool:
        """Compare for structural equality, with Number-coercion shim.

        Args:
            other: Another :class:`Exponent`, or a plain ``int`` /
                :class:`Fraction` (treated as a pure-constant Exponent).

        Returns:
            ``True`` iff both sides denote the same linear form;
            ``NotImplemented`` for unsupported operand types.
        """
        if isinstance(other, Exponent):
            return self.terms == other.terms and self.constant == other.constant
        if isinstance(other, (int, Fraction)):
            return self.is_constant() and self.constant == other
        return NotImplemented

    def __hash__(self) -> int:
        """Hash consistent with :meth:`__eq__`.

        For a pure-constant Exponent the hash equals the hash of the
        bare Number so dict / set lookups don't depend on which side
        of the comparison sits in the bucket; otherwise the hash is
        structural over ``(terms, constant)``.

        Returns:
            Integer hash.
        """
        # Python contract: ``a == b`` ⇒ ``hash(a) == hash(b)``. Since
        # ``__eq__`` returns True against an ``int``/``Fraction`` for a
        # pure-constant Exponent, the hash for that case must equal the
        # hash of the bare Number — otherwise dict / set membership
        # gives different answers depending on which side of the
        # comparison is in the bucket. For non-pure (terms present),
        # hash the full structural form.
        if not self.terms:
            return hash(self.constant)
        return hash((self.terms, self.constant))

    def __post_init__(self) -> None:
        """Validate that ``terms`` is already in canonical form.

        The smart constructor :meth:`build` is the only path for
        non-trivial construction; direct construction is allowed when
        the caller has already-canonical input (tests and the output
        of arithmetic methods).

        Raises:
            ValueError: If ``terms`` contains a zero-coefficient entry
                or is not sorted by name — either would break the
                equality / hash contract.
        """
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
        """Canonicalize a raw term mapping into an :class:`Exponent`.

        Aggregates duplicate keys by summing coefficients (callers may
        pass a sequence of pairs with repeats), drops zero coefficients,
        promotes ``int`` to :class:`Fraction` (including the ``constant``
        parameter), and sorts entries by name. Always use this for new
        Exponents unless the input is already canonical.

        Args:
            terms: Mapping (or sequence of pairs) from generator name to
                coefficient. Empty by default.
            constant: Rational constant term. Defaults to ``0``.

        Returns:
            A canonical :class:`Exponent`.
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
        """Promote a literal rational to a pure-constant Exponent.

        Args:
            value: Integer or :class:`Fraction` constant.

        Returns:
            Exponent with empty ``terms`` and ``constant=value``.
        """
        return cls.build(constant=value)

    @classmethod
    def from_symbol(cls, name: str, coefficient: Number = 1) -> Exponent:
        """Build an Exponent representing ``coefficient * name``.

        Args:
            name: Generator identifier (e.g. ``"kappa"``).
            coefficient: Rational coefficient on the generator. Defaults
                to ``1``.

        Returns:
            Single-term Exponent with zero constant.
        """
        return cls.build(terms={name: coefficient})

    # ---- queries ---------------------------------------------------------

    def is_constant(self) -> bool:
        """Return ``True`` iff this Exponent has no symbol terms.

        Returns:
            ``True`` for a pure-constant Exponent; ``False`` otherwise.
        """
        return len(self.terms) == 0

    def is_zero(self) -> bool:
        """Return ``True`` iff this Exponent is the additive identity (0).

        Returns:
            ``True`` iff ``terms`` is empty and ``constant == 0``.
        """
        return len(self.terms) == 0 and self.constant == 0

    def is_one(self) -> bool:
        """Return ``True`` iff this Exponent is the multiplicative identity (1).

        Returns:
            ``True`` iff ``terms`` is empty and ``constant == 1``.
        """
        return len(self.terms) == 0 and self.constant == 1

    def as_fraction(self) -> Fraction | None:
        """Return the rational value if pure-constant.

        Returns:
            The :class:`Fraction` ``constant`` when :meth:`is_constant`
            holds, otherwise ``None``.
        """
        return self.constant if self.is_constant() else None

    # ---- arithmetic ------------------------------------------------------

    def __add__(self, other: Exponent | Number) -> Exponent:
        """Add another Exponent or a numeric constant.

        Args:
            other: Right-hand operand — :class:`Exponent`, ``int``, or
                :class:`Fraction`.

        Returns:
            The canonicalized sum, or ``NotImplemented`` for
            unsupported operand types.
        """
        if isinstance(other, Exponent):
            agg: dict[str, Number] = dict(self.terms)
            for name, coeff in other.terms:
                agg[name] = agg.get(name, Fraction(0)) + coeff
            return Exponent.build(agg, self.constant + other.constant)
        if isinstance(other, (int, Fraction)):
            return Exponent.build(dict(self.terms), self.constant + other)
        return NotImplemented

    def __radd__(self, other: Number) -> Exponent:
        """Right-side addition with a numeric constant.

        Args:
            other: Left-hand ``int`` or :class:`Fraction` operand.

        Returns:
            The canonicalized sum.
        """
        return self.__add__(other)

    def __sub__(self, other: Exponent | Number) -> Exponent:
        """Subtract another Exponent or a numeric constant.

        Args:
            other: Right-hand operand — :class:`Exponent`, ``int``, or
                :class:`Fraction`.

        Returns:
            The canonicalized difference, or ``NotImplemented`` for
            unsupported operand types.
        """
        if isinstance(other, Exponent):
            return self + (-other)
        if isinstance(other, (int, Fraction)):
            return Exponent.build(dict(self.terms), self.constant - other)
        return NotImplemented

    def __rsub__(self, other: Number) -> Exponent:
        """Right-side subtraction with a numeric constant.

        Args:
            other: Left-hand ``int`` or :class:`Fraction` operand.

        Returns:
            The canonicalized ``other - self``.
        """
        return (-self) + other

    def __neg__(self) -> Exponent:
        """Negate every coefficient and the constant.

        Returns:
            The canonical additive inverse of this Exponent.
        """
        return Exponent.build(
            {name: -coeff for name, coeff in self.terms},
            -self.constant,
        )

    def __mul__(self, other: Number | Exponent) -> Exponent:
        """Multiply by a scalar (or a pure-constant Exponent).

        ``Exponent * scalar`` and ``scalar * Exponent`` are linear and
        always defined. ``Exponent * Exponent`` is defined *only* when
        one side is pure-constant — otherwise the product would be
        quadratic in the symbols, outside the linear algebra this type
        represents.

        Args:
            other: Right-hand operand — ``int``, :class:`Fraction`, or
                :class:`Exponent`.

        Returns:
            The canonicalized product, or ``NotImplemented`` for
            unsupported operand types.

        Raises:
            UnitError: When both operands carry symbol terms (non-linear
                product). The resolver catches this and falls back to
                ``D1.4``.
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
        """Right-side scalar multiplication.

        Args:
            other: Left-hand ``int`` or :class:`Fraction` operand.

        Returns:
            The canonicalized product.
        """
        return self.__mul__(other)

    # ---- presentation ----------------------------------------------------

    def __str__(self) -> str:
        """Render as a human-readable linear form.

        Terms are joined by ``+`` / ``-`` with their canonical sign;
        a unit coefficient is elided (``kappa`` rather than ``1·kappa``).
        Pure-zero renders as ``"0"``.

        Returns:
            String representation of the linear form.
        """
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
    """Base error for the unit-algebra layer.

    Raised by parser, smart constructors, and the algebra dispatch when
    an operation is well-formed at the Python level but ill-formed under
    the unit-algebra rules.
    """


class UnknownUnitError(UnitError):
    """Raised when an identifier is not in the unit table."""


class UnitAmbiguityWarning(UserWarning):
    """Warning emitted for ambiguous slash-precedence in a unit expression.

    Fired by the parser when a ``/`` at a given paren depth is followed
    by another ``*`` or ``/`` at the same depth (e.g. ``kg/m/s``), since
    different conventions parse this differently. Parentheses disambiguate.
    """


def _canonicalize_tyvars(
    items: tuple[tuple[str, Number | Exponent], ...],
) -> tuple[tuple[str, Exponent], ...]:
    """Canonicalize a ``tyvars`` mapping (mirrors :meth:`Exponent.build`).

    Aggregates duplicates by summing exponents, drops zero-exponent
    entries, sorts by name, and promotes raw ``Number`` exponents to
    :class:`Exponent`.

    Args:
        items: Tuple of ``(name, exponent)`` pairs; exponents may be
            ``int``, :class:`Fraction`, or :class:`Exponent`.

    Returns:
        Tuple of ``(name, non-zero Exponent)`` pairs sorted by name —
        the canonical form ``Unit.tyvars`` must hold.
    """
    agg: dict[str, Exponent] = {}
    for name, exp in items:
        e = exp if isinstance(exp, Exponent) else Exponent.from_value(exp)
        if name in agg:
            agg[name] = agg[name] + e
        else:
            agg[name] = e
    return tuple((n, agg[n]) for n in sorted(agg) if not agg[n].is_zero())


@dataclass(frozen=True)
class Unit:
    """Dimension vector (one Exponent per SI base slot) plus a rational prefactor.

    The dimension slots historically held :class:`Number` (``int`` /
    :class:`Fraction`). As of the symbolic-exponents work, each slot is
    an :class:`Exponent` — a linear form over Q with named opaque
    generators. Existing callers passing ``int`` / :class:`Fraction`
    slots still work: :meth:`__post_init__` promotes scalar entries
    automatically, coerces ``factor`` / ``offset`` to :class:`Fraction`,
    and canonicalizes ``tyvars``.

    Attributes:
        dimension: Seven-tuple of :class:`Exponent` over the SI base
            slots ``(M, L, T, Theta, I, N, J)``. Legacy callers may pass
            raw ``Number`` entries; they are promoted in
            :meth:`__post_init__`.
        factor: Multiplicative prefactor (prefix / conversion scale) as
            a :class:`Fraction`.
        offset: Affine zero-point shift versus the base unit. A non-zero
            ``offset`` marks an *absolute* affine quantity (e.g. degC
            with offset ``273.15``); conversion to base is
            ``x_base = factor*x + offset``. ``offset == 0`` is the
            *ordinary* case (every non-affine unit, absolute K, and
            every temperature *difference*), and the multiplicative
            algebra is unaffected. See ``docs/design/scale.md``
            §3.2–§3.3. Defaults to ``0``.
        tyvars: Parametric-polymorphism type-variable exponents
            (Kennedy-style AG-extension). Each entry is
            ``(name, Exponent)`` — the exponent is symbolic in the same
            way the SI slots are, so ``'a^κ`` composes with the
            symbolic-exponent machinery for free. Empty by default;
            every pre-polymorphism caller sees byte-identical
            behaviour. See ``docs/design/shipped/polymorphic-units.md``.
    """
    dimension: tuple[Exponent, ...]
    factor: Fraction
    # Affine zero-point shift vs the base unit (Phase 2 / scale offset).
    # ``offset != 0`` marks an *absolute* affine quantity (e.g. degC,
    # offset 273.15); ``offset == 0`` is *ordinary* (every other unit,
    # absolute K, and every temperature *difference*). Conversion to
    # base: ``x_base = factor*x + offset``. Defaults to 0, so all existing
    # callers and the multiplicative algebra are unaffected. See
    # docs/design/scale.md §3.2–§3.3.
    offset: Fraction = Fraction(0)
    tyvars: tuple[tuple[str, Exponent], ...] = ()

    def __post_init__(self) -> None:
        """Coerce, validate, and canonicalize the dataclass fields.

        Promotes raw ``Number`` dimension slots to :class:`Exponent`,
        coerces ``factor`` / ``offset`` to :class:`Fraction`, and
        canonicalizes ``tyvars`` (drop-zero, sort-by-name, promote raw
        exponents).

        Raises:
            UnitError: If ``dimension`` does not have exactly
                :data:`DIM_LEN` slots.
        """
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
        # Coerce factor and offset too.
        if not isinstance(self.factor, Fraction):
            object.__setattr__(self, "factor", Fraction(self.factor))
        if not isinstance(self.offset, Fraction):
            object.__setattr__(self, "offset", Fraction(self.offset))
        # Canonicalize tyvars: drop zero-exponent entries, sort by name,
        # promote any Number exponents. Equality / hash depend on the
        # canonical form.
        if self.tyvars:
            canon = _canonicalize_tyvars(self.tyvars)
            if canon != self.tyvars:
                object.__setattr__(self, "tyvars", canon)

    def __mul__(self, other: Unit) -> Unit:
        """Multiply two Units (slot-wise exponent sum, factor product).

        Args:
            other: Right-hand :class:`Unit`.

        Returns:
            The product unit; ``tyvars`` are concatenated and
            canonicalized by :meth:`__post_init__`.
        """
        return Unit(
            tuple(a + b for a, b in zip(self.dimension, other.dimension, strict=False)),
            self.factor * other.factor,
            tyvars=self.tyvars + other.tyvars,
        )

    def __truediv__(self, other: Unit) -> Unit:
        """Divide two Units (slot-wise exponent difference, factor quotient).

        Args:
            other: Right-hand (divisor) :class:`Unit`.

        Returns:
            The quotient unit; the divisor's ``tyvars`` exponents are
            negated and merged. Canonicalization in
            :meth:`__post_init__` drops anything that cancels to zero.
        """
        # Negate the divisor's tyvar exponents and merge. Canonicalization
        # in __post_init__ drops anything that cancels to zero.
        neg_tyvars = tuple((n, -e) for n, e in other.tyvars)
        return Unit(
            tuple(a - b for a, b in zip(self.dimension, other.dimension, strict=False)),
            self.factor / other.factor,
            tyvars=self.tyvars + neg_tyvars,
        )

    def pow(self, exp: Number | Exponent) -> Unit:
        """Raise the unit to a power.

        The result's dimension is the slot-wise product of the current
        :class:`Exponent` and ``exp``. Tyvar exponents are scaled by the
        same ``exp`` — ``('a)^k`` → ``'a^k``. Symbolic ``exp`` on a
        tyvar whose exponent is itself symbolic would be non-linear;
        :meth:`Exponent.__mul__` raises and the caller (:func:`power`)
        falls back to ``D1.4`` (same path as the SI-slot case).

        Args:
            exp: Literal :class:`Number` (the legacy path) or a
                symbolic :class:`Exponent`.

        Returns:
            The exponentiated unit.

        Raises:
            UnitError: When the multiplication is non-linear (both sides
                have symbol terms), or when ``exp`` is non-integer on a
                prefixed / scaled factor (``factor != 1``) — would
                generally not stay rational, so the algebra refuses.
                The caller (:func:`power`) converts both cases into a
                ``D1.4`` diagnostic.
        """
        new_dim = tuple(a * exp for a in self.dimension)
        new_tyvars = tuple((n, e * exp) for n, e in self.tyvars)
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
        return Unit(new_dim, new_factor, tyvars=new_tyvars)


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
    """Unit tagged as residing in log space (spec §1.2).

    Use :func:`wrap_log` to construct, since it applies the R2/R3
    canonicalization rules; direct construction bypasses them.

    Attributes:
        inner: The wrapped :class:`UnitExpr`.
    """
    inner: UnitExpr


@dataclass(frozen=True)
class ExpWrap:
    """Unit tagged as residing in exp space (spec §1.2).

    Use :func:`wrap_exp` to construct, since it applies the R2/R3
    canonicalization rules; direct construction bypasses them.

    Attributes:
        inner: The wrapped :class:`UnitExpr`.
    """
    inner: UnitExpr


UnitExpr = Unit | LogWrap | ExpWrap


def is_dimensionless(u: UnitExpr) -> bool:
    """Return ``True`` iff ``u`` has empty dimension and no tyvars.

    Wrappers around dimensionless never exist post-canonicalization
    (R2.3), so a wrapper is by definition non-dimensionless. A
    :class:`Unit` with no SI exponents but a live tyvar (``'a``) is
    **not** dimensionless — its dimension is the symbolic tyvar.

    Args:
        u: The unit expression to test.

    Returns:
        ``True`` iff ``u`` is a :class:`Unit` with all base exponents
        zero and an empty ``tyvars`` tuple.
    """
    return (
        isinstance(u, Unit)
        and all(d.is_zero() for d in u.dimension)
        and not u.tyvars
    )


def wrap_log(u: UnitExpr) -> UnitExpr:
    """Construct ``LOG(u)`` with canonicalization (R3.1 + R2.1 + R2.3).

    Args:
        u: Inner unit expression.

    Returns:
        ``u.inner`` if ``u`` is an :class:`ExpWrap` (R2.1); ``u`` itself
        if ``u`` is dimensionless (R2.3); otherwise a fresh
        :class:`LogWrap` around ``u`` (R3.1).
    """
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
    """Construct ``EXP(u)`` with canonicalization (R3.2 + R2.2 + R2.3).

    Args:
        u: Inner unit expression.

    Returns:
        ``u.inner`` if ``u`` is a :class:`LogWrap` (R2.2); ``u`` itself
        if ``u`` is dimensionless (R2.3); otherwise a fresh
        :class:`ExpWrap` around ``u`` (R3.2).
    """
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
    """Build a :class:`Unit` from a raw dimension tuple and prefactor.

    Internal convenience used by the parser and the default unit-table
    setup; promotes scalar slots to :class:`Exponent` so static typing
    sees a uniform tuple (``Unit.__post_init__`` would coerce at
    runtime regardless).

    Args:
        dim: Seven-tuple of dimension exponents.
        factor: Prefactor scale. Defaults to ``1``.

    Returns:
        The constructed :class:`Unit`.
    """
    # Promote any scalar slots to Exponent so the type matches Unit.dimension
    # (Unit.__post_init__ would coerce at runtime regardless).
    promoted = tuple(
        d if isinstance(d, Exponent) else Exponent.from_value(d) for d in dim
    )
    return Unit(promoted, Fraction(factor))


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
# All wrapper rules are implemented — R6.* for ExpWrap operands and
# R7.* for the LogWrap/ExpWrap cross-cases.


def _logwrap_inner_pow(
    inner: UnitExpr, k: Number | Exponent,
) -> UnitExpr | None:
    """Compute ``inner ^ k`` for use under a :class:`LogWrap` (R5.4 inner side).

    Args:
        inner: Inner unit expression. Only :class:`Unit` is handled;
            nested-wrapper inners return ``None``.
        k: Exponent — plain :class:`Number` (literal rational) or a
            symbolic :class:`Exponent` (linear form over named
            dimensionless generators).

    Returns:
        The exponentiated unit, or ``None`` if ``inner`` is not a
        :class:`Unit` or if :meth:`Unit.pow` raised :class:`UnitError`
        (e.g. non-linear multiplication of symbolic exponents). The
        caller falls back to ``D1.4`` in the latter case.
    """
    if not isinstance(inner, Unit):
        return None
    try:
        return inner.pow(k)
    except Exception:
        return None


def _result_offset(op: str, oa: Fraction, ob: Fraction) -> Fraction:
    """Compute the result offset for ``a <op> b`` under the affine algebra.

    Pure propagation — *validity* (e.g. point+point) is flagged
    separately at the emission site, so the value returned for an
    ill-defined combination is a harmless placeholder. See
    ``docs/design/scale.md`` §3.3.

    Args:
        op: Binary operator — ``"+"``, ``"-"``, ``"*"``, ``"/"``.
        oa: ``a``'s offset.
        ob: ``b``'s offset.

    Returns:
        The result offset:

        - ``"+"``: point+vector → the point's offset; ordinary → ``0``.
        - ``"-"``: point−vector → the point's offset; point−point
          (equal) → ``0`` (a difference); else ``0``.
        - ``"*"`` / ``"/"`` / ``"**"``: handled by the operators
          (result offset ``0``).
    """
    if op == "+":
        if ob == 0:
            return oa
        if oa == 0:
            return ob
        # point + point — ill-defined (flagged at the site). Keep ``oa`` so
        # the result stays absolute and doesn't *cascade* a second
        # (spurious) offset_mismatch at the enclosing assignment.
        return oa
    if op == "-":
        return oa if ob == 0 else Fraction(0)
    return Fraction(0)


def combine(
    op: str,
    a: UnitExpr,
    b: UnitExpr,
    *,
    a_literal: Number | Exponent | None = None,
    b_literal: Number | Exponent | None = None,
) -> tuple[UnitExpr | None, str | None]:
    """Apply binary op ``a <op> b`` at the unit-algebra level.

    Single dispatch entry point for §4–§7 of the spec, so the checker
    doesn't have to keep the rules in sync across two recursive walks.

    Args:
        op: Operator string — one of ``"+"``, ``"-"``, ``"*"``, ``"/"``.
        a: Left-hand unit expression.
        b: Right-hand unit expression.
        a_literal: Resolved literal value of ``a``'s source operand when
            it is a pure numeric literal, a PARAMETER reference, or a
            symbolic linear :class:`Exponent` (used by R5.4 to apply
            the log-power identity ``γ · LOG(u) = LOG(u^γ)``); else
            ``None``.
        b_literal: As ``a_literal``, for the right-hand operand.

    Returns:
        ``(result_unit_or_None, diag_code_or_None)``. A non-``None``
        diagnostic code with a ``None`` result means the op is
        undefined (rule error); a non-``None`` result with no diag
        code is success. Diagnostic codes are bare strings —
        ``"D1.1"`` / ``"D1.2"`` / ``"D1.3"`` / ``"D1.4"`` / ``"D1.5"``;
        the caller maps each to a concrete :class:`Diagnostic` with
        AST position.
    """
    from dimfort.core.trace import trace_step

    def _ok(rule_id: str, result: UnitExpr) -> tuple[UnitExpr, None]:
        """Record a successful rule fire and return ``(result, None)``."""
        trace_step(rule_id, (a, b), result)
        return result, None

    def _err(rule_id: str, diag: str) -> tuple[None, str]:
        """Record a failed rule fire and return ``(None, diag_code)``."""
        trace_step(rule_id, (a, b), None)
        return None, diag
    # ---- Regular × Regular (§4) ----
    if isinstance(a, Unit) and isinstance(b, Unit):
        if op in ("+", "-"):
            if equal_dim(a, b):
                # Offset-0 (the overwhelming majority, and every Phase-1
                # case) returns ``a`` unchanged — byte-identical. Only an
                # affine operand (offset != 0) triggers offset propagation.
                if a.offset == 0 and b.offset == 0:
                    return _ok("R4.1", a)
                return _ok(
                    "R4.1",
                    Unit(a.dimension, a.factor,
                         _result_offset(op, a.offset, b.offset),
                         tyvars=a.tyvars),
                )
            # H010 (implicit-cast demotion) fires only when the operand
            # was a source-level numeric literal (or a PARAMETER ref
            # that folded to a Number). A symbolic Exponent — e.g. a
            # dim'less *variable* reference — is an explicit dim'less
            # declaration, not a silent cast, so it should fire H002
            # not H010.
            a_is_numeric_literal = isinstance(a_literal, (int, Fraction))
            b_is_numeric_literal = isinstance(b_literal, (int, Fraction))
            # A literal 0 is the additive identity in *every* dimension
            # (0 m = 0 s = 0): it adopts the dimensioned operand's unit
            # silently — no implicit-cast warning, since there is nothing
            # to promote to a PARAMETER. Only value 0 earns this; a
            # non-zero literal (e.g. 273.15) still fires D1.5 because its
            # hidden unit is a real smell (the #006 K-literal family).
            a_is_zero = a_is_numeric_literal and a_literal == 0
            b_is_zero = b_is_numeric_literal and b_literal == 0
            if a_is_zero and is_dimensionless(a) and not is_dimensionless(b):
                return _ok("R4.1", b)
            if b_is_zero and is_dimensionless(b) and not is_dimensionless(a):
                return _ok("R4.1", a)
            if a_is_numeric_literal and is_dimensionless(a) and not is_dimensionless(b):
                return b, "D1.5"
            if b_is_numeric_literal and is_dimensionless(b) and not is_dimensionless(a):
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
                # Symbolic divisor: 1/κ isn't a linear form over Q with
                # named generators (it's a rational function), so it
                # doesn't fit our Exponent algebra. Refuse explicitly.
                if isinstance(b_literal, Exponent) and not b_literal.is_constant():
                    return _err("R5.5", "D1.4")
                # Normalise constant Exponent → its rational value.
                b_val: Number
                if isinstance(b_literal, Exponent):
                    bf = b_literal.as_fraction()
                    if bf is None or bf == 0:
                        return None, None
                    b_val = bf
                else:
                    b_val = b_literal
                inv = (
                    Fraction(1, b_val)
                    if isinstance(b_val, int)
                    else Fraction(1) / b_val
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
       dimensionless. ``base ^ Rn``, ``base ^ Ln``, ``base ^ En`` all
       error with ``D1.7`` regardless of ``base``. The mathematical
       reading via ``a^b = exp(b·log(a))`` would give a typed
       :class:`ExpWrap` result, but in practice ``2.0 ** speed``-style
       expressions are virtually always bugs; the gate surfaces them
       at the power site. (``D1.7`` defaults to a warning, so projects
       that genuinely live in exp-tagged space can opt out or be
       tolerated by default.)
    2. **Base-specific value gate** — once the exponent is known to be
       dimensionless, the base determines whether the value matters:

       - ``Rd`` (dimensionless): result is always ``Rd``. ``0·k = 0``
         for any ``k`` — literal, non-literal, integer, irrational.
       - ``Rn``: result is ``Rn(k·t)`` if the exponent value is a
         known literal rational; ``D1.4`` if not (classic Exner
         ``p^kappa`` pattern needing OQ4 to resolve precisely).
       - ``Ln``: result is ``Ln`` only for the trivial identity
         ``k = 1`` (R5.9); otherwise ``D1.2``.
       - ``En``: ``ExpWrap(k·U)`` if ``k`` is known literal (R6.4);
         ``D1.4`` if not.

    Args:
        base: Resolved unit of the base expression.
        exponent_unit: Resolved unit of the exponent expression, or
            ``None`` if the checker couldn't determine it — usually
            because the variable lacks an annotation. U005 surfaces
            that underlying issue, so ``D1.7`` is **not** fired on
            unknown-unit exponents.
        exponent_value: Literal rational (or symbolic
            :class:`Exponent`) extracted at the AST level, or ``None``
            for non-literal expressions.

    Returns:
        ``(result_unit_or_None, diag_code_or_None)`` — same shape as
        :func:`combine`.
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
        except UnitError:
            # ``Unit.pow`` raises UnitError for non-linear / non-rational
            # combinations the algebra can't represent (e.g. a symbolic
            # exponent on a unit with symbolic SI dimensions). That's
            # exactly the D1.4 "needs OQ4" surface — fire it explicitly
            # rather than returning ``(None, None)`` which the checker
            # would silently treat as "unknown unit, do nothing." Other
            # exceptions (TypeError / AttributeError on a malformed
            # input) should keep propagating — they're contract bugs.
            return None, "D1.4"

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
        wrapped = wrap_exp(new_inner)
        trace_step("R6.4", (base,), wrapped)
        return wrapped, None

    return None, None


@dataclass(frozen=True)
class UnitTable:
    """Resolved unit table — the parser's lookup source.

    Populated at import time from ``default_units.toml`` plus any
    project overrides; see :mod:`dimfort.core.unit_config`.

    Attributes:
        base: Mapping from canonical base-unit symbol to its
            :class:`Unit`.
        derived: Mapping from canonical derived-unit symbol to its
            :class:`Unit` (combinations of base units, possibly with
            non-unit ``factor`` / ``offset``).
        prefixable: Set of unit names that allow SI-prefix expansion
            (``kg``, ``s``, …).
        prefixes: Mapping from prefix string (``"k"``, ``"m"``, …) to
            its multiplicative :class:`Fraction`.
    """

    base: dict[str, Unit]
    derived: dict[str, Unit]
    prefixable: frozenset[str]   # names that allow prefix expansion
    prefixes: dict[str, Fraction]


# Populated by :mod:`dimfort.core.unit_config` at import time so callers can
# do ``parse(expr)`` without threading a table through.
DEFAULT_TABLE: UnitTable | None = None


def _resolve_identifier(name: str, table: UnitTable) -> Unit:
    """Resolve a unit identifier against a :class:`UnitTable`.

    Tries direct base / derived lookup first, then falls back to
    prefix expansion for any ``prefixable`` base or derived name.

    Args:
        name: Unit identifier as it appeared in the source.
        table: Active unit table.

    Returns:
        The resolved :class:`Unit`.

    Raises:
        UnknownUnitError: If ``name`` is not in the table and no
            prefix expansion succeeds.
    """
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
    \s+                              |  # whitespace
    (?P<TYVAR>'[A-Za-z][A-Za-z0-9]*) |  # OCaml-style type variable
    (?P<ID>[A-Za-z][A-Za-z0-9]*)     |
    (?P<INT>\d+)                     |
    (?P<POW>\*\*)                    |  # Fortran-style power, normalised to ^
    (?P<OP>[*/^()+\-])               |  # +/- needed inside paren'd exp linear forms
    (?P<BAD>.)
    """,
    re.VERBOSE,
)


def _tokenize(expr: str) -> list[tuple[str, str]]:
    """Tokenize a unit expression.

    The lexer recognises type-variables (``'a``), identifiers, integer
    literals, the Fortran-style ``**`` power operator (normalised to
    ``^``), and the single-character operators ``*/^()-``. Whitespace
    is skipped. The token stream is terminated by an ``("END", "")``
    sentinel.

    Args:
        expr: Source expression text.

    Returns:
        List of ``(kind, lexeme)`` pairs.

    Raises:
        UnitError: On any character outside the recognised classes.
    """
    tokens: list[tuple[str, str]] = []
    for m in _TOKEN_RE.finditer(expr):
        if m.group("TYVAR") is not None:
            # The leading ``'`` is part of the canonical tyvar name —
            # carrying it through avoids any ambiguity with concrete
            # base-unit identifiers downstream.
            tokens.append(("TYVAR", m.group("TYVAR")))
        elif m.group("ID") is not None:
            tokens.append(("ID", m.group("ID")))
        elif m.group("INT") is not None:
            tokens.append(("INT", m.group("INT")))
        elif m.group("POW") is not None:
            # ``**`` is Fortran's power operator. Normalise to ``^`` so
            # the parser's single power path (parse_term) handles both
            # ``m**2`` and ``m^2`` identically — including under ``/``
            # (``kg/m**3`` now parses as ``kg/(m**3)``).
            tokens.append(("OP", "^"))
        elif m.group("OP") is not None:
            tokens.append(("OP", m.group("OP")))
        elif m.group("BAD") is not None:
            raise UnitError(f"unexpected character {m.group('BAD')!r} in {expr!r}")
    tokens.append(("END", ""))
    return tokens


def _negate_exp(e: Number | Exponent) -> Number | Exponent:
    """Negate a parsed exponent value.

    Used by the unary-minus branch of :meth:`_Parser.parse_exp`.
    Number values negate via ``-`` directly; :class:`Exponent` is
    rebuilt with every coefficient and the constant flipped.
    """
    if isinstance(e, Exponent):
        return Exponent.build(
            {n: -c for n, c in e.terms},
            -e.constant,
        )
    return -e


class _Parser:
    """Recursive-descent parser for ``@unit{}`` expressions.

    Grammar (post-0.2.7 §3.0 + symbolic-exponent widening — both ship
    unconditionally, no flags)::

        unit       = term (('*' | '/') term)*
        term       = factor ('^' exp)?
        factor     = ident | tyvar | '(' unit ')' | INT (=1)
        exp        = signed_atom | '(' linear_form ')' | '-' exp
        signed_atom = ('+' | '-')? atom
        atom       = INT | IDENT
        linear_form = lin_term (('+' | '-') lin_term)*
        lin_term   = (INT ('/' INT)?) ('*' IDENT)? | IDENT

    The exponent surface accepts every shape the :class:`Exponent`
    algebra represents: integers (``m^2``, ``m^-1``), paren'd
    integers (``m^(2)``, ``m^(-1)``), paren'd rationals
    (``m^(2/3)``), bare identifiers (``m^kappa``), paren'd
    identifiers and linear forms over Q with identifier generators
    (``m^(2*kappa - 1/3)``, ``m^(kappa - lambda)``). Identifier
    resolution is deferred to the checker, which uses the same
    symbol-table path that handles variable-as-exponent in source
    expressions.

    ``/`` is left-associative, same precedence as ``*``. When a ``/``
    is followed at the same paren depth by another ``*`` or ``/``,
    :class:`UnitAmbiguityWarning` is emitted.
    """

    def __init__(self, tokens: list[tuple[str, str]], table: UnitTable):
        """Initialise the parser.

        Args:
            tokens: Token stream produced by :func:`_tokenize`.
            table: Active :class:`UnitTable` for identifier resolution.
        """
        self.tokens = tokens
        self.i = 0
        self.table = table

    def peek(self) -> tuple[str, str]:
        """Return the next token without consuming it.

        Returns:
            The ``(kind, lexeme)`` pair at the current cursor.
        """
        return self.tokens[self.i]

    def consume(self) -> tuple[str, str]:
        """Consume and return the next token, advancing the cursor.

        Returns:
            The ``(kind, lexeme)`` pair that was at the cursor.
        """
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        """Consume the next token, asserting its kind (and optionally lexeme).

        Args:
            kind: Expected token kind.
            value: Optional expected lexeme.

        Returns:
            The consumed token.

        Raises:
            UnitError: If the next token does not match.
        """
        tok = self.peek()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise UnitError(f"expected {kind} {value!r}, got {tok}")
        return self.consume()

    def parse_unit(self) -> UnitExpr:
        """Parse the top-level ``unit`` production.

        Returns:
            The parsed :class:`UnitExpr`. The result is a :class:`Unit`
            unless a ``LOG(...)`` / ``EXP(...)`` wrapper is the entire
            expression (arithmetic between wrapped units is not
            supported in annotation syntax).

        Raises:
            UnitError: On ill-formed input — including ``*`` / ``/``
                applied between :class:`LogWrap` / :class:`ExpWrap`
                operands.
        """
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
        """Parse a ``term`` (factor with optional ``^`` exponent).

        Returns:
            The parsed :class:`UnitExpr`.

        Raises:
            UnitError: If a power is applied to a wrapped unit (not
                expressible in annotation syntax).
        """
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
        """Parse a ``factor`` (identifier, ``(unit)``, ``1``, or tyvar).

        ``LOG(...)`` / ``EXP(...)`` wrappers shadow any same-named unit
        identifier (case-insensitive per A2).

        Returns:
            The parsed :class:`UnitExpr`.

        Raises:
            UnitError: On any other integer literal than ``1`` or any
                unexpected token.
        """
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
        if tok[0] == "TYVAR":
            # ``'a`` is a Unit with all-zero SI exponents whose only
            # active basis element is the tyvar itself with exponent 1.
            self.consume()
            zero = Exponent.from_value(0)
            return Unit(
                (zero, zero, zero, zero, zero, zero, zero),
                Fraction(1),
                tyvars=((tok[1], Exponent.from_value(1)),),
            )
        raise UnitError(f"expected unit factor, got {tok}")

    def parse_exp(self) -> Number | Exponent:
        """Parse an exponent.

        Accepts every shape the :class:`Exponent` algebra represents:
        signed integers (``2``, ``-1``), paren'd signed integers and
        rationals (``(2)``, ``(-1)``, ``(2/3)``), bare identifiers
        (``kappa``, ``-kappa``), paren'd identifiers and linear forms
        with rational coefficients (``(2*kappa - 1/3)``,
        ``(kappa - lambda)``).

        Returns:
            ``int`` / :class:`Fraction` when the parsed exponent
            reduces to a constant; :class:`Exponent` when it carries
            identifier generators (resolved at check time against the
            file's PARAMETER table via the existing source-side path).

        Raises:
            UnitError: On non-linear shapes (cross-product of
                identifiers, identifier as denominator), float
                coefficients, or any other ill-formed token sequence.
        """
        tok = self.peek()
        if tok == ("OP", "-"):
            self.consume()
            inner = self.parse_exp()
            return _negate_exp(inner)
        if tok == ("OP", "+"):
            self.consume()
            return self.parse_exp()
        if tok == ("OP", "("):
            self.consume()
            result = self._parse_paren_exp_body()
            self.expect("OP", ")")
            return result
        if tok[0] == "INT":
            return int(self.consume()[1])
        if tok[0] == "ID":
            name = self.consume()[1]
            return Exponent.build({name: Fraction(1)}, Fraction(0))
        raise UnitError(f"expected exponent, got {tok}")

    def _parse_paren_exp_body(self) -> Number | Exponent:
        """Parse the inside of ``( … )`` in an exponent position.

        Builds a linear form over Q with identifier generators.
        Returns an ``int`` or :class:`Fraction` when no identifier
        generators appear (a pure-constant exponent), otherwise an
        :class:`Exponent`.
        """
        terms: dict[str, Fraction] = {}
        constant = Fraction(0)
        # Optional leading sign on the first term.
        sign = Fraction(1)
        if self.peek() == ("OP", "-"):
            self.consume()
            sign = Fraction(-1)
        elif self.peek() == ("OP", "+"):
            self.consume()
        while True:
            term_const, term_terms = self._parse_lin_term()
            constant += sign * term_const
            for name, coeff in term_terms.items():
                terms[name] = terms.get(name, Fraction(0)) + sign * coeff
            nxt = self.peek()
            if nxt == ("OP", "+"):
                self.consume()
                sign = Fraction(1)
                continue
            if nxt == ("OP", "-"):
                self.consume()
                sign = Fraction(-1)
                continue
            break
        # Drop zero-coefficient terms so an expression like
        # ``kappa - kappa`` collapses to a pure constant cleanly.
        terms = {n: c for n, c in terms.items() if c != 0}
        if not terms:
            if constant.denominator == 1:
                return int(constant)
            return constant
        # ``Exponent.build`` declares ``dict[str, int | Fraction]``; our
        # local dict is the invariant ``dict[str, Fraction]`` — pass via
        # tuple to side-step mypy's dict-invariance complaint without
        # changing the dict literal's type at every assignment site.
        return Exponent.build(tuple(terms.items()), constant)

    def _parse_lin_term(self) -> tuple[Fraction, dict[str, Fraction]]:
        """Parse one term of a linear form.

        Accepts ``INT`` (constant), ``INT '/' INT`` (rational),
        ``INT '*' IDENT`` / ``INT '/' INT '*' IDENT`` (coefficient
        times identifier), or bare ``IDENT``. Leading signs are
        consumed by :meth:`_parse_paren_exp_body`; this helper sees
        only the magnitude part.

        Returns:
            ``(constant_contribution, terms_dict)`` — the term's
            contribution split into its constant offset and its
            symbol coefficients.
        """
        tok = self.peek()
        if tok[0] == "INT":
            num = int(self.consume()[1])
            coef = Fraction(num)
            if self.peek() == ("OP", "/"):
                self.consume()
                den_tok = self.expect("INT")
                coef = Fraction(num, int(den_tok[1]))
            if self.peek() == ("OP", "*"):
                self.consume()
                ident_tok = self.expect("ID")
                return Fraction(0), {ident_tok[1]: coef}
            return coef, {}
        if tok[0] == "ID":
            ident = self.consume()[1]
            return Fraction(0), {ident: Fraction(1)}
        raise UnitError(
            f"expected term in exponent linear form, got {tok}"
        )


def base_symbols(table: UnitTable | None = None) -> tuple[str, ...]:
    """Return the base-unit symbols in dimension-slot order.

    Args:
        table: Active :class:`UnitTable`. ``None`` (the default) uses
            :data:`DEFAULT_TABLE`.

    Returns:
        Seven-tuple of unit symbols. Falls back to the SI slot name
        (``M``, ``L``, …) for any slot the active table doesn't cover.

    Raises:
        RuntimeError: If ``table`` is ``None`` and :data:`DEFAULT_TABLE`
            has not been initialised (i.e. :mod:`dimfort.core.unit_config`
            has not been imported).
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
    """Translate digits and ``-`` in ``s`` to Unicode superscript glyphs.

    Args:
        s: ASCII digit / sign string (e.g. ``"-2"``).

    Returns:
        The same string with each character mapped through
        :data:`_SUPERSCRIPTS`; characters outside the table pass
        through unchanged.
    """
    return "".join(_SUPERSCRIPTS.get(c, c) for c in s)


def format_unit(
    u: UnitExpr,
    *,
    show_factor: bool = False,
    show_offset: bool = True,
    table: UnitTable | None = None,
) -> str:
    """Render ``u`` as a human-readable expression.

    Uses Unicode superscripts (``²``, ``³``, …) for integer exponents.
    Unit symbols are joined by the SI middle dot ``·``; the numeric
    ``factor`` (when shown) is joined to the body by ``×`` so the
    separator distinguishes a scale factor from another base unit.
    Negative exponents render as signed superscripts (``K⁻¹``,
    ``kg·m·s⁻²``) rather than a ``/`` denominator. Rational exponents
    fall back to ``^(p/q)`` since superscript fractions look messy.
    :class:`LogWrap` / :class:`ExpWrap` print as ``LOG(...)`` /
    ``EXP(...)`` per spec §9.

    Args:
        u: The unit expression to render.
        show_factor: If ``True``, prepend the rational :class:`Fraction`
            ``factor`` (e.g. ``1000×kg``) when not ``1``. Defaults to
            ``False``.
        show_offset: If ``True`` (the default), append the affine
            zero-point shift for absolute units — ``degC`` reads
            ``K + 273.15`` rather than an indistinguishable ``K``. This
            renders independently of ``show_factor``. Turn off where the
            output must be valid ``@unit{}`` syntax (e.g. a
            copy-pasteable PARAMETER suggestion), since ``K + 273.15``
            is a description, not a parseable unit.
        table: Active :class:`UnitTable`. ``None`` uses
            :data:`DEFAULT_TABLE`.

    Returns:
        The rendered string. Offset-zero units (every non-affine unit)
        are byte-identical to the pre-affine rendering.
    """
    if isinstance(u, LogWrap):
        inner = format_unit(
            u.inner, show_factor=show_factor, show_offset=show_offset, table=table
        )
        return f"LOG({inner})"
    if isinstance(u, ExpWrap):
        inner = format_unit(
            u.inner, show_factor=show_factor, show_offset=show_offset, table=table
        )
        return f"EXP({inner})"
    names = base_symbols(table)
    terms: list[str] = []
    # Tyvars render first — they are the most syntactically distinctive
    # part of the unit (``'a·kg`` reads better than ``kg·'a``) and follow
    # the OCaml-convention reading order. Each tyvar uses the same
    # exponent rendering rules as the SI slots.
    for name, exp in u.tyvars:
        q = exp.as_fraction()
        if q is not None:
            if q == 1:
                term = name
            elif q.denominator == 1:
                term = name + _to_super(str(int(q)))
            else:
                term = f"{name}^({q})"
        else:
            term = f"{name}^({exp})"
        terms.append(term)
    for sym, exp in zip(names, u.dimension, strict=False):
        if exp.is_zero():
            continue
        # Each factor renders as ``sym`` raised to its *signed* exponent,
        # SI-style: a negative exponent becomes a superscript ``⁻n`` rather
        # than moving the factor into a ``/`` denominator (``1/K`` → ``K⁻¹``,
        # ``kg m/s²`` → ``kg·m·s⁻²``). Rational exponents fall back to the
        # bracketed ``^(p/q)`` form since superscript fractions look messy,
        # and symbolic exponents to ``^(<linear form>)`` since superscripts
        # can't express linear combinations.
        q = exp.as_fraction()
        if q is not None:
            if q == 1:
                term = sym
            elif q.denominator == 1:
                term = sym + _to_super(str(int(q)))
            else:
                term = f"{sym}^({q})"
        else:
            term = f"{sym}^({exp})"
        terms.append(term)
    body = "·".join(terms) if terms else "1"
    if show_factor and u.factor != 1:
        rendered = f"{u.factor}×{body}" if body != "1" else f"{u.factor}"
    else:
        rendered = body
    # Affine offset: append the zero-point shift for absolute units so
    # ``degC`` reads ``K + 273.15`` rather than an indistinguishable ``K``.
    # Rendered as a decimal (273.15), not the raw Fraction (5463/20).
    if show_offset and u.offset != 0:
        off = float(u.offset)
        sign = "+" if off >= 0 else "-"
        rendered = f"{rendered} {sign} {abs(off):g}"
    return rendered


def format_unit_source(u: UnitExpr, *, table: UnitTable | None = None) -> str:
    """Serialize ``u`` as a parseable ``@unit{}`` string.

    :func:`format_unit` is for *display* — Unicode superscripts, ``·``
    products, signed-exponent powers (``kg·m·s⁻²``) — and its output
    does **not** round-trip through :func:`parse`. This function emits
    the ASCII DSL the parser accepts (``*`` products, ``^`` powers, a
    ``/`` denominator: ``kg*m/s^2``) so the result can be written back
    into source as a ``@unit{...}`` annotation (e.g. the H010
    extract-to-PARAMETER quick-fix). The affine ``offset`` is dropped
    — an absolute unit's zero-point shift is not expressible in
    annotation syntax — so ``degC`` serializes to ``K``.

    Args:
        u: The unit expression to serialize.
        table: Active :class:`UnitTable`. ``None`` uses
            :data:`DEFAULT_TABLE`.

    Returns:
        ASCII unit-expression source compatible with :func:`parse`.
    """
    if isinstance(u, LogWrap):
        return f"LOG({format_unit_source(u.inner, table=table)})"
    if isinstance(u, ExpWrap):
        return f"EXP({format_unit_source(u.inner, table=table)})"
    names = base_symbols(table)
    pos_terms: list[str] = []
    neg_terms: list[str] = []
    # Tyvars go first so the source form mirrors ``format_unit``:
    # ``'a*kg`` rather than ``kg*'a``.
    for name, exp in u.tyvars:
        q = exp.as_fraction()
        if q is not None:
            mag = abs(q)
            if mag == 1:
                term = name
            elif mag.denominator == 1:
                term = f"{name}^{int(mag)}"
            else:
                term = f"{name}^({mag})"
            (pos_terms if q > 0 else neg_terms).append(term)
        else:
            pos_terms.append(f"{name}^({exp})")
    for sym, exp in zip(names, u.dimension, strict=False):
        if exp.is_zero():
            continue
        q = exp.as_fraction()
        if q is not None:
            mag = abs(q)
            if mag == 1:
                term = sym
            elif mag.denominator == 1:
                term = f"{sym}^{int(mag)}"
            else:
                term = f"{sym}^({mag})"
            (pos_terms if q > 0 else neg_terms).append(term)
        else:
            pos_terms.append(f"{sym}^({exp})")
    body = "*".join(pos_terms) if pos_terms else "1"
    if neg_terms:
        denom = "*".join(neg_terms)
        if len(neg_terms) > 1:
            denom = f"({denom})"
        body = f"{body}/{denom}"
    return body


def parse(expr: str, table: UnitTable | None = None) -> UnitExpr:
    """Parse a unit-expression string against a :class:`UnitTable`.

    Args:
        expr: Source expression text (the body of an ``@unit{...}``
            annotation, or an equivalent inline string).
        table: Active :class:`UnitTable`. ``None`` (the default) uses
            :data:`DEFAULT_TABLE`.

    Returns:
        The parsed :class:`UnitExpr`.

    Raises:
        UnitError: On any lex / parse failure or trailing input.
        UnknownUnitError: If an identifier is not in the table.
        RuntimeError: If ``table`` is ``None`` and :data:`DEFAULT_TABLE`
            has not been initialised.
    """
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
    """Structural dimension-equality on the :class:`UnitExpr` tree.

    Two :class:`Unit` leaves compare on their 7-tuples *and* on their
    tyvar maps (``factor`` / ``offset`` ignored). A free type variable
    is part of the Unit's dimension under the AG-extension, so ``'a``
    and ``'b`` are **not** dim-equal even if both have zero SI slots.
    Two :class:`LogWrap` (or two :class:`ExpWrap`) compare by
    recursing into ``inner``. A wrapper is never dim-equal to a leaf.

    Args:
        a: Left operand.
        b: Right operand.

    Returns:
        ``True`` iff ``a`` and ``b`` have the same dimension.
    """
    if isinstance(a, Unit) and isinstance(b, Unit):
        return tuple(a.dimension) == tuple(b.dimension) and a.tyvars == b.tyvars
    if isinstance(a, LogWrap) and isinstance(b, LogWrap):
        return equal_dim(a.inner, b.inner)
    if isinstance(a, ExpWrap) and isinstance(b, ExpWrap):
        return equal_dim(a.inner, b.inner)
    return False


def equal_strict(a: UnitExpr, b: UnitExpr) -> bool:
    """Compare ``a`` and ``b`` including ``factor`` and ``offset``.

    Like :func:`equal_dim` but :class:`Unit` leaves also compare
    ``factor`` and ``offset`` — equivalent to ``Unit.__eq__`` on the
    leaves. Previously omitted offset, which let
    ``equal_strict(degC, K)`` return ``True`` even though
    ``degC == K`` returned ``False``; that broke the invariant the
    rest of the codebase relies on (two Units that compare equal under
    ``Unit.__eq__`` must compare equal under :func:`equal_strict` and
    vice versa).

    Args:
        a: Left operand.
        b: Right operand.

    Returns:
        ``True`` iff ``a`` and ``b`` are equal under the strict
        (dimension + factor + offset) comparison.
    """
    if isinstance(a, Unit) and isinstance(b, Unit):
        return (
            equal_dim(a, b)
            and a.factor == b.factor
            and a.offset == b.offset
        )
    if isinstance(a, LogWrap) and isinstance(b, LogWrap):
        return equal_strict(a.inner, b.inner)
    if isinstance(a, ExpWrap) and isinstance(b, ExpWrap):
        return equal_strict(a.inner, b.inner)
    return False


@dataclass(frozen=True)
class Verdict:
    """Structured result of comparing two unit expressions (scale layer).

    Representation-only: it reports *what* differs, never *how severe*.
    The ``scale_mode`` gate and per-code severity live at the call
    sites, not here — so soft-units can later remap severity without
    touching this function. See ``docs/design/scale.md``.

    Attributes:
        kind: One of ``"equal"`` (same dimension, ``factor``, and
            ``offset``), ``"dim_mismatch"`` (base dimensions differ —
            today's H001 / H002 case), ``"scale_mismatch"`` (same
            dimension, different ``factor`` — S001), or
            ``"offset_mismatch"`` (same dimension and ``factor``,
            different ``offset`` — e.g. ``K`` vs ``degC``, S002 path 1).
        ratio: For ``"scale_mismatch"``, ``a.factor / b.factor`` —
            the magnitude discrepancy. ``None`` otherwise.
        delta: For ``"offset_mismatch"``, ``a.offset - b.offset``.
            ``None`` otherwise.

    Note:
        :func:`compare` only sees offset *mismatches*; affine
        *operation-validity* failures (``degC + degC`` etc., where
        offsets are equal) are flagged in :func:`combine` /
        :func:`power`, not here — see ``docs/design/scale.md`` §4–§5.
    """

    kind: str
    ratio: Fraction | None = None
    delta: Fraction | None = None


def compare(a: UnitExpr, b: UnitExpr) -> Verdict:
    """Compare two unit expressions, returning a structured verdict.

    Dimension first, then multiplicative scale (``factor``), then
    affine ``offset``. Wrappers recurse into ``inner`` exactly as
    :func:`equal_dim`; a wrapper is never comparable to a leaf (→
    ``dim_mismatch``).

    Args:
        a: Left operand.
        b: Right operand.

    Returns:
        A :class:`Verdict` describing the relationship.

    Note:
        ``factor`` is checked **even when the dimension is** ``{1}`` —
        that is what lets ``g/kg`` (factor 1/1000) vs ``kg/kg``
        (factor 1) be caught. :func:`equal_dim` / :func:`equal_strict`
        are left as-is; this function is the scale layer's single
        source of truth.
    """
    if isinstance(a, Unit) and isinstance(b, Unit):
        if tuple(a.dimension) != tuple(b.dimension) or a.tyvars != b.tyvars:
            return Verdict("dim_mismatch")
        if a.factor != b.factor:
            return Verdict("scale_mismatch", ratio=a.factor / b.factor)
        if a.offset != b.offset:
            return Verdict("offset_mismatch", delta=a.offset - b.offset)
        return Verdict("equal")
    if isinstance(a, LogWrap) and isinstance(b, LogWrap):
        return compare(a.inner, b.inner)
    if isinstance(a, ExpWrap) and isinstance(b, ExpWrap):
        return compare(a.inner, b.inner)
    return Verdict("dim_mismatch")
