"""Regression test for the silent-failure-audit cache-save daemon wrapper.

Exercises ``server._cache_save_wrapped`` against a real filesystem
permission error to confirm the fix from the 0.2.7 silent-failure
audit: cache-save failures must be log.warning-routed locally rather
than tripping ``threading.excepthook`` and polluting the LSP
crash-trace file with "best-effort write failed" entries.

Pre-audit behaviour: ``threading.Thread(target=save_persistent_*)``
let the target's uncaught exceptions bubble to the thread's default
excepthook → ``_install_crash_trace_hook``'s ``_CrashFileHandler`` →
``/tmp/dimfort-lsp.crash`` flagged with what looked like an LSP-side
crash. Operators reading the trace file couldn't tell whether the
LSP had genuinely died or whether the cache directory had merely
gone read-only — exact opposite of the "loud failures" the file is
for.

Post-audit (this branch): ``_cache_save_wrapped(save_fn, label,
cache, root)`` catches the target's exception, ``log.warning``-routes
it with the cache label for context, and returns. Crash-trace file
stays clean; operator sees the cache-write failure in the Output
channel where it belongs.

The test passes a deliberately-failing save function (raises
``PermissionError`` to mimic ``chmod -w``) and asserts the wrapper:
1. Does NOT re-raise.
2. Emits exactly one WARNING-level log record naming the cache
   label so the operator can identify which cache failed.
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("pygls")

from dimfort.lsp.server import _cache_save_wrapped


def test_cache_save_wrapper_swallows_and_warns_on_permission_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Permission-denied cache write → log.warning, no re-raise."""

    def failing_save(_cache: object, _root: object) -> None:
        # Mirrors what happens when chmod -w is applied to the cache
        # directory mid-flight: the underlying open() in
        # save_persistent_projection_cache (or _exports_cache) hits
        # EACCES and surfaces as PermissionError.
        raise PermissionError(13, "Permission denied: '.dimfort-cache/v15'")

    caplog.set_level(logging.WARNING, logger="dimfort.lsp")
    # No assertion that this doesn't raise — pytest fails the test
    # automatically if the call raises.
    _cache_save_wrapped(failing_save, "projection", object(), object())

    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "projection" in r.getMessage()
    ]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING naming the 'projection' label; "
        f"got {len(warnings)} matching records out of {len(caplog.records)}"
    )
    msg = warnings[0].getMessage()
    assert "persistent" in msg
    assert "best-effort" in msg, (
        "warning must include the best-effort context so operators "
        "reading the log know this isn't an LSP-crash class failure"
    )


def test_cache_save_wrapper_passes_through_arguments(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wrapper invokes the save_fn with the cache + root pair as expected."""
    received = []

    def capturing_save(cache: object, root: object) -> None:
        received.append((cache, root))

    cache_sentinel = object()
    root_sentinel = object()
    _cache_save_wrapped(capturing_save, "exports", cache_sentinel, root_sentinel)

    assert received == [(cache_sentinel, root_sentinel)], (
        "wrapper must forward (cache, root) unchanged to the target"
    )
    # No warning fired on the success path.
    assert not [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ], "successful save should not produce any WARNING-level log"


def test_cache_save_wrapper_label_appears_in_warning_for_both_caches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 'label' arg routes through to the warning text for both
    'projection' (M4) and 'exports' (M5) cache save calls — operators
    seeing the WARNING know which subsystem to investigate."""
    caplog.set_level(logging.WARNING, logger="dimfort.lsp")
    for label in ("projection", "exports"):
        caplog.clear()

        def failing(_c: object, _r: object) -> None:
            raise OSError("disk full or permission denied")

        _cache_save_wrapped(failing, label, object(), object())
        msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any(label in m for m in msgs), (
            f"label {label!r} missing from warning text; "
            f"operator wouldn't know which cache failed. Got: {msgs}"
        )
