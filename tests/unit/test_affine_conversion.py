"""``@unit_affine_conversion{ <src> -> <tgt> }`` — the verified affine
conversion directive (Phase 2c, scale.md §11).

Unlike ``@unit_assume`` (a *trusted* escape for the irreducible), this
directive is *verified*: DimFort knows both units' offsets, so it checks the
annotated assignment actually performs the ``src→tgt`` conversion. A valid
directive suppresses the ``S002`` the statement would otherwise raise; an
invalid one (wrong direction / constant / target, non-affine pair, non-linear
RHS) fires ``S003`` (error). The whole layer is opt-in (``scale_mode``).
"""
from __future__ import annotations

from dimfort.core import unit_config  # noqa: F401 — installs DEFAULT_TABLE
from dimfort.core.multifile import check_files

# A self-contained module with the temperature declarations the corpus needs.
# ``RTT`` is the canonical 273.15 K conversion constant (as commonly used in
# real-world Fortran climate codebases).
_PREAMBLE = (
    "module m\n"
    " real, parameter :: RTT = 273.15  !< @unit{K}\n"
    " contains\n"
    "  subroutine s()\n"
    "   real :: t_k   !< @unit{K}\n"
    "   real :: t_c   !< @unit{degC}\n"
    "   real :: t_c2  !< @unit{degC}\n"
    "   real :: other !< @unit{1}\n"
)
_EPILOGUE = "  end subroutine\n end module\n"


def _write(tmp_path, body: str):
    p = tmp_path / "m.f90"
    p.write_text(_PREAMBLE + f"   {body}\n" + _EPILOGUE)
    return p


def _codes(tmp_path, body: str, *, scale_mode: bool = True):
    f = _write(tmp_path, body)
    res = check_files([f], scale_mode=scale_mode)
    return [d.code for d in res.diagnostics[f.resolve()]]


# ---------------------------------------------------------------------------
# Valid forms — silent (S002 suppressed, no S003)
# ---------------------------------------------------------------------------


def test_valid_forward_is_silent(tmp_path):
    """``t_k = t_c + RTT  !< @unit_affine_conversion{degC -> K}`` verifies:
    a*=1, b*=273.15 match the RHS → no S002, no S003."""
    codes = _codes(tmp_path, "t_k = t_c + RTT  !< @unit_affine_conversion{degC -> K}")
    assert "S002" not in codes, codes
    assert "S003" not in codes, codes


def test_valid_forward_without_directive_fires_s002(tmp_path):
    """Guard: the same statement *without* the directive fires S002 (the
    untyped-offset caveat) — proving the directive is what suppresses it."""
    codes = _codes(tmp_path, "t_k = t_c + RTT")
    assert "S002" in codes, codes


def test_valid_comma_synonym_is_silent(tmp_path):
    """``{degC, K}`` (comma) is an accepted synonym for ``{degC -> K}``."""
    codes = _codes(tmp_path, "t_k = t_c + RTT  !< @unit_affine_conversion{degC, K}")
    assert "S002" not in codes and "S003" not in codes, codes


def test_valid_reverse_is_silent(tmp_path):
    """``t_c = t_k - RTT  !< @{K -> degC}`` — RTT (typed K, same frame as the
    source) is the 273.15 *constant*, not a second source operand. a*=1,
    b*=-273.15 match → silent."""
    codes = _codes(tmp_path, "t_c = t_k - RTT  !< @unit_affine_conversion{K -> degC}")
    assert "S002" not in codes and "S003" not in codes, codes


def test_valid_via_function_body_silent_callers_clean(tmp_path):
    """The recommended idiom (§11.6): a ``c_to_k`` conversion function whose
    one body line is verified; callers are checked against the clean typed
    signature."""
    src = (
        "module m\n"
        " real, parameter :: RTT = 273.15  !< @unit{K}\n"
        " contains\n"
        "  real function c_to_k(t) result(tk)\n"
        "    real, intent(in) :: t   !< @unit{degC}\n"
        "    real             :: tk  !< @unit{K}\n"
        "    tk = t + RTT            !< @unit_affine_conversion{degC -> K}\n"
        "  end function\n"
        "  subroutine s()\n"
        "    real :: x_c !< @unit{degC}\n"
        "    real :: x_k !< @unit{K}\n"
        "    x_k = c_to_k(x_c)\n"
        "  end subroutine\n end module\n"
    )
    f = tmp_path / "m.f90"
    f.write_text(src)
    res = check_files([f], scale_mode=True)
    codes = [d.code for d in res.diagnostics[f.resolve()]]
    assert "S002" not in codes and "S003" not in codes, codes


