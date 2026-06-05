"""M4 tests for polymorphic call-site instantiation + H020.

When a call site for a polymorphic function passes actual args whose
units bind a tyvar inconsistently across slots, the unifier rejects the
substitution and H020 fires. Concrete slots still fire H004 (existing
UX). Cleanly polymorphic calls produce no fires.
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
# H020 fires


def test_h020_two_way_conflict(tmp_path: Path):
    """Caller passes x={m}, y={kg} into a function expecting both as 'a.
    Both slots collide on 'a — H020 lists both contributors."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{m}\n"
        "    real, intent(out) :: b  !< @unit{kg}\n"
        "    call f(a, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H020" in codes, [(d.code, d.message) for d in diags]


def test_h020_message_lists_both_contributors(tmp_path: Path):
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{m}\n"
        "    real, intent(out) :: b  !< @unit{kg}\n"
        "    call f(a, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    h020 = next(d for d in diags if d.code == "H020")
    # The two implied bindings must both appear, with a symmetric
    # "collides with" trailer naming the other arg.
    assert "'a = m" in h020.message
    assert "'a = kg" in h020.message
    assert "collides with arg 2" in h020.message
    assert "collides with arg 1" in h020.message


# ---------------------------------------------------------------------------
# H020 does NOT fire — clean polymorphic call


def test_no_h020_clean_polymorphic_call(tmp_path: Path):
    """Caller passes two {m} args into a function expecting both as 'a —
    cleanly polymorphic, no fire."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{m}\n"
        "    real, intent(out) :: b  !< @unit{m}\n"
        "    call f(a, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H020" not in codes
    assert "H004" not in codes


def test_no_h020_concrete_slot_passes(tmp_path: Path):
    """A polymorphic signature mixed with concrete slots — concrete slot
    that matches doesn't trigger anything."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, c, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(in)  :: c  !< @unit{kg}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, m, b)\n"
        "    real, intent(in)  :: a  !< @unit{m}\n"
        "    real, intent(in)  :: m  !< @unit{kg}\n"
        "    real, intent(out) :: b  !< @unit{m}\n"
        "    call f(a, m, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H020" not in codes
    assert "H004" not in codes


# ---------------------------------------------------------------------------
# Concrete-slot mismatch still fires H004 (UX preserved)


def test_concrete_slot_mismatch_fires_h004(tmp_path: Path):
    """Polymorphic signature with mixed concrete slot — concrete slot
    mismatch keeps its existing H004 UX."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, c, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(in)  :: c  !< @unit{kg}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, bad, b)\n"
        "    real, intent(in)  :: a    !< @unit{m}\n"
        "    real, intent(in)  :: bad  !< @unit{m/s}\n"
        "    real, intent(out) :: b    !< @unit{m}\n"
        "    call f(a, bad, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H004" in codes
    assert "H020" not in codes


# ---------------------------------------------------------------------------
# Non-polymorphic callee unaffected


def test_concrete_callee_unchanged(tmp_path: Path):
    """Existing H004 path for fully concrete callees is untouched."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{m}\n"
        "    real, intent(out) :: y  !< @unit{m}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(a, b)\n"
        "    real, intent(in)  :: a  !< @unit{kg}\n"
        "    real, intent(out) :: b  !< @unit{m}\n"
        "    call f(a, b)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H004" in codes
    assert "H020" not in codes
