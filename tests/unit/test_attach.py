"""Tests for the annotation→declaration attachment pass (stage 2)."""
from dimfort.core.annotations import (
    AnnotationKind,
    DeclarationSite,
    RawAnnotation,
    ScanResult,
)
from dimfort.core.attach import attach


def _scan(
    annotations: list[RawAnnotation] | None = None,
    pre_block_lines: list[int] | None = None,
    declarations: list[DeclarationSite] | None = None,
) -> ScanResult:
    return ScanResult(
        annotations=tuple(annotations or []),
        errors=(),
        pre_block_lines=frozenset(pre_block_lines or []),
        declarations=tuple(declarations or []),
    )


# ---------- POST -----------------------------------------------------------


def test_post_attaches_to_declaration_on_same_line():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 3, 12, "m/s")],
        declarations=[DeclarationSite(3, 3, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {"v": "m/s"}
    assert res.orphans == []


def test_post_applies_to_every_name_in_declaration_list():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 5, 20, "m")],
        declarations=[DeclarationSite(5, 5, ("a", "b", "c"))],
    )
    res = attach(scan)
    assert res.var_units == {"a": "m", "b": "m", "c": "m"}


def test_post_on_last_line_of_continuation_attaches():
    # `real :: pressure, &
    #          temperature, &
    #          density   !< @unit{Pa}`
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 18, 30, "Pa")],
        declarations=[DeclarationSite(16, 18, ("pressure", "temperature", "density"))],
    )
    res = attach(scan)
    assert res.var_units == {
        "pressure": "Pa",
        "temperature": "Pa",
        "density": "Pa",
    }


def test_post_on_first_line_of_continuation_attaches():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 21, 30, "kg")],
        declarations=[DeclarationSite(21, 23, ("a1", "a2", "a3"))],
    )
    res = attach(scan)
    assert res.var_units == {"a1": "kg", "a2": "kg", "a3": "kg"}


def test_post_outside_any_declaration_is_orphan():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 7, 1, "kg")],
        declarations=[DeclarationSite(3, 3, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {}
    assert len(res.orphans) == 1
    assert res.orphans[0].line == 7


# ---------- PRE ------------------------------------------------------------


def test_pre_single_line_block_attaches_to_next_declaration():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.PRE, 3, 1, "m/s")],
        pre_block_lines=[3],
        declarations=[DeclarationSite(4, 4, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {"v": "m/s"}


def test_pre_multi_line_block_attaches_after_block_end():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.PRE, 2, 1, "kg*m/s^2")],
        pre_block_lines=[1, 2, 3],
        declarations=[DeclarationSite(4, 4, ("f",))],
    )
    res = attach(scan)
    assert res.var_units == {"f": "kg*m/s^2"}


def test_pre_block_before_continued_declaration_attaches():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.PRE, 1, 1, "1")],
        pre_block_lines=[1],
        declarations=[DeclarationSite(2, 4, ("alpha", "beta", "gamma"))],
    )
    res = attach(scan)
    assert res.var_units == {"alpha": "1", "beta": "1", "gamma": "1"}


def test_pre_without_following_declaration_is_orphan():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.PRE, 3, 1, "m")],
        pre_block_lines=[3],
        declarations=[DeclarationSite(10, 10, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {}
    assert len(res.orphans) == 1
    assert "no declaration" in res.orphans[0].reason


# ---------- conflict -------------------------------------------------------


def test_pre_and_post_matching_unit_is_fine():
    scan = _scan(
        annotations=[
            RawAnnotation(AnnotationKind.PRE, 3, 1, "m"),
            RawAnnotation(AnnotationKind.POST, 4, 20, "m"),
        ],
        pre_block_lines=[3],
        declarations=[DeclarationSite(4, 4, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {"v": "m"}
    assert res.conflicts == []


def test_pre_and_post_disagree_records_conflict():
    scan = _scan(
        annotations=[
            RawAnnotation(AnnotationKind.PRE, 3, 1, "m"),
            RawAnnotation(AnnotationKind.POST, 4, 20, "kg"),
        ],
        pre_block_lines=[3],
        declarations=[DeclarationSite(4, 4, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {"v": "m"}
    assert len(res.conflicts) == 1
    c = res.conflicts[0]
    assert (c.variable, c.first_unit, c.second_unit) == ("v", "m", "kg")
