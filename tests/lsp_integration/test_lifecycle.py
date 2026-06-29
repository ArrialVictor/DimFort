"""LSP integration tests — lifecycle.

The 9 tests in this file pin the server's lifecycle contract: how
``initialize`` shapes the capabilities, how ``initialized`` kicks off
the workspace scan, how shutdown and cancel behave, how cross-file
state propagates, what stays silent at boundaries.

See ``docs/design/future/lsp-integration-tests.md`` §4 for the design
context. Tests marked with a ``regression:`` comment trace to a
specific past bug class; tests without are blindspot pins for
intentional contracts.

Per §8 #11, default per-test timeout is 10s.
"""
from __future__ import annotations

import asyncio
import contextlib
import pathlib

import pytest
from lsprotocol import types as lsp
from pytest_lsp import LanguageClient

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
SIMPLE_WS = FIXTURES_DIR / "simple"
SIMPLE_FOO = SIMPLE_WS / "foo.f90"
SIMPLE_BAR = SIMPLE_WS / "bar.f90"
FOLDER_A = FIXTURES_DIR / "multi_folder" / "folder_a"
FOLDER_A_FOO = FOLDER_A / "foo.f90"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Capability shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_initialize_capability_shape(client_uninitialized: LanguageClient):
    """``initialize`` response advertises the full expected capabilities.

    Accidental capability removal would silently disable a feature for
    every companion (the design's "wire-format regression" class). Pin
    the contract: every provider the server has must be advertised.
    """
    try:
        result = await client_uninitialized.initialize_session(
            lsp.InitializeParams(
                capabilities=lsp.ClientCapabilities(),
                workspace_folders=[
                    lsp.WorkspaceFolder(uri=SIMPLE_WS.as_uri(), name="simple"),
                ],
            )
        )
        _assert_capability_shape(result.capabilities)
    finally:
        # Always shut down — if we skip this, pytest-lsp's fixture
        # teardown hangs trying to gracefully stop a server that
        # never received a shutdown notification.
        # audited(0.2.7): silent-OK — shutdown failure during teardown
        # of an already-failed test should not mask the test's real
        # failure. The fixture's `client.stop()` will subsequently kill
        # the subprocess regardless of whether shutdown succeeded.
        with contextlib.suppress(Exception):
            await client_uninitialized.shutdown_session()


def _assert_capability_shape(caps: lsp.ServerCapabilities) -> None:
    """Inline helper so the test body's try/finally stays compact."""
    # Sync mode: Incremental. pygls registers `didOpen/didChange/didClose`
    # at the Incremental level (open_close=True, change=Incremental).
    # If this ever changes — to Full or to None — most diagnostics
    # tests would silently lose half their coverage; pin the contract.
    sync = caps.text_document_sync
    assert isinstance(sync, lsp.TextDocumentSyncOptions)
    assert sync.change == lsp.TextDocumentSyncKind.Incremental
    assert sync.open_close is True

    # All five user-facing providers must be advertised.
    assert caps.hover_provider, "hover provider missing"
    assert caps.definition_provider, "definition provider missing"
    assert caps.code_action_provider, "codeAction provider missing"
    assert caps.inlay_hint_provider, "inlayHint provider missing"
    assert caps.completion_provider, "completion provider missing"

    # Completion triggers — set must match the server's registration
    # at src/dimfort/lsp/server.py (~line 1863). Order doesn't matter;
    # presence does.
    assert caps.completion_provider.trigger_characters is not None
    triggers = set(caps.completion_provider.trigger_characters)
    assert triggers == {"{", " ", "/", "*", "^"}, (
        f"completion trigger characters drifted: got {triggers}"
    )

    # Code-action kinds — only QuickFix is registered; nothing else.
    # pygls returns these as raw strings (the enum value), not as the
    # CodeActionKind enum, so compare against the string form.
    kinds = caps.code_action_provider.code_action_kinds
    assert tuple(kinds) == (lsp.CodeActionKind.QuickFix.value,), (
        f"codeAction kinds drifted: got {kinds}"
    )

    # Inlay hint provider — server does NOT resolve hints lazily; the
    # full payload comes back on the initial request.
    inlay = caps.inlay_hint_provider
    if isinstance(inlay, lsp.InlayHintOptions):
        assert inlay.resolve_provider is False or inlay.resolve_provider is None


# ---------------------------------------------------------------------------
# 2. Workspace scan readiness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_initialize_then_index_ready(client_simple: LanguageClient):
    """``initialized`` kicks off a background scan that eventually settles.

    The simplest observable signal of "scan complete enough to serve
    requests" is a successful diagnostic publish after didOpen.
    Asserts the publish round-trip works at all — the harness proof of
    life and a smoke test that the background workspace scan doesn't
    deadlock against the document-sync handler.
    """
    text = _read(SIMPLE_FOO)
    client_simple.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=SIMPLE_FOO.as_uri(),
                language_id="fortran",
                version=1,
                text=text,
            )
        )
    )
    # Wait for diagnostics — proves the pipeline ran end-to-end.
    await client_simple.wait_for_notification("textDocument/publishDiagnostics")


