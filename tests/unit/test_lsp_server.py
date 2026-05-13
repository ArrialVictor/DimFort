"""Unit tests for the LSP server's pure helpers.

The full LSP roundtrip needs a real client; we only test the
conversion layer here.
"""
from pathlib import Path

import pytest

pytest.importorskip("pygls")

from lsprotocol import types as lsp

from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.lsp.server import _to_lsp_diagnostic, _uri_to_path


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
    assert d.source == "dimfort"
    assert d.message == "bad"


def test_to_lsp_diagnostic_handles_zero_lines():
    # Whole-file diagnostics (e.g. U007) have line 0; must not go negative.
    d = _to_lsp_diagnostic(_diag(line=0, col=0))
    assert d.range.start.line == 0
    assert d.range.start.character == 0
