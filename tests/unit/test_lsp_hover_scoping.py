"""Hover resolves identifiers honouring their enclosing routine scope.

Two routines in one file declaring same-named parameters with
different units used to confuse the bare-identifier hover path
(``merged_var_units`` is first-seen-wins). The scope-aware lookup
keeps each routine's annotation distinct.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pygls")

from dimfort.core import unit_config  # noqa: F401
from dimfort.core.multifile import check_files
from dimfort.lsp import server as _server


def _drive_hover(file: Path, line_1based: int, col_1based: int):
    """Populate ``_last_result`` and dispatch ``_resolve_hover`` directly.

    Bypasses pygls so we can exercise the resolver against a real
    workset without spinning a client/server pair.
    """
    result = check_files([file])
    with _server._last_result_lock:
        _server._last_result = result
    uri = file.resolve().as_uri()
    try:
        return _server._resolve_hover(uri, line_1based, col_1based, None)
    finally:
        with _server._last_result_lock:
            _server._last_result = None


def test_hover_picks_per_routine_unit(tmp_path: Path):
    """Hover on ``pte`` inside ``orodrag`` vs ``orolift`` shows each
    routine's own annotation, not the first-seen across the file."""
    src = (
        "subroutine orodrag(pte)\n"           # line 1
        "  real :: pte  !< @unit{m/s}\n"      # line 2 — declared m/s
        "  pte = 0.0\n"                       # line 3 — usage
        "end subroutine\n"                    # line 4
        "subroutine orolift(pte)\n"           # line 5
        "  real :: pte  !< @unit{K/s}\n"      # line 6 — declared K/s
        "  pte = 0.0\n"                       # line 7 — usage
        "end subroutine\n"                    # line 8
    )
    f = tmp_path / "scoped_hover.f90"
    f.write_text(src)

    # Hover the ``pte`` on line 3 (inside orodrag) — column points at
    # the first character of the name (1-based).
    res_m = _drive_hover(f, line_1based=3, col_1based=3)
    assert res_m is not None
    text_m, _ = res_m
    assert "m / s" in text_m or "m/s" in text_m or "ᐟs" in text_m, text_m
    assert "K" not in text_m, text_m

    # Hover the ``pte`` on line 7 (inside orolift).
    res_k = _drive_hover(f, line_1based=7, col_1based=3)
    assert res_k is not None
    text_k, _ = res_k
    assert "K" in text_k, text_k
