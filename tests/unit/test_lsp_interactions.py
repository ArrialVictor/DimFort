"""Tests for the dimfort/interactions LSP helpers.

The full request handler is exercised via the editor client; here we cover
the pure pieces — cursor→symbol resolution and report serialisation — like
``test_panel_info.py`` does for the panel builders.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pygls = pytest.importorskip("pygls")  # noqa: F841


def _src(tmp_path: Path) -> Path:
    f = tmp_path / "i.f90"
    f.write_text(
        "subroutine s(x, y, z)\n"
        "  real :: x\n"
        "  real :: y  !< @unit{1/s}\n"
        "  real :: z  !< @unit{1/s}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    return f


def test_identifier_at_resolves_symbol_under_cursor(tmp_path: Path):
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _identifier_at

    f = _src(tmp_path)
    src = f.read_bytes()
    tree = _ts.parse_text(src)
    # `x` on line 5 (1-based), at its column in `z = x + y`.
    assert _identifier_at(tree, src, 5, 7) == "x"


def test_identifier_at_returns_none_off_identifier(tmp_path: Path):
    from dimfort.core import ts_parser as _ts
    from dimfort.lsp.tree_nav import _identifier_at

    f = _src(tmp_path)
    src = f.read_bytes()
    tree = _ts.parse_text(src)
    # The `+` operator position — not an identifier.
    assert _identifier_at(tree, src, 5, 9) is None


def test_serialize_interaction_point_shape(tmp_path: Path):
    from dimfort.core.interactions import InteractionPoint
    from dimfort.lsp.interactions import _serialize_interaction_point

    p = InteractionPoint(
        file="i.f90", line=5, column=7, scope="s",
        kind="requires", unit=None, snippet="z = x + y",
    )
    d = _serialize_interaction_point(p)
    assert d == {
        "file": "i.f90", "line": 5, "column": 7, "scope": "s",
        "kind": "requires", "unit": "?", "snippet": "z = x + y",
    }
