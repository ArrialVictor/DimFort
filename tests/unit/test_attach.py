"""Tests for the annotation→declaration attachment pass (stage 2)."""
from dimfort.core.annotations import (
    AnnotationKind,
    DeclarationSite,
    RawAnnotation,
    ScanResult,
    scan_text,
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


# ---------- U010 — intermediate continuation line --------------------------


def test_post_on_intermediate_continuation_line_is_rejected():
    # `real :: a, &
    #          b, &      !<  HERE — interior of the continuation
    #          c`
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 2, 30, "m")],
        declarations=[DeclarationSite(1, 3, ("a", "b", "c"))],
    )
    res = attach(scan)
    assert res.var_units == {}, "U010 must NOT apply the annotation"
    assert len(res.intermediate_continuations) == 1
    rec = res.intermediate_continuations[0]
    assert rec.line == 2
    assert rec.declaration_line_start == 1
    assert rec.declaration_line_end == 3
    assert "intermediate" in rec.reason


def test_post_on_first_line_of_continuation_is_not_u010():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 1, 30, "kg")],
        declarations=[DeclarationSite(1, 3, ("a", "b", "c"))],
    )
    res = attach(scan)
    assert res.var_units == {"a": "kg", "b": "kg", "c": "kg"}
    assert res.intermediate_continuations == []


def test_post_on_last_line_of_continuation_is_not_u010():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 3, 30, "Pa")],
        declarations=[DeclarationSite(1, 3, ("a", "b", "c"))],
    )
    res = attach(scan)
    assert res.var_units == {"a": "Pa", "b": "Pa", "c": "Pa"}
    assert res.intermediate_continuations == []


def test_single_line_declaration_is_not_u010():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 5, 20, "m")],
        declarations=[DeclarationSite(5, 5, ("v",))],
    )
    res = attach(scan)
    assert res.var_units == {"v": "m"}
    assert res.intermediate_continuations == []


# ---------- derived-type field annotations ---------------------------------


def test_field_annotation_goes_into_field_units_not_var_units():
    scan = _scan(
        annotations=[RawAnnotation(AnnotationKind.POST, 2, 20, "kg")],
        declarations=[
            DeclarationSite(2, 2, ("m",), enclosing_type="particle"),
        ],
    )
    res = attach(scan)
    assert res.var_units == {}
    assert res.field_units == {("particle", "m"): "kg"}


def test_field_and_local_with_same_name_dont_collide():
    scan = _scan(
        annotations=[
            RawAnnotation(AnnotationKind.POST, 2, 20, "kg"),   # field
            RawAnnotation(AnnotationKind.POST, 5, 20, "m"),     # local
        ],
        declarations=[
            DeclarationSite(2, 2, ("m",), enclosing_type="particle"),
            DeclarationSite(5, 5, ("m",), enclosing_type=None),
        ],
    )
    res = attach(scan)
    assert res.var_units == {"m": "m"}
    assert res.field_units == {("particle", "m"): "kg"}


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


# ---------------------------------------------------------------------------
# Intrinsic-type default dim'less (INTEGER / LOGICAL / CHARACTER)
# ---------------------------------------------------------------------------


def test_integer_decl_defaults_to_dimless():
    src = (
        "subroutine s\n"
        "  integer :: i, j\n"
        "  real :: r            !< @unit{m/s}\n"
        "end subroutine\n"
    )
    result = attach(scan_text(src))
    assert result.var_units["i"] == "1"
    assert result.var_units["j"] == "1"
    assert result.var_units["r"] == "m/s"
    # Scope-aware view carries the same entries.
    assert result.var_units_by_scope[("s", "i")] == "1"
    assert result.var_units_by_scope[("s", "j")] == "1"


def test_explicit_integer_annotation_wins_over_default():
    """``integer :: t  !< @unit{s}`` keeps ``s``, not the dim'less default."""
    src = "integer :: t   !< @unit{s}\n"
    result = attach(scan_text(src))
    assert result.var_units["t"] == "s"


def test_logical_decl_defaults_to_dimless():
    src = "logical :: flag\n"
    result = attach(scan_text(src))
    assert result.var_units["flag"] == "1"


def test_character_decl_defaults_to_dimless():
    src = "character(len=10) :: name\n"
    result = attach(scan_text(src))
    assert result.var_units["name"] == "1"


def test_real_decl_does_not_default():
    """Unannotated ``real ::`` declarations stay out of the table —
    they're the U005-eligible population."""
    src = "real :: x\n"
    result = attach(scan_text(src))
    assert "x" not in result.var_units


def test_complex_decl_does_not_default():
    """``complex`` carries physical measurements (impedance, wave
    amplitudes) — no dim'less default."""
    src = "complex :: z\n"
    result = attach(scan_text(src))
    assert "z" not in result.var_units


def test_integer_field_of_derived_type_does_not_default():
    """Fields of a derived type keep needing explicit annotation —
    the dim'less default is local-only."""
    src = (
        "type :: state\n"
        "  integer :: counter\n"
        "end type\n"
    )
    result = attach(scan_text(src))
    assert "counter" not in result.var_units
    assert ("state", "counter") not in result.field_units


# ---------- var_units_span uses RawAnnotation.end_column ------------------


def test_var_units_span_uses_raw_annotation_end_column():
    """var_units_span must mirror the RawAnnotation's end_column rather
    than re-deriving from unit_text length — otherwise configurable
    comment delimiters (e.g. ``[m/s]``) get the wrong span and any U002
    squiggle / LSP hover range lands in the wrong column.

    The canonical ``@unit{m/s}`` ends 7 columns after the start
    (``@unit{`` + ``m/s`` + ``}``), but a ``[m/s]`` annotation ends only
    5 columns after the start. The span must reflect whichever
    delimiters the scanner saw — which is exactly what
    RawAnnotation.end_column already carries."""
    scan = _scan(
        annotations=[
            RawAnnotation(
                AnnotationKind.POST,
                line=3, column=12, unit_text="m/s",
                end_column=17,  # custom: 12 + len("[m/s]") - non-canonical span
            ),
        ],
        declarations=[DeclarationSite(3, 3, ("v",))],
    )
    res = attach(scan)
    assert res.var_units_span["v"] == (3, 12, 17)
