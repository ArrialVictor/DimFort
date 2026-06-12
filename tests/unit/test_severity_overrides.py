"""Tests for the [diagnostics] severity-override mechanism.

A project's ``dimfort.toml`` can promote a warning to an error, demote
an error to a warning, or silence either entirely. Overrides are keyed
by diagnostic code (``H001``, ``H010``) or rule marker (``D1.4``,
``D1.7``); markers take precedence.
"""
from __future__ import annotations

from dimfort.core import ts_checker, unit_config  # noqa: F401
from dimfort.core import ts_parser as ts
from dimfort.core.diagnostics import (
    Severity,
    finalize_diagnostics,
    set_severity_overrides,
)


def _check(src: bytes, var_units: dict[str, str]) -> list:
    tree = ts.parse_text(src)
    return ts_checker.check(
        tree, var_units, source=src, file="t.f90",
    )


def teardown_function() -> None:
    """Reset overrides between tests so they don't leak."""
    set_severity_overrides({})


def test_no_override_keeps_default_severity():
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    diags = _check(src, {"a": "m/s", "b": "kg"})
    h001 = next(d for d in diags if d.code == "H001")
    assert h001.severity is Severity.ERROR


def test_demote_h001_to_warning_via_code():
    set_severity_overrides({"H001": "warning"})
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    diags = _check(src, {"a": "m/s", "b": "kg"})
    h001 = next(d for d in diags if d.code == "H001")
    assert h001.severity is Severity.WARNING


def test_off_drops_the_diagnostic_entirely():
    set_severity_overrides({"H001": "off"})
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    diags = _check(src, {"a": "m/s", "b": "kg"})
    assert all(d.code != "H001" for d in diags)


def test_rule_marker_takes_precedence_over_code():
    """``D1.4`` override should NOT affect a plain ``H001`` (assignment
    mismatch) — only H001-with-D1.4-marker diagnostics."""
    set_severity_overrides({"D1.4": "warning"})
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    diags = _check(src, {"a": "m/s", "b": "kg"})
    # Plain H001 (no D1.4 marker) stays as error.
    h001 = next(d for d in diags if d.code == "H001" and "D1.4" not in d.message)
    assert h001.severity is Severity.ERROR


def test_d17_default_warning_promoted_to_error():
    """D1.7 ships as a warning; the override flips it to error."""
    set_severity_overrides({"D1.7": "error"})
    src = (
        b"subroutine s\n"
        b"  real :: speed\n"
        b"  real :: r\n"
        b"  r = 2.0 ** speed\n"
        b"end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "r": "1"})
    d17 = next(d for d in diags if "D1.7" in d.message)
    assert d17.severity is Severity.ERROR


def test_d17_default_warning_preserved_without_override():
    """Without override, D1.7 fires as a WARNING (default)."""
    src = (
        b"subroutine s\n"
        b"  real :: speed\n"
        b"  real :: r\n"
        b"  r = 2.0 ** speed\n"
        b"end subroutine\n"
    )
    diags = _check(src, {"speed": "m/s", "r": "1"})
    d17 = next(d for d in diags if "D1.7" in d.message)
    assert d17.severity is Severity.WARNING


def test_finalize_diagnostics_is_pure():
    """finalize_diagnostics returns a new list; originals are frozen."""
    from dimfort.core.diagnostics import Diagnostic, Position
    set_severity_overrides({"H001": "warning"})
    d = Diagnostic(
        file="x.f90",
        start=Position(1, 1), end=Position(1, 5),
        severity=Severity.ERROR, code="H001", message="test",
    )
    out = finalize_diagnostics([d])
    assert out[0].severity is Severity.WARNING
    # Original Diagnostic is frozen — unchanged.
    assert d.severity is Severity.ERROR


def test_demote_h001_to_info():
    """``info`` is a valid override value (added 0.3.0); previously
    silently dropped by the config parser. Verifies both that the
    override is honoured and that Severity.INFO is the resulting value."""
    set_severity_overrides({"H001": "info"})
    src = b"subroutine s\n  real :: a, b\n  a = b\n end subroutine\n"
    diags = _check(src, {"a": "m/s", "b": "kg"})
    h001 = next(d for d in diags if d.code == "H001")
    assert h001.severity is Severity.INFO


def test_config_parser_accepts_info():
    """``[diagnostics] U021 = "info"`` — the literal example shipped in
    docs/reference/dimfort-toml.md — must round-trip through the config
    loader without being silently dropped."""
    import tempfile
    from pathlib import Path

    from dimfort.config import load_config

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "dimfort.toml"
        p.write_text('[diagnostics]\nU021 = "info"\n')
        cfg = load_config(Path(td))
        assert cfg.diagnostic_severities.get("U021") == "info"
