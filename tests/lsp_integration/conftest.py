"""Shared fixtures for the LSP integration test suite.

Each test gets its own server subprocess (per the design's §8 #2 —
isolation costs ~1s per test but state leakage from a session-scoped
server is harder to debug than the wall-clock cost).

The fixtures here cover the three workspace shapes the lifecycle suite
needs:

  - ``client_simple`` — initialized against ``fixtures/simple/``,
    the standard single-folder workspace. Most tests use this.
  - ``client_multi_folder`` — initialized with TWO workspace folders
    (``folder_a`` then ``folder_b``). The posture-pin test asserts
    the server uses only the first.
  - ``client_uninitialized`` — yields the raw LanguageClient WITHOUT
    sending ``initialize``. Tests that need to inspect the
    pre-handshake state (capability negotiation, before-init
    requests) use this.

Per §8 #1, the design decided ``pytest-anyio`` over ``pytest-asyncio``
to leave the door open for trio. ``pytest-lsp`` 1.0.1 internally wraps
``pytest-asyncio.fixture`` — accepted deviation because either works
(§8 #1 "either works") and matching the underlying tool's choice keeps
fixture wiring straightforward. A future migration to pytest-anyio
would re-evaluate when pytest-lsp upstream supports it.
"""
from __future__ import annotations

import pathlib
import sys

import pytest_lsp
from lsprotocol import types as lsp
from pytest_lsp import ClientServerConfig, LanguageClient, make_test_lsp_client

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"


def _make_test_client() -> LanguageClient:
    """Test client with extra handlers DimFort's server expects on the client side.

    DimFort sends ``workspace/inlayHint/refresh`` to the client after every
    successful check (see LSP-surface inventory at server.py:790, 797).
    pytest-lsp's default ``make_test_lsp_client`` doesn't register a handler
    for it, so the server's request raises ``JsonRpcMethodNotFound`` and the
    error bleeds into unrelated tests. We register a no-op so the request
    succeeds silently — tests that specifically care about the refresh
    semantics will inspect it explicitly.
    """
    client = make_test_lsp_client()

    @client.feature("workspace/inlayHint/refresh")
    def _on_inlay_refresh(_params):
        return None

    return client


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "dimfort", "lsp"],
        client_factory=_make_test_client,
    ),
)
async def client_simple(lsp_client: LanguageClient):
    """A LanguageClient initialized against the simple single-folder workspace.

    The session is set up before yielding and shut down after, so tests
    receive a fully ready client. Yields the underlying LanguageClient
    instance; tests use ``client_simple`` directly to send requests.
    """
    workspace = FIXTURES_DIR / "simple"
    await lsp_client.initialize_session(
        lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            workspace_folders=[
                lsp.WorkspaceFolder(uri=workspace.as_uri(), name="simple"),
            ],
        )
    )
    yield
    await lsp_client.shutdown_session()


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "dimfort", "lsp"],
        client_factory=_make_test_client,
    ),
)
async def client_multi_folder(lsp_client: LanguageClient):
    """A LanguageClient initialized with TWO workspace folders.

    Folder_a's dimfort.toml turns H001 off; folder_b's is empty (defaults).
    The multi-folder posture-pin test asserts the server uses folder_a's
    config (the first) — folder_a's file deliberately contains an H001
    site that would fire under default config.
    """
    folder_a = FIXTURES_DIR / "multi_folder" / "folder_a"
    folder_b = FIXTURES_DIR / "multi_folder" / "folder_b"
    await lsp_client.initialize_session(
        lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            workspace_folders=[
                lsp.WorkspaceFolder(uri=folder_a.as_uri(), name="folder_a"),
                lsp.WorkspaceFolder(uri=folder_b.as_uri(), name="folder_b"),
            ],
        )
    )
    yield
    await lsp_client.shutdown_session()


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "dimfort", "lsp"],
        client_factory=_make_test_client,
    ),
)
async def client_uninitialized(lsp_client: LanguageClient):
    """A LanguageClient with the server subprocess running but NO initialize sent.

    Tests that inspect the pre-handshake state (capability shape on
    initialize response, request-before-initialize safe partial) use
    this. Tests are responsible for sending their own initialize +
    shutdown if they care about graceful teardown.
    """
    yield


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "dimfort", "lsp"],
        client_factory=_make_test_client,
    ),
)
async def client_diagnostics(lsp_client: LanguageClient):
    """A LanguageClient initialized against ``fixtures/diagnostics/``.

    One .f90 file per bug class (bug_classes, keyword_args,
    lhs_subscript, unit_assume, polymorphism, burst). Tests pick the
    file they need; per-file diagnostics are isolated by construction
    (no cross-file references in the fixtures).
    """
    workspace = FIXTURES_DIR / "diagnostics"
    await lsp_client.initialize_session(
        lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            workspace_folders=[
                lsp.WorkspaceFolder(uri=workspace.as_uri(), name="diagnostics"),
            ],
        )
    )
    yield
    await lsp_client.shutdown_session()


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "dimfort", "lsp"],
        client_factory=_make_test_client,
    ),
)
async def client_diagnostics_multifile(lsp_client: LanguageClient):
    """A LanguageClient initialized against ``fixtures/diagnostics_multifile/``.

    Two .f90 files in one workspace. Used by
    ``test_multi_file_publishdiagnostics_ordering``.
    """
    workspace = FIXTURES_DIR / "diagnostics_multifile"
    await lsp_client.initialize_session(
        lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            workspace_folders=[
                lsp.WorkspaceFolder(uri=workspace.as_uri(), name="multifile"),
            ],
        )
    )
    yield
    await lsp_client.shutdown_session()


@pytest_lsp.fixture(
    config=ClientServerConfig(
        server_command=[sys.executable, "-m", "dimfort", "lsp"],
        client_factory=_make_test_client,
    ),
)
async def client_diagnostics_severity(lsp_client: LanguageClient):
    """A LanguageClient initialized against ``fixtures/diagnostics_severity/``.

    The workspace's ``dimfort.toml`` overrides H001 to ``"info"`` — the
    0.2.3 #info-severity-override-silent-reject regression. Test asserts
    the wire severity matches Information, not Error.
    """
    workspace = FIXTURES_DIR / "diagnostics_severity"
    await lsp_client.initialize_session(
        lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            workspace_folders=[
                lsp.WorkspaceFolder(uri=workspace.as_uri(), name="severity"),
            ],
        )
    )
    yield
    await lsp_client.shutdown_session()
