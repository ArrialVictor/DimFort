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


def test_stats_workspace_scope_aggregates_across_files(tmp_path: Path):
    """Without a ``uri``, stats runs a workspace-wide check over the index.

    Distinct from the file-scope path, which reads from
    ``state.last_result``. Workspace scope does its own
    ``check_files`` run so the WS aggregate is a stable project-level
    number, not a function of which file the user is editing.
    """
    from dimfort.core.workspace_index import scan_workspace
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    # Two-file workspace so the aggregate covers more than one file.
    _clean_src(tmp_path)
    other = tmp_path / "other.f90"
    other.write_text(
        "subroutine other(a, b, c)\n"
        "  real :: a  !< @unit{kg}\n"
        "  real :: b  !< @unit{kg}\n"
        "  real :: c  !< @unit{kg}\n"
        "  c = a + b\n"
        "end subroutine\n"
    )
    index = scan_workspace([tmp_path])
    saved_idx = state.workspace_index
    state.workspace_index = index
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        state.workspace_index = saved_idx

    assert payload is not None
    assert payload["scope"] == "workspace"
    assert "files" in payload
    assert "total" in payload
    total = payload["total"]
    assert set(total.keys()) == {"ok", "warn", "fire", "unparsed", "out", "coverage_pct"}
    # Both files fully annotated: aggregate ok count covers both.
    assert total["ok"] >= 2
    assert len(payload["files"]) == 2


def test_stats_workspace_scope_returns_empty_without_index():
    """Workspace scope falls back to a zero payload when the index isn't ready."""
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    saved_idx = state.workspace_index
    state.workspace_index = None
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        state.workspace_index = saved_idx

    assert payload is not None
    assert payload["scope"] == "workspace"
    assert payload["files"] == []
    assert payload["total"]["ok"] == 0
    assert payload["total"]["coverage_pct"] == 0.0


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
    """Without a workspace index, the workspace scope returns zeroed totals."""
    from dimfort.lsp import coverage
    from dimfort.lsp.state import state

    saved_result = state.last_result
    saved_idx = state.workspace_index
    state.last_result = None
    state.workspace_index = None
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        state.last_result = saved_result  # type: ignore[assignment]
        state.workspace_index = saved_idx

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