# ---------------------------------------------------------------------------
# Invalid forms — fire S003 (error)
# ---------------------------------------------------------------------------


def test_wrong_direction_fires_s003(tmp_path):
    """``t_k = t_c - RTT  !< @{degC -> K}`` — b=-273.15, expected +273.15."""
    codes = _codes(tmp_path, "t_k = t_c - RTT  !< @unit_affine_conversion{degC -> K}")
    assert "S003" in codes, codes


def test_wrong_constant_fires_s003(tmp_path):
    """``t_k = t_c + 100.  !< @{degC -> K}`` — b=100 ≠ 273.15."""
    codes = _codes(tmp_path, "t_k = t_c + 100.  !< @unit_affine_conversion{degC -> K}")
    assert "S003" in codes, codes


def test_wrong_target_fires_s003(tmp_path):
    """LHS typed degC but directive declares target K → target mismatch."""
    codes = _codes(tmp_path, "t_c = t_c2 + RTT  !< @unit_affine_conversion{degC -> K}")
    assert "S003" in codes, codes


def test_non_affine_pair_fires_s003(tmp_path):
    """``{Pa -> hPa}`` is a multiplicative (offset-0) pair — rejected; use a
    typed PARAMETER for multiplicative conversions."""
    codes = _codes(tmp_path, "t_k = t_c + RTT  !< @unit_affine_conversion{Pa -> hPa}")
    assert "S003" in codes, codes


def test_non_linear_rhs_fires_s003(tmp_path):
    """``t_k = t_c * other + RTT`` is not affine-linear in a single degC
    operand with constant coefficients."""
    codes = _codes(
        tmp_path, "t_k = t_c * other + RTT  !< @unit_affine_conversion{degC -> K}"
    )
    assert "S003" in codes, codes


def test_extra_source_operand_fires_s003(tmp_path):
    """Two degC operands (``t_c + t_c2``) → need exactly one source."""
    codes = _codes(
        tmp_path, "t_k = t_c + t_c2  !< @unit_affine_conversion{degC -> K}"
    )
    assert "S003" in codes, codes


def test_unknown_unit_in_directive_fires_s003(tmp_path):
    """A directive unit that doesn't resolve → S003 (not a scan error)."""
    codes = _codes(
        tmp_path, "t_k = t_c + RTT  !< @unit_affine_conversion{degZ -> K}"
    )
    assert "S003" in codes, codes


# ---------------------------------------------------------------------------
# Gating + malformed
# ---------------------------------------------------------------------------


def test_directive_inert_when_scale_off(tmp_path):
    """Scale off (default) ⇒ the whole scale family, directive included, is
    inert: even a wrong conversion is silent (no S002, no S003)."""
    codes = _codes(
        tmp_path,
        "t_k = t_c - RTT  !< @unit_affine_conversion{degC -> K}",
        scale_mode=False,
    )
    assert "S002" not in codes and "S003" not in codes, codes


def test_malformed_directive_is_reported(tmp_path):
    """A directive missing the ``->``/``,`` separator is a malformed-scan
    error (U001), not a silent drop."""
    codes = _codes(tmp_path, "t_k = t_c + RTT  !< @unit_affine_conversion{degC K}")
    assert "U001" in codes, codes


def test_h010_on_affine_target_suggests_parseable_unit(tmp_path):
    """A literal added to an absolute temperature (``t_c + 100.``) casts to
    the degC frame. The H010 message *describes* it as ``K + 273.15`` but its
    copy-pasteable ``@unit{...}`` suggestion must stay valid syntax (``K``) —
    ``K + 273.15`` would not parse. See format_unit show_offset."""
    f = _write(tmp_path, "t_k = t_c + 100.")
    res = check_files([f], scale_mode=True)
    h010 = [d for d in res.diagnostics[f.resolve()] if d.code == "H010"]
    assert len(h010) == 1, [d.code for d in res.diagnostics[f.resolve()]]
    msg = h010[0].message
    assert "to K + 273.15" in msg, msg          # description shows the offset
    assert "@unit{K}" in msg, msg               # suggestion is parseable
    assert "@unit{K + 273.15}" not in msg, msg  # …and never the invalid form