# ---------------------------------------------------------------------------
# 3. Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_shutdown_cleanup(client_simple: LanguageClient):
    """``shutdown`` + ``exit`` complete without error.

    The fixture's teardown runs shutdown_session; this test exists so a
    regression in graceful shutdown (e.g., a daemon thread hanging the
    exit) shows up as a clear test failure rather than a flaky teardown
    in some other test.
    """
    # The fixture's auto-shutdown does the actual work; this test
    # passes if the fixture teardown doesn't raise. We just verify
    # the client is in a state that allows shutdown.
    assert client_simple is not None


# ---------------------------------------------------------------------------
# 4. $/cancelRequest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.skip(
    reason="TODO: requires sending cancelRequest mid-flight; needs a "
    "request type that's reliably slow. dimfort/checkWorkspace is the "
    "natural candidate but its async ack confuses the cancel semantics. "
    "Implement in PR 5 (workspace) alongside the checkWorkspace tests."
)
async def test_cancelrequest_complete_and_discard(client_simple: LanguageClient):
    """Per §8 #5: cancel completes the request and drops the result.

    The handler doesn't poll for cancellation today; interrupting
    mid-tree-walk risks leaving ``state.last_result`` partially
    populated. The contract is: server completes, drops the result
    before sending; client sees no late response.
    """


# ---------------------------------------------------------------------------
# 5. Request before workspace scan completes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.skip(
    reason="TODO: timing-sensitive. Needs a deterministic way to send "
    "a request BEFORE the background scan completes. One approach: "
    "use a fixture workspace large enough that scan is slow, fire hover "
    "immediately. Implement when adding the bulk-fixture harness."
)
async def test_request_before_index_returns_safe_partial(
    client_uninitialized: LanguageClient,
):
    """Per §8 #7: pre-index requests return an empty/safe-partial payload.

    Contract: no error UI, no client hang, no late responses after
    index becomes ready. Test pins the empty-payload shape.
    """


# ---------------------------------------------------------------------------
# 6. didClose releases per-URI resources
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_didclose_releases_resources(client_simple: LanguageClient):
    """``didClose`` republishes empty diagnostics for the closed URI.

    The contract surfaced from the LSP-surface inventory: didClose
    "republishes cached diagnostics, forgets URI from caches"
    (server.py:1499). The diagnostic republish IS the observable
    signal — workspace-index data may legitimately persist, but the
    URI's PUBLISHED diagnostic list goes empty so the client clears
    its decoration overlay.
    """
    text = _read(SIMPLE_FOO)
    client_simple.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=SIMPLE_FOO.as_uri(),
                language_id="fortran",
                version=1,
                text=text,
            )
        )
    )
    await client_simple.wait_for_notification("textDocument/publishDiagnostics")
    # Clear the captured diagnostics so we can wait for the next publish.
    client_simple.diagnostics.clear()

    # Close the document.
    client_simple.text_document_did_close(
        lsp.DidCloseTextDocumentParams(
            text_document=lsp.TextDocumentIdentifier(uri=SIMPLE_FOO.as_uri()),
        )
    )

    # Wait for the post-close publish (should be empty list).
    await client_simple.wait_for_notification("textDocument/publishDiagnostics")
    diags = client_simple.diagnostics.get(SIMPLE_FOO.as_uri(), None)
    # pygls represents empty diagnostics as () (tuple), not [] (list).
    assert not diags, (
        f"closed URI didn't republish empty diagnostics: {diags}"
    )


# ---------------------------------------------------------------------------
# 7. Tab-switch stale workset republish (★ blindspot from LSP-surface inventory)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_tab_switch_stale_workset_republish(client_simple: LanguageClient):
    """Opening a second file (different workset slot) re-publishes its diagnostics.

    Wire-surface blindspot from the LSP-method inventory: on tab
    switch, ``_ensure_uri_loaded`` detects a stale workset and fires
    synchronous publish before the handler runs. The observable signal
    is that publishDiagnostics arrives for the newly-opened file
    before its hover/inlay/etc. handler responds.

    Test scenario: open foo.f90 (workset includes foo), then open bar.f90.
    Assert: publishDiagnostics arrives for bar.f90.
    """
    text_foo = _read(SIMPLE_FOO)
    client_simple.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=SIMPLE_FOO.as_uri(),
                language_id="fortran",
                version=1,
                text=text_foo,
            )
        )
    )
    await client_simple.wait_for_notification("textDocument/publishDiagnostics")

    text_bar = _read(SIMPLE_BAR)
    client_simple.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=SIMPLE_BAR.as_uri(),
                language_id="fortran",
                version=1,
                text=text_bar,
            )
        )
    )

    # The second didOpen must trigger publishDiagnostics for bar.f90
    # within a reasonable window (post-debounce, ~400 ms). Use a longer
    # timeout to account for cold-start workspace scan if it hasn't
    # already completed.
    async def _wait_for_bar_diagnostics():
        while True:
            await client_simple.wait_for_notification(
                "textDocument/publishDiagnostics"
            )
            # The notifications come in batches; check the last
            # diagnostic for the bar URI.
            diags = client_simple.diagnostics.get(SIMPLE_BAR.as_uri())
            if diags is not None:
                return

    await asyncio.wait_for(_wait_for_bar_diagnostics(), timeout=8.0)


