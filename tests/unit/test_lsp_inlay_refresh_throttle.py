"""Throttle around ``workspace/inlayHint/refresh``.

Per 0.2.6 plan item #11. The throttle has two halves: a leading-edge
fire (so the first call after a quiet period is immediate) and a
trailing-edge timer (so additional calls within the interval coalesce
into one fire at the interval's end).
"""
from __future__ import annotations

import time
from unittest import mock

from dimfort.lsp import server as _s


class _FakeLS:
    """Minimal stand-in for the pygls LanguageServer.

    Tracks every ``workspace_inlay_hint_refresh`` call.
    """

    def __init__(self) -> None:
        self.calls = 0

    def workspace_inlay_hint_refresh(self, _params: object) -> None:
        self.calls += 1


def _reset_throttle_state() -> None:
    """Wipe module-level throttle state between tests."""
    _s._refresh_inlay_last_fired_at = 0.0
    if _s._refresh_inlay_pending_timer is not None:
        _s._refresh_inlay_pending_timer.cancel()
        _s._refresh_inlay_pending_timer = None


def test_leading_edge_fires_immediately() -> None:
    """The very first call fires synchronously."""
    _reset_throttle_state()
    ls = _FakeLS()
    _s._refresh_inlay_hints(ls)  # type: ignore[arg-type]
    assert ls.calls == 1


def test_burst_within_interval_coalesces_to_two() -> None:
    """Many calls inside one interval produce exactly two fires: leading + trailing."""
    _reset_throttle_state()
    # Shorten the interval so the test doesn't drag.
    with mock.patch.object(_s, "_REFRESH_INLAY_INTERVAL", 0.05):
        ls = _FakeLS()
        for _ in range(20):
            _s._refresh_inlay_hints(ls)  # type: ignore[arg-type]
        # After the leading fire, all subsequent calls are coalesced
        # into one trailing timer. Wait for it.
        time.sleep(0.20)
    assert ls.calls == 2, f"expected leading + trailing, got {ls.calls}"
    _reset_throttle_state()


def test_two_calls_far_apart_both_fire() -> None:
    """Calls separated by more than the interval both leading-fire."""
    _reset_throttle_state()
    with mock.patch.object(_s, "_REFRESH_INLAY_INTERVAL", 0.05):
        ls = _FakeLS()
        _s._refresh_inlay_hints(ls)  # type: ignore[arg-type]
        time.sleep(0.15)
        _s._refresh_inlay_hints(ls)  # type: ignore[arg-type]
    assert ls.calls == 2
    _reset_throttle_state()


def test_single_call_after_burst_does_not_double_fire() -> None:
    """A late single call should not fire twice (no leading + scheduled trailing race)."""
    _reset_throttle_state()
    with mock.patch.object(_s, "_REFRESH_INLAY_INTERVAL", 0.05):
        ls = _FakeLS()
        # Burst: leading + scheduled trailing.
        for _ in range(5):
            _s._refresh_inlay_hints(ls)  # type: ignore[arg-type]
        time.sleep(0.15)  # let trailing fire and clear
        # Quiet period elapsed → next call leading-fires only.
        _s._refresh_inlay_hints(ls)  # type: ignore[arg-type]
    # 1 leading + 1 trailing from burst, plus 1 leading from the lone call.
    assert ls.calls == 3
    _reset_throttle_state()


def test_refresh_swallows_transport_errors() -> None:
    """A client that refuses the refresh must not propagate."""
    _reset_throttle_state()

    class _BadLS:
        def workspace_inlay_hint_refresh(self, _params: object) -> None:
            raise RuntimeError("client does not support refresh")

    # Should NOT raise.
    _s._refresh_inlay_hints(_BadLS())  # type: ignore[arg-type]
    _reset_throttle_state()
