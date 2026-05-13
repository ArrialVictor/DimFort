"""Tests for the homogeneity checker (semantic phase)."""
from dimfort.core import unit_config  # noqa: F401  ensure DEFAULT_TABLE is loaded
from dimfort.core.checker import check


def _var(name: str) -> dict:
    return {"node": "Var", "fields": {"v": f"{name} (SymbolTable0)"}, "loc": {}}


def _real_const(value: float = 1.0) -> dict:
    return {"node": "RealConstant", "fields": {"r": value}, "loc": {}}


def _int_const(value: int = 1) -> dict:
    return {"node": "IntegerConstant", "fields": {"n": value}, "loc": {}}


def _binop(op: str, left: dict, right: dict, *, kind: str = "RealBinOp") -> dict:
    return {
        "node": kind,
        "fields": {"left": left, "op": op, "right": right},
        "loc": {"first_line": 10, "first_column": 1, "last_line": 10, "last_column": 1},
    }


def _assign(target: dict, value: dict, line: int = 5) -> dict:
    return {
        "node": "Assignment",
        "fields": {"target": target, "value": value},
        "loc": {
            "first_line": line,
            "first_column": 1,
            "last_line": line,
            "last_column": 1,
            "first_filename": "test.f90",
        },
    }


def _prog(items: list[dict]) -> dict:
    return {"node": "TranslationUnit", "items": items}


# ---------------------- H001: assignment ---------------------------------------


def test_assignment_matching_units_is_ok():
    asr = _prog([_assign(_var("v"), _var("u"))])
    diags = check(asr, {"v": "m/s", "u": "m/s"}, file="t.f90")
    assert diags == []


def test_assignment_unit_mismatch_emits_h001():
    asr = _prog([_assign(_var("force"), _var("mass"))])
    diags = check(asr, {"force": "N", "mass": "kg"}, file="t.f90")
    codes = [d.code for d in diags]
    assert codes == ["H001"]
    assert "mismatch" in diags[0].message


def test_assignment_with_constant_rhs_uses_lhs_unit_check():
    # `v = 1.0` — RHS is unit-less (dimensionless); LHS has m/s → H001.
    asr = _prog([_assign(_var("v"), _real_const(1.0))])
    diags = check(asr, {"v": "m/s"}, file="t.f90")
    assert [d.code for d in diags] == ["H001"]


def test_unknown_variable_skipped_silently():
    # `v = unknown_var` where unknown_var has no annotation: no diagnostic.
    asr = _prog([_assign(_var("v"), _var("unknown_var"))])
    diags = check(asr, {"v": "m/s"}, file="t.f90")
    assert diags == []


# ---------------------- H002: arithmetic operands ------------------------------


def test_addition_matching_dims_is_ok():
    asr = _prog([_assign(_var("c"), _binop("Add", _var("a"), _var("b")))])
    diags = check(asr, {"a": "m", "b": "m", "c": "m"}, file="t.f90")
    assert diags == []


def test_addition_mismatched_dims_emits_h002():
    asr = _prog([_assign(_var("c"), _binop("Add", _var("a"), _var("b")))])
    diags = check(asr, {"a": "m", "b": "kg", "c": "m"}, file="t.f90")
    codes = [d.code for d in diags]
    assert "H002" in codes


def test_subtraction_mismatched_dims_emits_h002():
    asr = _prog([_assign(_var("c"), _binop("Sub", _var("a"), _var("b")))])
    diags = check(asr, {"a": "s", "b": "kg", "c": "s"}, file="t.f90")
    assert any(d.code == "H002" for d in diags)


def test_mul_combines_units():
    # force = mass * acceleration
    rhs = _binop("Mul", _var("mass"), _var("accel"))
    asr = _prog([_assign(_var("force"), rhs)])
    diags = check(
        asr,
        {"force": "kg*m/s^2", "mass": "kg", "accel": "m/s^2"},
        file="t.f90",
    )
    assert diags == []


def test_mul_unit_mismatch_after_combine_emits_h001():
    # `e = m * c` where e is energy (J) but `m * c` evaluates to kg*m/s → H001
    rhs = _binop("Mul", _var("m"), _var("c"))
    asr = _prog([_assign(_var("e"), rhs)])
    diags = check(
        asr,
        {"e": "J", "m": "kg", "c": "m/s"},  # right side is kg*m/s, not kg*m^2/s^2
        file="t.f90",
    )
    assert any(d.code == "H001" for d in diags)


def test_div_combines_units():
    # speed = distance / time
    rhs = _binop("Div", _var("distance"), _var("time"))
    asr = _prog([_assign(_var("speed"), rhs)])
    diags = check(
        asr,
        {"speed": "m/s", "distance": "m", "time": "s"},
        file="t.f90",
    )
    assert diags == []


# ---------------------- Pow ----------------------------------------------------


def test_pow_with_integer_exponent_squares_unit():
    # area = side**2 → m * m = m^2
    rhs = _binop("Pow", _var("side"), _int_const(2))
    asr = _prog([_assign(_var("area"), rhs)])
    diags = check(asr, {"area": "m^2", "side": "m"}, file="t.f90")
    assert diags == []


def test_pow_emits_h001_when_target_unit_wrong():
    rhs = _binop("Pow", _var("side"), _int_const(2))
    asr = _prog([_assign(_var("area"), rhs)])
    diags = check(asr, {"area": "m", "side": "m"}, file="t.f90")  # m vs m^2
    assert any(d.code == "H001" for d in diags)


# ---------------------- U002: bad annotation ----------------------------------


def test_bad_annotation_text_emits_u002():
    asr = _prog([])
    diags = check(asr, {"x": "widget"}, file="t.f90")
    assert any(d.code == "U002" for d in diags)
