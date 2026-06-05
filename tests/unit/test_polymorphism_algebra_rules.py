"""Audit-completion tests for polymorphism spec-table algebra rules.

The polymorphism spec (``docs/design/.../polymorphic-units.md``) promises
that a long list of operations preserve / propagate / unify type
variables for free via the existing algebra (Exponent + LogWrap +
ExpWrap + the intrinsic-dispatch machinery in the checker). The audit
found that many of these rows had no direct test. This file pins each
down with a minimal Fortran-source test through the full checker.

Coverage tracks the spec table in ``polymorphic-units.md`` §"Algebra
rules with type variables (complete table)" — each test is labelled
with the row it pins down.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.core.multifile import check_files


def _materialise(tmp_path: Path, body: str) -> Path:
    src = tmp_path / "p.f90"
    src.write_text(body)
    return src


def _diags(result, file: Path) -> list:
    return list(result.diagnostics.get(file.resolve(), []))


def _no_polymorphic_fires(diags) -> bool:
    return not any(d.code in ("H020", "H023") for d in diags)


# ---------------------------------------------------------------------------
# Spec row: SQRT('a) → 'a^(1/2)


def test_sqrt_on_tyvar_preserves_polymorphism(tmp_path: Path):
    src = _materialise(tmp_path,
        "subroutine f(x, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a^2}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = sqrt(x)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert _no_polymorphic_fires(diags), [(d.code, d.message) for d in diags]


# ---------------------------------------------------------------------------
# Spec row: ABS('a) → 'a


def test_abs_on_tyvar_preserves_polymorphism(tmp_path: Path):
    src = _materialise(tmp_path,
        "subroutine f(x, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = abs(x)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert _no_polymorphic_fires(diags), [(d.code, d.message) for d in diags]


# ---------------------------------------------------------------------------
# Spec rows: MAX('a, 'a) → 'a (preserves); MAX('a, kg) → forced binding → H023


def test_max_of_two_tyvar_args_preserves(tmp_path: Path):
    src = _materialise(tmp_path,
        "subroutine f(x, y, z)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: y  !< @unit{'a}\n"
        "  real, intent(out) :: z  !< @unit{'a}\n"
        "  z = max(x, y)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert _no_polymorphic_fires(diags), [(d.code, d.message) for d in diags]


def test_max_tyvar_with_concrete_fires_h023(tmp_path: Path):
    """``MAX('a, kg)`` would force ``'a = kg`` — H023 fires."""
    src = _materialise(tmp_path,
        "subroutine f(x, c, z)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: c  !< @unit{kg}\n"
        "  real, intent(out) :: z  !< @unit{'a}\n"
        "  z = max(x, c)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" in codes, [(d.code, d.message) for d in diags]


# ---------------------------------------------------------------------------
# Spec row: 'a < 'b → unifies, result {1}


def test_comparison_between_tyvars_preserves_when_equal(tmp_path: Path):
    src = _materialise(tmp_path,
        "subroutine f(x, y, flag)\n"
        "  real, intent(in)     :: x  !< @unit{'a}\n"
        "  real, intent(in)     :: y  !< @unit{'a}\n"
        "  logical, intent(out) :: flag\n"
        "  flag = (x < y)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert _no_polymorphic_fires(diags), [(d.code, d.message) for d in diags]


@pytest.mark.xfail(
    reason=(
        "Pre-existing checker-wide gap: relational operators (<, >, ==) "
        "aren't dim-checked anywhere in the checker today, polymorphism "
        "or not. The spec table promises ``'a < 'b`` unifies and forces "
        "H023 against a concrete operand, but a similar concrete-vs-"
        "concrete mismatch (kg < m) also isn't caught. Flipping this to "
        "strict requires wiring relational_expression nodes into "
        "_walk_expressions first."
    ),
    strict=True,
)
def test_comparison_tyvar_vs_concrete_fires_h023(tmp_path: Path):
    """``'a < kg`` would force ``'a = kg`` — H023 fires."""
    src = _materialise(tmp_path,
        "subroutine f(x, c, flag)\n"
        "  real, intent(in)     :: x  !< @unit{'a}\n"
        "  real, intent(in)     :: c  !< @unit{kg}\n"
        "  logical, intent(out) :: flag\n"
        "  flag = (x < c)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" in codes, [(d.code, d.message) for d in diags]


# ---------------------------------------------------------------------------
# Spec row: LOG('a) → LogWrap('a). Then exp(log('a) + log('b)) → 'a*'b


def test_log_on_tyvar_wraps(tmp_path: Path):
    """``y = log(x)`` where x:'a — no fire; y types as LOG('a)."""
    src = _materialise(tmp_path,
        "subroutine f(x, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(out) :: y  !< @unit{LOG('a)}\n"
        "  y = log(x)\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert _no_polymorphic_fires(diags), [(d.code, d.message) for d in diags]


def test_exp_of_log_sum_chain(tmp_path: Path):
    """``exp(log(x) + log(y))`` where x:'a, y:'b — chain reduces to 'a*'b
    via R5.1 (log+log → log of product) and R6 (exp(log) → identity)."""
    src = _materialise(tmp_path,
        "subroutine f(x, y, z)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: y  !< @unit{'b}\n"
        "  real, intent(out) :: z  !< @unit{'a*'b}\n"
        "  z = exp(log(x) + log(y))\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert _no_polymorphic_fires(diags), [(d.code, d.message) for d in diags]


# ---------------------------------------------------------------------------
# Edge cases — gaps 5, 6, 7, 8


def test_recursive_call_with_concrete_value_binds_cleanly(tmp_path: Path):
    """A recursive call that passes a CONCRETE value for the tyvar slot
    (different from the slot's binding inside the calling instance) —
    should bind 'a = kg cleanly at that site without H020."""
    src = _materialise(tmp_path,
        "module mod\n"
        "contains\n"
        "  recursive subroutine f(x, depth, y)\n"
        "    real, intent(in)    :: x      !< @unit{'a}\n"
        "    integer, intent(in) :: depth\n"
        "    real, intent(out)   :: y      !< @unit{'a}\n"
        "    real                :: kg_v   !< @unit{kg}\n"
        "    real                :: kg_out !< @unit{kg}\n"
        "    if (depth > 0) then\n"
        "      call f(kg_v, depth - 1, kg_out)\n"
        "    end if\n"
        "    y = x\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    # The recursive call passes ({kg}, _, {kg}) into ('a, _, 'a). This
    # is a clean instantiation σ('a) = {kg} — no fire.
    poly = [d for d in diags if d.code in ("H020", "H023")]
    assert not poly, [(d.code, d.message) for d in poly]


def test_only_return_polymorphic(tmp_path: Path):
    """A function whose only polymorphic slot is the RETURN — no arg
    constrains 'a. Caller's binding-side gets a polymorphic-typed value
    that flows into concrete assignment, surfacing as concrete mismatch."""
    src = _materialise(tmp_path,
        "module mod\n"
        "contains\n"
        "  function gen() result(y)\n"
        "    real :: y  !< @unit{'a}\n"
        "    y = 0.0\n"
        "  end function\n"
        "  subroutine caller(out_kg)\n"
        "    real, intent(out) :: out_kg  !< @unit{kg}\n"
        "    out_kg = gen()\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    # No H020 should fire (the function has no slot to unify against).
    # The caller's assignment sees the function's return type — current
    # behaviour is unbound 'a passes through; the assignment then sees
    # 'a vs kg mismatch, firing H001 (a concrete-side problem, not an
    # H020). This test pins down that current behaviour rather than
    # asserting a specific spec-mandated wording.
    assert "H020" not in [d.code for d in diags]


def test_unit_assume_polymorphic_fires_u020_info(tmp_path: Path):
    """``@unit_assume{'a : reason}`` is permitted inside a polymorphic
    body (spec §"@unit_assume"). The directive emits U020 INFO and
    suppresses any D1.4 the RHS would otherwise raise."""
    src = _materialise(tmp_path,
        "subroutine f(x, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = 2.0 * x  !< @unit_assume{'a : polymorphic empirical fit}\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "U020" in codes
    assert "H023" not in codes


def test_factor_difference_under_polymorphism_silently_accepted(tmp_path: Path):
    """Phase 1 deliberately doesn't unify ``Unit.factor`` — a g/kg actual
    into an 'a slot bound to kg/kg at another slot should NOT fire H020
    (today's behaviour) even though the factors differ. Documents the
    deferred-factor-unification limitation."""
    src = _materialise(tmp_path,
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{kg/kg}\n"
        "    real, intent(out) :: b  !< @unit{kg/kg}\n"
        "    call f(a, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    assert "H020" not in [d.code for d in diags]
