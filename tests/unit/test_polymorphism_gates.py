"""M5 tests for the parse-position gate (H021) and the affine gate (H022).

H021 fires when ``@unit{'a}`` appears in a position where the tyvar
cannot be quantified — module-level vars, PARAMETER declarations,
derived-type components. Allowed positions (dummy args, body locals
inside a routine) produce no fire.

H022 fires at call sites that try to bind a tyvar slot to an affine
unit (``offset != 0``, e.g. ``degC``). The fix is to convert the
affine value to its base unit at the call site, or pass it as a delta.
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
# H021 fires


def test_h021_module_level_var(tmp_path: Path):
    """A module-level variable typed ``@unit{'a}`` has no quantifier — H021."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "  real :: shared  !< @unit{'a}\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H021" in codes, [(d.code, d.message) for d in diags]


def test_h021_derived_type_component(tmp_path: Path):
    """A tyvar attached to a derived-type field has no quantifier — H021.
    The lookup path goes through ``field_units`` (keyed by type + field
    name), not the scoped ``unit_for`` map; this test pins down that
    branch."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "  type :: point\n"
        "    real :: x  !< @unit{'a}\n"
        "  end type point\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H021" in codes, [(d.code, d.message) for d in diags]
    h021 = next(d for d in diags if d.code == "H021")
    assert "derived-type" in h021.message


def test_h021_parameter_decl(tmp_path: Path):
    """``PARAMETER`` declarations carry no quantifier — H021."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x)\n"
        "    real, intent(in) :: x  !< @unit{'a}\n"
        "    real, parameter :: c = 1.0  !< @unit{'a}\n"
        "    x = c\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H021" in codes, [(d.code, d.message) for d in diags]


def test_h021_message_names_position(tmp_path: Path):
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "  real :: shared  !< @unit{'a}\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    h021 = next(d for d in diags if d.code == "H021")
    assert "module-level" in h021.message


# ---------------------------------------------------------------------------
# H021 does NOT fire — allowed positions


def test_no_h021_for_dummy_args(tmp_path: Path):
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H021" not in codes


def test_no_h021_for_body_local(tmp_path: Path):
    """A non-PARAMETER local var inside a routine is an allowed position."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    real :: tmp  !< @unit{'a}\n"
        "    tmp = x\n"
        "    y = tmp\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H021" not in codes


# ---------------------------------------------------------------------------
# H022 fires


def test_h022_affine_actual_into_tyvar_slot(tmp_path: Path):
    """Passing a degC-typed variable into a 'a slot — affine units cannot
    bind tyvars. H022."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(t_celsius, t_out)\n"
        "    real, intent(in)  :: t_celsius  !< @unit{degC}\n"
        "    real, intent(out) :: t_out      !< @unit{degC}\n"
        "    call f(t_celsius, t_out)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H022" in codes, [(d.code, d.message) for d in diags]


def test_h022_message_mentions_fix(tmp_path: Path):
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(t_celsius, t_out)\n"
        "    real, intent(in)  :: t_celsius  !< @unit{degC}\n"
        "    real, intent(out) :: t_out      !< @unit{degC}\n"
        "    call f(t_celsius, t_out)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    h022 = next(d for d in diags if d.code == "H022")
    assert "affine" in h022.message.lower()
    assert "delta" in h022.message.lower() or "convert" in h022.message.lower()
    # Spec wording: "cannot bind 'a to affine unit ..." — message names
    # the specific tyvar, not the generic "type variable".
    assert "'a" in h022.message


# ---------------------------------------------------------------------------
# H022 does NOT fire — non-affine actual is fine


def test_no_h022_non_affine_actual(tmp_path: Path):
    """A {K} actual (offset 0) binds 'a cleanly — no H022."""
    src = _materialise(tmp_path, "p.f90",
        "module mod\n"
        "contains\n"
        "  subroutine f(x, y)\n"
        "    real, intent(in)  :: x  !< @unit{'a}\n"
        "    real, intent(out) :: y  !< @unit{'a}\n"
        "    y = x\n"
        "  end subroutine\n"
        "  subroutine caller(t_kelvin, t_out)\n"
        "    real, intent(in)  :: t_kelvin  !< @unit{K}\n"
        "    real, intent(out) :: t_out     !< @unit{K}\n"
        "    call f(t_kelvin, t_out)\n"
        "  end subroutine\n"
        "end module\n"
    )
    result = check_files([src])
    diags = _diags(result, src)
    codes = [d.code for d in diags]
    assert "H022" not in codes
