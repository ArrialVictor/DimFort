"""Tests for the annotation→variable attachment pass (stage 2).

These tests don't need LFortran: they feed hand-built ASR-shaped dicts
into :func:`attach`. The real LFortran-driven path is covered in
``test_lfortran.py``.
"""
from dimfort.core.annotations import AnnotationKind, RawAnnotation, ScanResult
from dimfort.core.attach import attach


def _scan(annotations: list[RawAnnotation]) -> ScanResult:
    return ScanResult(annotations=tuple(annotations), errors=())


def _variable(name: str, type_line: int) -> dict:
    """Build a minimal ASR-shaped Variable node."""
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


def test_post_annotation_attaches_to_single_variable():
    asr = _asr([_variable("v", type_line=3)])
    scan = _scan([
        RawAnnotation(AnnotationKind.POST, line=3, column=12, unit_text="m/s")
    ])
    res = attach(scan, asr)
    assert res.var_units == {"v": "m/s"}
    assert res.orphans == []


def test_post_annotation_applies_to_all_variables_on_same_type_line():
    # `real :: a, b, c !< @unit{m}` — three Variables sharing type line.
    asr = _asr([
        _variable("a", type_line=5),
        _variable("b", type_line=5),
        _variable("c", type_line=5),
    ])
    scan = _scan([
        RawAnnotation(AnnotationKind.POST, line=5, column=20, unit_text="m")
    ])
    res = attach(scan, asr)
    assert res.var_units == {"a": "m", "b": "m", "c": "m"}


def test_post_annotation_without_matching_declaration_is_orphan():
    asr = _asr([_variable("v", type_line=3)])
    scan = _scan([
        RawAnnotation(AnnotationKind.POST, line=7, column=1, unit_text="kg")
    ])
    res = attach(scan, asr)
    assert res.var_units == {}
    assert len(res.orphans) == 1
    assert res.orphans[0].line == 7
    assert res.orphans[0].unit_text == "kg"


def test_pre_annotations_are_collected_unattached_for_now():
    asr = _asr([_variable("v", type_line=4)])
    pre = RawAnnotation(AnnotationKind.PRE, line=3, column=1, unit_text="m")
    scan = _scan([pre])
    res = attach(scan, asr)
    assert res.var_units == {}
    assert res.unattached_pre == [pre]


def test_mixed_pre_and_post():
    asr = _asr([_variable("x", type_line=2), _variable("y", type_line=5)])
    scan = _scan([
        RawAnnotation(AnnotationKind.PRE, line=1, column=1, unit_text="m"),
        RawAnnotation(AnnotationKind.POST, line=5, column=20, unit_text="kg"),
    ])
    res = attach(scan, asr)
    assert res.var_units == {"y": "kg"}
    assert len(res.unattached_pre) == 1
    assert res.unattached_pre[0].unit_text == "m"
