"""Tests for the ``dimfort/lineStatus`` and ``dimfort/coverageStats`` LSP wrappers.

Covers the thin translation layer in :mod:`dimfort.lsp.coverage`. The
core projection logic is tested separately in ``test_coverage.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pygls = pytest.importorskip("pygls")  # noqa: F841


def _clean_src(tmp_path: Path) -> Path:
    f = tmp_path / "clean.f90"
    f.write_text(
        "subroutine clean(x, y, z)\n"
        "  real :: x  !< @unit{m}\n"
        "  real :: y  !< @unit{m}\n"
        "  real :: z  !< @unit{m}\n"
        "  z = x + y\n"
        "end subroutine\n"
    )
    return f


def _set_last_result(result: object) -> None:
    """Install ``result`` on the module-level LSP state, restoring on teardown.

    Tests should call this from inside a finally / fixture cleanup
    to avoid leaking state across the suite.
    """
    from dimfort.lsp.state import state

    state.last_result = result  # type: ignore[assignment]


def _reset_ws_async_state() -> None:
    """Reset coverage module's workspace cache.

    ``_ws_result_cache`` persists at module level across tests;
    without an explicit reset, earlier tests' results leak into
    later ones. Tests touching the workspace-scope stats path
    should call this in setup and teardown.
    """
    from dimfort.lsp import coverage

    coverage._ws_result_cache = None


# ---------------------------------------------------------------------------
# resolve (dimfort/lineStatus)
# ---------------------------------------------------------------------------


def test_resolve_returns_lines_for_known_file(tmp_path: Path):
    """A fully-annotated file: response includes the green decl + use lines."""
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    src = _clean_src(tmp_path)
    result = check_files([src])
    saved = state.last_result
    _set_last_result(result)
    try:
        params = {"uri": src.resolve().as_uri()}
        payload = coverage.resolve(None, params)  # type: ignore[arg-type]
    finally:
        state.last_result = saved  # type: ignore[assignment]

    assert payload is not None
    assert payload["uri"] == src.resolve().as_uri()
    # Lines list is sorted by line number.
    line_nums = [entry["line"] for entry in payload["lines"]]
    assert line_nums == sorted(line_nums)
    # Every entry has the wire-format shape.
    for entry in payload["lines"]:
        assert set(entry.keys()) == {"line", "status"}
        assert entry["status"] in {"green", "yellow", "red", "blue"}


def test_resolve_returns_none_on_missing_uri():
    """A request with no ``uri`` yields ``None``."""
    from dimfort.lsp import coverage

    assert coverage.resolve(None, {}) is None  # type: ignore[arg-type]


def test_resolve_returns_empty_lines_when_no_cached_result():
    """Before the first check completes, returning an empty list is the
    documented behaviour — companions render no decoration."""
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    saved = state.last_result
    state.last_result = None
    try:
        params = {"uri": "file:///nonexistent.f90"}
        payload = coverage.resolve(None, params)  # type: ignore[arg-type]
    finally:
        state.last_result = saved  # type: ignore[assignment]

    assert payload is not None
    assert payload["lines"] == []


def test_resolve_returns_empty_for_uri_not_in_workset(tmp_path: Path):
    """A request for a file not in the workset returns an empty list,
    matching the documented behaviour."""
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    src = _clean_src(tmp_path)
    result = check_files([src])
    saved = state.last_result
    _set_last_result(result)
    try:
        other = tmp_path / "not-checked.f90"
        params = {"uri": other.as_uri()}
        payload = coverage.resolve(None, params)  # type: ignore[arg-type]
    finally:
        state.last_result = saved  # type: ignore[assignment]

    assert payload is not None
    assert payload["lines"] == []


# ---------------------------------------------------------------------------
# stats (dimfort/coverageStats)
# ---------------------------------------------------------------------------


def test_stats_workspace_scope_serves_from_cache(tmp_path: Path):
    """Workspace stats reads ``_ws_result_cache`` directly — no auto-refresh.

    Stats handler is a pure cache read; the cache is populated by
    explicit calls to :func:`seed_workspace_cache`. Tests
    seed the cache directly to exercise the projection + payload
    shape without running the full check.
    """
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage

    src = _clean_src(tmp_path)
    other = tmp_path / "other.f90"
    other.write_text(
        "subroutine other(a, b, c)\n"
        "  real :: a  !< @unit{kg}\n"
        "  real :: b  !< @unit{kg}\n"
        "  real :: c  !< @unit{kg}\n"
        "  c = a + b\n"
        "end subroutine\n"
    )
    result = check_files([src, other])

    _reset_ws_async_state()
    coverage._ws_result_cache = result
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        _reset_ws_async_state()

    assert payload is not None
    assert payload["scope"] == "workspace"
    total = payload["total"]
    assert set(total.keys()) == {"ok", "warn", "fire", "unparsed", "out", "coverage_pct"}
    # Both files fully annotated: aggregate ok count covers both.
    assert total["ok"] >= 2
    assert len(payload["files"]) == 2


def test_stats_workspace_scope_returns_empty_when_cache_unset():
    """Cold start: no manual refresh yet — return zeroed payload."""
    from dimfort.lsp import coverage

    _reset_ws_async_state()
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        _reset_ws_async_state()

    assert payload is not None
    assert payload["scope"] == "workspace"
    assert payload["files"] == []
    assert payload["total"]["ok"] == 0


def test_stats_file_scope_with_uri_returns_single_file(tmp_path: Path):
    """With a ``uri``, stats covers only that file and tags scope as 'file'."""
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    src = _clean_src(tmp_path)
    result = check_files([src])
    saved = state.last_result
    _set_last_result(result)
    try:
        params = {"uri": src.resolve().as_uri()}
        payload = coverage.stats(None, params)  # type: ignore[arg-type]
    finally:
        state.last_result = saved  # type: ignore[assignment]

    assert payload is not None
    assert payload["scope"] == "file"
    assert payload["uri"] == src.resolve().as_uri()
    assert len(payload["files"]) == 1


def test_stats_returns_empty_when_no_cached_result():
    """Default state (no cache, no dirty): workspace scope returns zeroed totals."""
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    saved_result = state.last_result
    saved_idx = state.workspace_index
    state.last_result = None
    state.workspace_index = None
    _reset_ws_async_state()
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        state.last_result = saved_result  # type: ignore[assignment]
        state.workspace_index = saved_idx
        _reset_ws_async_state()

    assert payload is not None
    assert payload["scope"] == "workspace"
    assert payload["files"] == []
    assert payload["total"]["ok"] == 0
    assert payload["total"]["coverage_pct"] == 0.0


# ---------------------------------------------------------------------------
# Stats cache (identity-keyed)
# ---------------------------------------------------------------------------


def test_stats_cache_hits_on_same_result(tmp_path: Path):
    """Repeat file-scope ``stats()`` calls on the same WorksetResult skip the tree walk.

    The per-file cache backs file-scope queries (``{uri: ...}``) only;
    workspace scope runs its own ``check_files`` and bypasses this
    cache (covered by the workspace-scope tests above).
    """
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    src = _clean_src(tmp_path)
    result = check_files([src])
    saved = state.last_result

    # Reset the module cache so we start clean.
    coverage._cache_result = None
    coverage._cache_files = {}
    _set_last_result(result)
    params = {"uri": src.resolve().as_uri()}
    try:
        first = coverage.stats(None, params)  # type: ignore[arg-type]
        # Cache should now contain the file's FileCoverage.
        assert coverage._cache_result is result
        assert src.resolve() in coverage._cache_files

        # Second call: same result identity → cache populated, payload identical.
        second = coverage.stats(None, params)  # type: ignore[arg-type]
        assert first == second
    finally:
        state.last_result = saved  # type: ignore[assignment]
        coverage._cache_result = None
        coverage._cache_files = {}


def test_stats_cache_invalidates_on_new_result(tmp_path: Path):
    """A new WorksetResult identity drops the previous file-scope cache entries."""
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    src = _clean_src(tmp_path)
    result_a = check_files([src])
    result_b = check_files([src])
    assert result_a is not result_b  # sanity: fresh check produces fresh object

    saved = state.last_result
    coverage._cache_result = None
    coverage._cache_files = {}
    params = {"uri": src.resolve().as_uri()}
    _set_last_result(result_a)
    try:
        coverage.stats(None, params)  # type: ignore[arg-type]
        assert coverage._cache_result is result_a

        _set_last_result(result_b)
        coverage.stats(None, params)  # type: ignore[arg-type]
        # Cache should have rotated to result_b; result_a entries dropped.
        assert coverage._cache_result is result_b
    finally:
        state.last_result = saved  # type: ignore[assignment]
        coverage._cache_result = None
        coverage._cache_files = {}


# ---------------------------------------------------------------------------
# Manual workspace refresh
# ---------------------------------------------------------------------------


def test_seed_workspace_cache_stores_result(tmp_path: Path):
    """``seed_workspace_cache`` stores the result for the stats handler."""
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage

    src = _clean_src(tmp_path)
    result = check_files([src])

    _reset_ws_async_state()
    try:
        coverage.seed_workspace_cache(result)
        assert coverage._ws_result_cache is result
    finally:
        _reset_ws_async_state()


def test_build_workspace_payload_shape(tmp_path: Path):
    """``build_workspace_payload`` returns the wire-format dict."""
    from dimfort.core.multifile import check_files
    from dimfort.lsp import coverage

    src = _clean_src(tmp_path)
    result = check_files([src])

    payload = coverage.build_workspace_payload(result)
    assert payload["scope"] == "workspace"
    assert "files" in payload
    assert "total" in payload
    # Files list contains the input; total aggregates them.
    assert len(payload["files"]) == 1
