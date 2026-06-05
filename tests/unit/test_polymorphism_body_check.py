"""M3 tests for polymorphic body checking + H023.

When a function's signature declares ``@unit{'a}`` on one or more args,
the body is checked in the polymorphic context: tyvar-typed operations
that would force a binding on ``'a`` (e.g. ``'a + concrete``, ``'a +
'a^(-1)``) fire **H023** rather than H001/H002. Concrete-only operations
inside the same function still fire H001/H002 normally.

Outside any polymorphic function (module-level code, non-polymorphic
routine), behaviour is unchanged.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.core.multifile import check_files


def _materialise(tmp_path: Path, name: str, body: str) -> Path:
    src = tmp_path / name
    src.write_text(body)
    return src


def _diags(result, file: Path) -> list:
    return list(result.diagnostics.get(file.resolve(), []))


# ---------------------------------------------------------------------------
# H023 fires


def test_h023_for_tyvar_plus_concrete(tmp_path: Path):
    """``'a + kg`` inside a polymorphic body fires H023, not H002."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(x, c, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: c  !< @unit{kg}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = x + c\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" in codes, [(d.code, d.message) for d in diags]
    assert "H002" not in codes  # H023 supersedes H002


def test_h023_assignment_tyvar_lhs_concrete_rhs(tmp_path: Path):
    """``'a-typed y = concrete-typed c`` fires H023 at the assignment."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(c, y)\n"
        "  real, intent(in)  :: c  !< @unit{kg}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = c\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" in codes, [(d.code, d.message) for d in diags]
    assert "H001" not in codes


def test_h023_message_names_tyvar(tmp_path: Path):
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(x, c, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: c  !< @unit{kg}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = x + c\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    h023 = next(d for d in diags if d.code == "H023")
    assert "'a" in h023.message
    assert "polymorphic" in h023.message.lower()


# ---------------------------------------------------------------------------
# H023 does NOT fire — polymorphism preserved


def test_no_h023_when_body_is_polymorphic(tmp_path: Path):
    """``y = x`` where both are ``'a``-typed is the clean polymorphic case."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(x, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(out) :: y  !< @unit{'a}\n"
        "  y = x\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" not in codes
    assert "H001" not in codes
    assert "H002" not in codes


def test_no_h023_tyvar_plus_tyvar(tmp_path: Path):
    """``x + y`` both ``'a``-typed: ``'a + 'a → 'a`` clean."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(x, y, z)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: y  !< @unit{'a}\n"
        "  real, intent(out) :: z  !< @unit{'a}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" not in codes
    assert "H002" not in codes


def test_no_h023_tyvar_times_concrete(tmp_path: Path):
    """``x * c`` where x is ``'a`` and c is ``{kg}``: product is ``'a·kg``,
    no constraint. Cleanly polymorphic (the vlx u_mq pattern)."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(x, c, y)\n"
        "  real, intent(in)  :: x  !< @unit{'a}\n"
        "  real, intent(in)  :: c  !< @unit{kg}\n"
        "  real, intent(out) :: y  !< @unit{'a*kg}\n"
        "  y = x * c\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H023" not in codes
    assert "H001" not in codes


# ---------------------------------------------------------------------------
# Outside polymorphic context — H001/H002 unchanged


def test_h001_unchanged_in_non_polymorphic_function(tmp_path: Path):
    """A concrete (non-polymorphic) function with an LHS-RHS mismatch
    still fires H001, not H023."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(a, b)\n"
        "  real, intent(in)  :: a  !< @unit{m/s}\n"
        "  real, intent(out) :: b  !< @unit{kg}\n"
        "  b = a\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H001" in codes
    assert "H023" not in codes


def test_h002_unchanged_in_non_polymorphic_function(tmp_path: Path):
    """Concrete + mismatch in a non-polymorphic function fires H002."""
    src = _materialise(tmp_path, "p.f90",
        "subroutine f(a, b, c)\n"
        "  real, intent(in)  :: a  !< @unit{kg}\n"
        "  real, intent(in)  :: b  !< @unit{m/s}\n"
        "  real, intent(out) :: c  !< @unit{kg}\n"
        "  c = a + b\n"
        "end subroutine\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H002" in codes
    assert "H023" not in codes