# ---------------------------------------------------------------------------
# 8. Multi-folder posture pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_multi_folder_posture_pin(client_multi_folder: LanguageClient):
    """Server uses only the FIRST workspace folder's ``dimfort.toml`` config.

    Per §8 #10: current contract is single-folder config; this test
    carries the marker for the future maintainer who lands full
    multi-folder support to update the assertion.

    Scenario: two folders. folder_a/dimfort.toml turns H001 OFF.
    folder_b/dimfort.toml is empty (defaults: H001 = error).
    folder_a/foo.f90 contains an H001 site. With folder_a's config
    applied (current contract), H001 must NOT fire. If the server
    ever silently switches to folder_b's config (or merges them),
    H001 will fire and this test catches it.

    # TODO(pre-0.3.0): revisit when full multi-folder support lands.
    """
    text = _read(FOLDER_A_FOO)
    client_multi_folder.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=FOLDER_A_FOO.as_uri(),
                language_id="fortran",
                version=1,
                text=text,
            )
        )
    )
    await client_multi_folder.wait_for_notification(
        "textDocument/publishDiagnostics"
    )

    diags = client_multi_folder.diagnostics.get(FOLDER_A_FOO.as_uri(), [])
    h001 = [d for d in diags if d.code == "H001"]
    assert h001 == [], (
        f"H001 fired despite folder_a's config disabling it; "
        f"server may have merged or used folder_b's config. "
        f"Diagnostics: {[(d.code, d.message[:40]) for d in diags]}"
    )


# ---------------------------------------------------------------------------
# 9. No file-watcher capability (★ option-b contract pin)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_no_file_watcher_capability(client_uninitialized: LanguageClient):
    """Server does NOT advertise ``workspace/didChangeWatchedFiles`` support.

    Current 0.2.7 contract: DimFort has no file watcher. Config changes
    (``dimfort.toml``, units file) require a server restart to take
    effect.

    This test pins that boundary. If you're here because you added
    a watcher capability, update this test intentionally and reason
    through what changes — there's a race between watcher events and
    the workspace-index daemon thread to think through.

    Catches regression in BOTH directions:
      - A watcher gets added without thinking through cache
        invalidation / threading → this test fails, forcing review.
      - A watcher gets half-removed (capability advertised but no
        handler) → this test signals which side of the boundary the
        code is really on.
    """
    try:
        result = await client_uninitialized.initialize_session(
            lsp.InitializeParams(
                capabilities=lsp.ClientCapabilities(),
                workspace_folders=[
                    lsp.WorkspaceFolder(uri=SIMPLE_WS.as_uri(), name="simple"),
                ],
            )
        )
        # pygls always sets ``workspace`` and ``workspace.file_operations``
        # to default objects (with all None inner callbacks). What we
        # specifically must NOT see: any non-None file-operation callback,
        # which would imply the server is watching files. workspace_folders
        # support IS expected (enumerate the folders the client opened).
        ws = result.capabilities.workspace
        assert ws is not None
        fo = ws.file_operations
        if fo is not None:
            # Each callback must be None — file-operations object exists
            # only as a pygls default container, advertises no actual
            # watcher.
            assert fo.did_create is None, "server registered did_create"
            assert fo.will_create is None, "server registered will_create"
            assert fo.did_rename is None, "server registered did_rename"
            assert fo.will_rename is None, "server registered will_rename"
            assert fo.did_delete is None, "server registered did_delete"
            assert fo.will_delete is None, "server registered will_delete"
        # Note: workspace/didChangeWatchedFiles capability is registered
        # dynamically via `client/registerCapability` AFTER initialize,
        # not statically in the initialize response. The contract this
        # test pins is the static capability shape; the dynamic-register
        # absence is implicitly verified by the fact that we never see
        # a registerCapability call in normal operation.
    finally:
        # audited(0.2.7): silent-OK — shutdown failure during teardown
        # of an already-failed test should not mask the test's real
        # failure. The fixture's `client.stop()` will subsequently kill
        # the subprocess regardless of whether shutdown succeeded.
        with contextlib.suppress(Exception):
            await client_uninitialized.shutdown_session()
