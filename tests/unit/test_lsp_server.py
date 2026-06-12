"""Unit tests for the LSP server's pure helpers.

The full LSP roundtrip needs a real client; we only test the
conversion layer here.
"""
from pathlib import Path

import pytest

pytest.importorskip("pygls")

from types import SimpleNamespace

from lsprotocol import types as lsp

import dimfort.core.unit_config  # noqa: F401  — initialise DEFAULT_TABLE
from dimfort.core import diagnostics as _diagnostics
from dimfort.core.diagnostics import (
    Diagnostic,
    Position,
    Severity,
    set_severity_overrides,
)
from dimfort.lsp.server import (
    _initialize,
    _to_lsp_diagnostic,
)
from dimfort.lsp.tree_access import _uri_to_path
from dimfort.lsp.tree_nav import _normalized_unit


def _diag(line, col, code="H001", severity=Severity.ERROR, msg="msg"):
    return Diagnostic(
        file="/x.f90",
        start=Position(line, col),
        end=Position(line, col),
        severity=severity,
        code=code,
        message=msg,
    )


def test_uri_to_path_decodes_file_uri():
    assert _uri_to_path("file:///tmp/foo.f90") == Path("/tmp/foo.f90")


def test_uri_to_path_decodes_percent_encoded():
    assert _uri_to_path("file:///tmp/has%20space.f90") == Path("/tmp/has space.f90")


def test_uri_to_path_rejects_non_file_scheme():
    assert _uri_to_path("untitled:Untitled-1") is None


def test_uri_to_path_strips_leading_slash_before_drive_letter():
    """Windows URIs from ``Path.as_uri()`` look like ``file:///C:/...``;
    the leading slash before the drive letter is a URL-path artefact
    and must be stripped so the resulting Path matches what
    ``Path("C:/...").resolve()`` produced. Regression for a Windows-
    only failure in the LSP hover / goto-def test suite.
    """
    assert _uri_to_path("file:///C:/Users/foo.f90") == Path("C:/Users/foo.f90")
    assert _uri_to_path("file:///D:/x.F90") == Path("D:/x.F90")
    # Lowercase drive letters in URIs are tolerated.
    assert _uri_to_path("file:///c:/x.f90") == Path("c:/x.f90")
    # POSIX paths are untouched.
    assert _uri_to_path("file:///tmp/foo.f90") == Path("/tmp/foo.f90")


def test_to_lsp_diagnostic_converts_to_zero_based():
    d = _to_lsp_diagnostic(_diag(line=5, col=3))
    assert d.range.start.line == 4
    assert d.range.start.character == 2


def test_to_lsp_diagnostic_widens_zero_length_range():
    # A point range (start == end) is widened by one column so the
    # editor draws a squiggle.
    d = _to_lsp_diagnostic(_diag(line=5, col=3))
    assert d.range.end.character == d.range.start.character + 1


def test_to_lsp_diagnostic_maps_severity():
    err = _to_lsp_diagnostic(_diag(1, 1, severity=Severity.ERROR))
    warn = _to_lsp_diagnostic(_diag(1, 1, severity=Severity.WARNING))
    assert err.severity is lsp.DiagnosticSeverity.Error
    assert warn.severity is lsp.DiagnosticSeverity.Warning


def test_to_lsp_diagnostic_preserves_code_and_source():
    d = _to_lsp_diagnostic(_diag(1, 1, code="H002", msg="bad"))
    assert d.code == "H002"
    assert d.source == "DimFort"
    assert d.message == "bad"


def test_to_lsp_diagnostic_handles_zero_lines():
    # Whole-file diagnostics (e.g. U007) have line 0; must not go negative.
    d = _to_lsp_diagnostic(_diag(line=0, col=0))
    assert d.range.start.line == 0
    assert d.range.start.character == 0


def test_normalized_unit_surfaces_scale_factor():
    # The panel shows the input unit; the normalized form expands derived
    # units to base SI and — with scale mode on — surfaces the otherwise-
    # invisible multiplicative scale factor. Off-mode hides the factor
    # (the linter ignores scale, so displays shouldn't claim otherwise).
    assert _normalized_unit("hPa", scale_mode=True) == "100×kg·m⁻¹·s⁻²"
    assert _normalized_unit("hPa", scale_mode=False) == "kg·m⁻¹·s⁻²"
    assert _normalized_unit("Pa") == "kg·m⁻¹·s⁻²"       # derived expanded
    assert _normalized_unit("kg/kg") == "1"             # dimensionless


def test_normalized_unit_unchanged_for_base_si():
    # A base-SI annotation normalizes to itself, so the panel can suppress
    # the redundant "m = m" and show just "m".
    assert _normalized_unit("m") == "m"
    assert _normalized_unit("m/s") == "m·s⁻¹"


def test_normalized_unit_returns_none_on_parse_failure():
    assert _normalized_unit("not a unit {{{") is None


def test_initialize_applies_diagnostic_severity_overrides(tmp_path):
    # Regression: the LSP must call set_severity_overrides at initialize.
    # finalize_diagnostics reads a process-wide global, so without this
    # the editor silently ignores every [diagnostics] override (only the
    # CLI used to set it). _initialize touches `ls` only via _notify,
    # which tolerates ls=None.
    (tmp_path / "dimfort.toml").write_text(
        '[diagnostics]\nS001 = "error"\nH001 = "off"\n'
    )
    set_severity_overrides({})  # start clean
    params = SimpleNamespace(
        workspace_folders=None,
        root_uri=tmp_path.as_uri(),
        initialization_options=None,
    )
    try:
        _initialize(None, params)
        assert _diagnostics._severity_overrides == {"S001": "error", "H001": "off"}
    finally:
        set_severity_overrides({})  # don't leak into other tests
