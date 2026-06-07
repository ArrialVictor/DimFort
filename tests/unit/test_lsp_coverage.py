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
    """Reset coverage module's async workspace state.

    The async refresh state (``_ws_result_cache``, ``_ws_dirty``,
    ``_ws_last_dirty_at``, ``_ws_refresh_in_flight``) persists at
    module level across tests; without an explicit reset earlier
    tests' results leak into later ones. Tests touching the
    workspace-scope stats path should call this in setup and
    teardown.
    """
    from dimfort.lsp import coverage

    coverage._ws_result_cache = None
    coverage._ws_dirty = False  # explicit: no kickoff during the test
    coverage._ws_last_dirty_at = 0.0
    coverage._ws_refresh_in_flight = False


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
    """Workspace stats returns the cached aggregate; refresh is asynchronous.

    The handler doesn't run ``check_files`` synchronously — it
    returns whatever's in ``_ws_result_cache``. Tests that need a
    real workspace aggregate seed the cache directly via the
    refresh worker, since the production path spawns a daemon
    thread we can't reliably join in a test.
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
    # Cache present + not dirty + no refresh in flight → not stale.
    assert payload["ws_stale"] is False


def test_stats_workspace_scope_returns_empty_with_stale_when_cache_unset():
    """Cold start: no cache, no kickoff possible — return empty + stale=True."""
    from dimfort.lsp import coverage

    _reset_ws_async_state()
    # Mark dirty so the handler attempts a kickoff (which will no-op
    # because the workspace_index is None on a fresh test environment).
    coverage._ws_dirty = True
    try:
        payload = coverage.stats(None, {})  # type: ignore[arg-type]
    finally:
        _reset_ws_async_state()

    assert payload is not None
    assert payload["scope"] == "workspace"
    assert payload["files"] == []
    assert payload["total"]["ok"] == 0
    assert payload["ws_stale"] is True


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
    # Cache empty, dirty False after reset → not stale.
    assert payload["ws_stale"] is False


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
# Async workspace refresh
# ---------------------------------------------------------------------------


def test_mark_workspace_dirty_sets_flag():
    """``mark_workspace_dirty`` flips ``_ws_dirty`` and stamps the timestamp."""
    import time

    from dimfort.lsp import coverage

    _reset_ws_async_state()
    try:
        assert coverage._ws_dirty is False
        coverage.mark_workspace_dirty()
        assert coverage._ws_dirty is True
        assert coverage._ws_last_dirty_at > 0.0
        # Timestamp is a monotonic clock value.
        assert coverage._ws_last_dirty_at <= time.monotonic()
    finally:
        _reset_ws_async_state()


def test_maybe_start_refresh_respects_idle_debounce():
    """A fresh dirty mark should NOT immediately trigger a refresh.

    The idle debounce keeps active typing from firing back-to-back
    refreshes. The mark must sit for ``_WS_IDLE_DEBOUNCE_S`` seconds
    before the next ``_maybe_start_refresh`` call kicks off a worker.
    """
    from dimfort.lsp import coverage

    _reset_ws_async_state()
    try:
        coverage.mark_workspace_dirty()
        # No workspace_index → worker would no-op even if it spawned,
        # but the debounce check happens first so the thread should
        # never start.
        started = coverage._maybe_start_refresh(None)
        assert started is False
        # Dirty flag stays set since the refresh didn't run.
        assert coverage._ws_dirty is True
    finally:
        _reset_ws_async_state()


def test_maybe_start_refresh_force_bypasses_debounce():
    """``force=True`` skips the idle-debounce check + spawns a worker.

    Used by the companion's manual-mode refresh: the user explicitly
    asked for fresh stats, so we don't wait for typing to settle.
    The worker still no-ops harmlessly when the workspace index is
    missing — the return value of ``_maybe_start_refresh`` tells us
    whether spawn happened, independent of how fast the worker
    drains.
    """
    import time

    from dimfort.lsp import coverage

    _reset_ws_async_state()
    try:
        # Return value is True iff a worker was actually started.
        # Reliable indicator regardless of how fast the worker drains.
        started = coverage._maybe_start_refresh(None, force=True)
        assert started is True
        # Drain the daemon thread before teardown so a racing call
        # to _reset_ws_async_state doesn't fight the worker.
        for _ in range(50):
            if not coverage._ws_refresh_in_flight:
                break
            time.sleep(0.02)
        assert coverage._ws_refresh_in_flight is False
    finally:
        _reset_ws_async_state()


def test_maybe_start_refresh_no_double_spawn():
    """Concurrent calls don't spawn two workers for the same dirty mark.

    The first ``_maybe_start_refresh`` sets ``_ws_refresh_in_flight =
    True`` *before* starting the thread, so a second call beating
    the worker to in_flight=False reliably sees in_flight=True and
    returns False. We assert the second call's return without
    relying on observing the in-flight state directly.
    """
    import threading
    import time

    from dimfort.lsp import coverage

    # Block the worker by patching _run_workspace_check to a wait
    # we can release manually — ensures the second call hits while
    # the first is in flight, regardless of system load.
    release = threading.Event()
    original = coverage._run_workspace_check
    coverage._run_workspace_check = lambda ls: release.wait(timeout=2.0) or None
    _reset_ws_async_state()
    try:
        first = coverage._maybe_start_refresh(None, force=True)
        second = coverage._maybe_start_refresh(None, force=True)
        assert first is True
        assert second is False  # second call saw in_flight=True
    finally:
        release.set()  # let the worker exit
        # Wait for the worker thread to finish so we don't leak.
        for _ in range(100):
            if not coverage._ws_refresh_in_flight:
                break
            time.sleep(0.02)
        coverage._run_workspace_check = original
        _reset_ws_async_state()


def test_stats_handler_force_refresh_param_kicks_worker():
    """``force_refresh: true`` in stats params triggers an immediate refresh.

    Verifies the wire-up: the handler reads the param, forwards
    ``force=True`` to ``_maybe_start_refresh``, and a worker spins
    up. The worker drains immediately when no workspace index is
    present.
    """
    import time

    from dimfort.lsp import coverage

    _reset_ws_async_state()
    spawned = []
    original = coverage._maybe_start_refresh

    def stub(ls, *, force=False):
        spawned.append(force)
        return original(ls, force=force)

    coverage._maybe_start_refresh = stub
    try:
        payload = coverage.stats(None, {"force_refresh": True})  # type: ignore[arg-type]
        assert payload is not None
        assert payload["scope"] == "workspace"
        assert spawned == [True]  # handler passed force=True through

        # Drain.
        for _ in range(50):
            if not coverage._ws_refresh_in_flight:
                break
            time.sleep(0.02)
    finally:
        coverage._maybe_start_refresh = original
        _reset_ws_async_state()
