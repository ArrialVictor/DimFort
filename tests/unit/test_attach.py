"""Tests for the annotation→variable attachment pass (stage 2)."""
from dimfort.core.annotations import AnnotationKind, RawAnnotation, ScanResult
from dimfort.core.attach import attach


def _scan(
    annotations: list[RawAnnotation] | None = None,
    pre_block_lines: list[int] | None = None,
) -> ScanResult:
    return ScanResult(
        annotations=tuple(annotations or []),
        errors=(),
        pre_block_lines=frozenset(pre_block_lines or []),
    )


def _variable(name: str, type_line: int) -> dict:
    return {
        "node": "Variable",
        "fields": {
            "name": name,
            "type": {
                "node": "Real",
                "fields": {"kind": 4},
                "loc": {"first_line": type_line, "first_column": 1},
            },
        },
    }


def _asr(variables: list[dict]) -> dict:
    return {"node": "TranslationUnit", "items": variables}


# ---------- POST -----------------------------------------------------------


def test_post_attaches_to_single_variable():
    asr = _asr([_variable("v", type_line=3)])
    scan = _scan([RawAnnotation(AnnotationKind.POST, 3, 12, "m/s")])
    res = attach(scan, asr)
    assert res.var_units == {"v": "m/s"}
    assert res.orphans == []


def test_post_applies_to_all_variables_on_same_type_line():
    asr = _asr([
        _variable("a", 5),
        _variable("b", 5),
        _variable("c", 5),
    ])
    scan = _scan([RawAnnotation(AnnotationKind.POST, 5, 20, "m")])
    res = attach(scan, asr)
    assert res.var_units == {"a": "m", "b": "m", "c": "m"}


def test_post_without_matching_declaration_is_orphan():
    asr = _asr([_variable("v", 3)])
    scan = _scan([RawAnnotation(AnnotationKind.POST, 7, 1, "kg")])
    res = attach(scan, asr)
    assert res.var_units == {}
    assert len(res.orphans) == 1
    assert res.orphans[0].line == 7


# ---------- PRE ------------------------------------------------------------


def test_pre_single_line_block_attaches_to_next_declaration():
    asr = _asr([_variable("v", type_line=4)])
    scan = _scan(
        [RawAnnotation(AnnotationKind.PRE, line=3, column=1, unit_text="m/s")],
        pre_block_lines=[3],
    )
    res = attach(scan, asr)
    assert res.var_units == {"v": "m/s"}


def test_pre_multi_line_block_attaches_to_line_after_block_end():
    # Block on lines 1-3, declaration on line 4.
    asr = _asr([_variable("f", type_line=4)])
    scan = _scan(
        [RawAnnotation(AnnotationKind.PRE, line=2, column=1, unit_text="kg*m/s^2")],
        pre_block_lines=[1, 2, 3],
    )
    res = attach(scan, asr)
    assert res.var_units == {"f": "kg*m/s^2"}


def test_pre_applies_to_all_variables_on_target_type_line():
    asr = _asr([_variable("a", 4), _variable("b", 4)])
    scan = _scan(
        [RawAnnotation(AnnotationKind.PRE, 3, 1, "m")],
        pre_block_lines=[3],
    )
    res = attach(scan, asr)
    assert res.var_units == {"a": "m", "b": "m"}


def test_pre_with_no_following_declaration_is_orphan():
    asr = _asr([_variable("v", 10)])
    scan = _scan(
        [RawAnnotation(AnnotationKind.PRE, 3, 1, "m")],
        pre_block_lines=[3],
    )
    res = attach(scan, asr)
    assert res.var_units == {}
    assert len(res.orphans) == 1
    assert "no declaration" in res.orphans[0].reason


# ---------- conflict between PRE and POST ----------------------------------


def test_pre_and_post_on_same_variable_with_matching_unit_is_fine():
    asr = _asr([_variable("v", 4)])
    scan = _scan(
        [
            RawAnnotation(AnnotationKind.PRE, 3, 1, "m"),
            RawAnnotation(AnnotationKind.POST, 4, 20, "m"),
        ],
        pre_block_lines=[3],
    )
    res = attach(scan, asr)
    assert res.var_units == {"v": "m"}
    assert res.conflicts == []


def test_pre_and_post_disagree_records_conflict():
    asr = _asr([_variable("v", 4)])
    scan = _scan(
        [
            RawAnnotation(AnnotationKind.PRE, 3, 1, "m"),
            RawAnnotation(AnnotationKind.POST, 4, 20, "kg"),
        ],
        pre_block_lines=[3],
    )
    res = attach(scan, asr)
    # First wins; conflict recorded with the second.
    assert res.var_units == {"v": "m"}
    assert len(res.conflicts) == 1
    c = res.conflicts[0]
    assert (c.variable, c.first_unit, c.second_unit) == ("v", "m", "kg")
