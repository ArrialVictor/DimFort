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


# ---------------------- intrinsics --------------------------------------------


def _intrinsic(args: list[dict], *, line: int = 10, col: int = 7) -> dict:
    """Build a minimal IntrinsicElementalFunction node at (line, col)."""
    return {
        "node": "IntrinsicElementalFunction",
        "fields": {"args": args, "intrinsic_id": 0},
        "loc": {
            "first_line": line, "first_column": col,
            "last_line": line, "last_column": col,
        },
    }


def _intrinsic_names(*names_at: tuple[int, int, str]) -> dict:
    """Build an AST shaped to expose the given intrinsic-name table.

    ``names_at`` is a list of ``(line, col, name)``.
    """
    nodes = [
        {
            "node": "FuncCallOrArray",
            "fields": {"func": name},
            "loc": {
                "first_line": line, "first_column": col,
                "last_line": line, "last_column": col,
            },
        }
        for (line, col, name) in names_at
    ]
    return {"node": "TranslationUnit", "items": nodes}


def test_sqrt_halves_unit():
    # side = sqrt(area)  where area: m^2 and side: m  → ok.
    call = _intrinsic([_var("area")])
    asr = _prog([_assign(_var("side"), call)])
    ast = _intrinsic_names((10, 7, "sqrt"))
    diags = check(
        asr, {"side": "m", "area": "m^2"}, ast=ast, file="t.f90"
    )
    assert diags == []


def test_sqrt_wrong_target_emits_h001():
    call = _intrinsic([_var("area")])
    asr = _prog([_assign(_var("side"), call)])
    ast = _intrinsic_names((10, 7, "sqrt"))
    diags = check(
        asr, {"side": "kg", "area": "m^2"}, ast=ast, file="t.f90"
    )
    assert any(d.code == "H001" for d in diags)


def test_exp_non_dimensionless_emits_h003():
    call = _intrinsic([_var("x")])
    asr = _prog([_assign(_var("y"), call)])
    ast = _intrinsic_names((10, 7, "exp"))
    diags = check(asr, {"x": "m", "y": "1"}, ast=ast, file="t.f90")
    assert any(d.code == "H003" for d in diags)


def test_exp_dimensionless_is_ok():
    call = _intrinsic([_var("x")])
    asr = _prog([_assign(_var("y"), call)])
    ast = _intrinsic_names((10, 7, "exp"))
    diags = check(asr, {"x": "1", "y": "1"}, ast=ast, file="t.f90")
    assert diags == []


def test_max_with_matching_units_is_ok():
    call = _intrinsic([_var("a"), _var("b")])
    asr = _prog([_assign(_var("c"), call)])
    ast = _intrinsic_names((10, 7, "max"))
    diags = check(asr, {"a": "m", "b": "m", "c": "m"}, ast=ast, file="t.f90")
    assert diags == []


def test_max_with_mismatched_units_emits_h002():
    call = _intrinsic([_var("a"), _var("b")])
    asr = _prog([_assign(_var("c"), call)])
    ast = _intrinsic_names((10, 7, "max"))
    diags = check(asr, {"a": "m", "b": "kg", "c": "m"}, ast=ast, file="t.f90")
    codes = [d.code for d in diags]
    assert "H002" in codes


def test_dot_product_multiplies_units():
    call = _intrinsic([_var("a"), _var("b")])
    asr = _prog([_assign(_var("c"), call)])
    ast = _intrinsic_names((10, 7, "dot_product"))
    diags = check(
        asr, {"a": "m", "b": "kg", "c": "kg*m"}, ast=ast, file="t.f90"
    )
    assert diags == []


def test_sum_preserves_element_unit():
    call = _intrinsic([_var("arr")])
    asr = _prog([_assign(_var("total"), call)])
    ast = _intrinsic_names((10, 7, "sum"))
    diags = check(
        asr, {"arr": "kg", "total": "kg"}, ast=ast, file="t.f90"
    )
    assert diags == []


def test_floor_passes_unit_through():
    call = _intrinsic([_var("x")])
    asr = _prog([_assign(_var("y"), call)])
    ast = _intrinsic_names((10, 7, "floor"))
    diags = check(asr, {"x": "m", "y": "m"}, ast=ast, file="t.f90")
    assert diags == []


def test_unknown_intrinsic_name_skips_check_silently():
    # Without AST, intrinsic name is unknown → expression is unknown unit.
    call = _intrinsic([_var("x")])
    asr = _prog([_assign(_var("y"), call)])
    diags = check(asr, {"x": "m", "y": "kg"}, file="t.f90")  # would H001 if known
    assert diags == []


# ---------------------- user-defined function calls (H004) --------------------


def _function(name: str, args: list[str], return_name: str) -> dict:
    """A minimal ASR Function node with `args` and `return_var` as Vars."""
    return {
        "node": "Function",
        "fields": {
            "name": name,
            "args": [_var(a) for a in args],
            "return_var": _var(return_name),
        },
    }


def _subroutine(name: str, args: list[str]) -> dict:
    return {
        "node": "Subroutine",
        "fields": {
            "name": name,
            "args": [_var(a) for a in args],
            "return_var": [],
        },
    }


def _call_arg(node: dict) -> dict:
    return {"node": "call_arg", "fields": {"value": node}}


def _function_call(name: str, args: list[dict]) -> dict:
    return {
        "node": "FunctionCall",
        "fields": {
            "name": f"{name} (SymbolTable0)",
            "args": [_call_arg(a) for a in args],
        },
        "loc": {"first_line": 20, "first_column": 7, "last_line": 20, "last_column": 7},
    }


