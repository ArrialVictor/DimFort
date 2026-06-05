"""M6 tests for polymorphism polish:

- Hover signature rendering prefixes ``∀ 'a.`` for polymorphic functions.
- Concrete signatures unchanged.
- Self-recursive polymorphic functions check cleanly (verifies that
  M3/M4's per-call-site fresh instantiation already handles recursion
  with no extra fixpoint pass).
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core.multifile import check_files
from dimfort.core.symbols import FuncSig
from dimfort.core.units import parse
from dimfort.lsp.hover_render import _sig_render_md


def _materialise(tmp_path: Path, name: str, body: str) -> Path:
    src = tmp_path / name
    src.write_text(body)
    return src


def _diags(result, file: Path) -> list:
    return list(result.diagnostics.get(file.resolve(), []))


# ---------------------------------------------------------------------------
# Hover signature rendering


def test_concrete_signature_has_no_quantifier():
    sig = FuncSig(
        arg_names=("x", "y"),
        arg_units=(parse("m"), parse("s")),
        return_unit=parse("m/s"),
        is_subroutine=False,
    )
    rendered = _sig_render_md("foo", sig)
    assert "∀" not in rendered
    assert rendered.startswith("foo(")


def test_polymorphic_signature_has_quantifier_prefix():
    sig = FuncSig(
        arg_names=("x", "y"),
        arg_units=(parse("'a"), parse("'a")),
        return_unit=parse("'a"),
        is_subroutine=False,
    )
    rendered = _sig_render_md("avg", sig)
    assert rendered.startswith("∀ 'a.")
    assert "avg(" in rendered


def test_polymorphic_signature_two_tyvars_two_quantifiers():
    sig = FuncSig(
        arg_names=("m", "v", "p"),
        arg_units=(parse("'a"), parse("'b"), parse("'a*'b")),
        return_unit=None,
        is_subroutine=True,
    )
    rendered = _sig_render_md("momentum", sig)
    # Sorted order — 'a before 'b.
    assert rendered.startswith("∀ 'a. ∀ 'b.")


def test_polymorphic_signature_mixed_with_concrete_slots():
    """A signature with both tyvar and concrete slots — only the tyvar
    drives the quantifier prefix."""
    sig = FuncSig(
        arg_names=("x", "c", "y"),
        arg_units=(parse("'a"), parse("kg"), parse("'a")),
        return_unit=None,
        is_subroutine=True,
    )
    rendered = _sig_render_md("scaled_avg", sig)
    assert rendered.startswith("∀ 'a.")


# ---------------------------------------------------------------------------
# Recursion


def test_self_recursive_polymorphic_function_checks_cleanly(tmp_path: Path):
    """A polymorphic function that calls itself with the same-typed arg
    must not fire any diagnostic. M3/M4 already do per-call-site fresh
    instantiation, so self-recursion needs no extra fixpoint pass."""
    src = _materialise(tmp_path, "rec.f90",
        "module mod\n"
        "contains\n"
        "  recursive subroutine f(x, depth, y)\n"
        "    real, intent(in)    :: x      !< @unit{'a}\n"
        "    integer, intent(in) :: depth\n"
        "    real, intent(out)   :: y      !< @unit{'a}\n"
        "    real                :: tmp    !< @unit{'a}\n"
        "    if (depth > 0) then\n"
        "      call f(x, depth - 1, tmp)\n"
        "      y = tmp\n"
        "    else\n"
        "      y = x\n"
        "    end if\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    poly_codes = [d.code for d in diags if d.code in ("H020", "H023")]
    assert not poly_codes, [(d.code, d.message) for d in diags]


def test_recursive_call_with_wrong_unit_fires_h020(tmp_path: Path):
    """Negative case: even in a recursive call, a unit mismatch at the
    recursive site fires H020 — confirms the call-site dispatch runs
    on recursive calls just like external ones."""
    src = _materialise(tmp_path, "rec.f90",
        "module mod\n"
        "contains\n"
        "  recursive subroutine f(x, y)\n"
        "    real, intent(in)    :: x      !< @unit{'a}\n"
        "    real, intent(out)   :: y      !< @unit{'a}\n"
        "    real                :: bad    !< @unit{kg}\n"
        "    real                :: out_m  !< @unit{m}\n"
        "    call f(bad, out_m)\n"
        "    y = x\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H020" in codes, [(d.code, d.message) for d in diags]