def _subroutine_call(name: str, args: list[dict]) -> dict:
    return {
        "node": "SubroutineCall",
        "fields": {
            "name": f"{name} (SymbolTable0)",
            "args": [_call_arg(a) for a in args],
        },
        "loc": {"first_line": 25, "first_column": 3, "last_line": 25, "last_column": 3},
    }


def test_function_call_matching_arg_units_ok():
    fn = _function("area", args=["side"], return_name="area")
    call = _function_call("area", [_var("s")])
    asr = _prog([fn, _assign(_var("a"), call)])
    diags = check(
        asr,
        {"side": "m", "area": "m^2", "s": "m", "a": "m^2"},
        file="t.f90",
    )
    assert diags == []


def test_function_call_wrong_arg_unit_emits_h004():
    fn = _function("area", args=["side"], return_name="area")
    call = _function_call("area", [_var("s")])
    asr = _prog([fn, _assign(_var("a"), call)])
    diags = check(
        asr,
        {"side": "m", "area": "m^2", "s": "kg", "a": "m^2"},
        file="t.f90",
    )
    codes = [d.code for d in diags]
    assert "H004" in codes
    h004 = next(d for d in diags if d.code == "H004")
    assert "area" in h004.message
    assert "expected m" in h004.message


def test_function_call_return_unit_drives_h001():
    fn = _function("area", args=["side"], return_name="area")
    call = _function_call("area", [_var("s")])
    # Assigning an m^2 return to an m target → H001.
    asr = _prog([fn, _assign(_var("len"), call)])
    diags = check(
        asr,
        {"side": "m", "area": "m^2", "s": "m", "len": "m"},
        file="t.f90",
    )
    codes = [d.code for d in diags]
    assert "H001" in codes


def test_subroutine_call_arg_mismatch_emits_h004():
    sub = _subroutine("grow", args=["x", "factor"])
    call = _subroutine_call("grow", [_var("v"), _var("s")])
    asr = _prog([sub, call])
    diags = check(
        asr,
        {"x": "m", "factor": "1", "v": "m", "s": "kg"},  # factor should be dimensionless
        file="t.f90",
    )
    assert any(d.code == "H004" for d in diags)


def test_unknown_function_skips_check_silently():
    call = _function_call("mystery", [_var("x")])
    asr = _prog([_assign(_var("y"), call)])
    diags = check(asr, {"x": "m", "y": "kg"}, file="t.f90")
    # No signature known → expression unit is None → no diagnostic.
    assert diags == []


# ---------------------- derived-type field access -----------------------------


def _struct_member(receiver: str, type_name: str, field_name: str) -> dict:
    """A StructInstanceMember node accessing `<receiver>%<field_name>`.

    The qualified `m` field follows LFortran's
    ``<index>_<typename>_<field>`` convention.
    """
    return {
        "node": "StructInstanceMember",
        "fields": {
            "v": _var(receiver),
            "m": f"1_{type_name}_{field_name} (SymbolTable0)",
        },
        "loc": {"first_line": 10, "first_column": 1, "last_line": 10, "last_column": 1},
    }


def test_struct_member_read_resolves_to_field_unit():
    # `tot = b%m` — both sides kg → ok.
    asr = _prog([_assign(_var("tot"), _struct_member("b", "particle", "m"))])
    diags = check(
        asr,
        {"tot": "kg"},
        field_units_text={("particle", "m"): "kg"},
        file="t.f90",
    )
    assert diags == []


def test_struct_member_read_unit_mismatch_emits_h001():
    asr = _prog([_assign(_var("tot"), _struct_member("b", "particle", "m"))])
    diags = check(
        asr,
        {"tot": "m"},                                      # local m: metres
        field_units_text={("particle", "m"): "kg"},        # field m: kilograms
        file="t.f90",
    )
    assert any(d.code == "H001" for d in diags)


def test_struct_member_write_unit_mismatch_emits_h001():
    # `b%m = mass`  where mass is kg and b%m is kg → ok.
    asr = _prog([_assign(_struct_member("b", "particle", "m"), _var("mass"))])
    diags = check(
        asr,
        {"mass": "kg"},
        field_units_text={("particle", "m"): "kg"},
        file="t.f90",
    )
    assert diags == []


def test_struct_member_write_with_wrong_unit_emits_h001():
    asr = _prog([_assign(_struct_member("b", "particle", "m"), _var("len"))])
    diags = check(
        asr,
        {"len": "m"},
        field_units_text={("particle", "m"): "kg"},
        file="t.f90",
    )
    assert any(d.code == "H001" for d in diags)


def test_struct_member_unknown_field_is_silent():
    asr = _prog([_assign(_var("y"), _struct_member("b", "particle", "z"))])
    diags = check(
        asr,
        {"y": "kg"},
        field_units_text={("particle", "m"): "kg"},  # only m is annotated
        file="t.f90",
    )
    assert diags == []


def test_function_with_no_annotated_return_does_not_drive_h001():
    fn = _function("opaque", args=["x"], return_name="opaque")
    call = _function_call("opaque", [_var("v")])
    asr = _prog([fn, _assign(_var("y"), call)])
    # `opaque` has no return annotation → expression is unknown unit → no H001.
    diags = check(asr, {"x": "m", "v": "m", "y": "kg"}, file="t.f90")
    assert diags == []
