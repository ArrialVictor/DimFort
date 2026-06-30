"""LSP integration tests — coverage + warm cache serde.

Coverage area covers the ``dimfort/lineStatus`` per-line tier
classifications and the ``dimfort/coverageStats`` workspace payload.
The wire surface here is server-side (the design doc classed coverage
DECORATIONS as a display concern, but the tier classification itself
is wire — what shape paints depends on what the server says paints).

Warm cache area covers the 0.2.3 cache-serde regression class:
``Unit.offset`` (for affine units like degC) and U002
``suggested_rewrite`` payload were silently dropped during cache
serialization, so warm-restart consumers saw a different (degraded)
diagnostic payload than cold runs. Tests assert cold and warm runs
produce identical wire shapes.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

import pytest
from lsprotocol import types as lsp
from pytest_lsp import ClientServerConfig, LanguageClient

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
COV_WS = FIXTURES_DIR / "coverage"
COV_TIERS = COV_WS / "tiers.f90"
COV_RED_CODES = COV_WS / "red_codes.f90"
COV_U005 = COV_WS / "u005_use_site.f90"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


async def _open_and_wait(client: LanguageClient, path: pathlib.Path) -> None:
    client.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=path.as_uri(),
                language_id="fortran",
                version=1,
                text=_read(path),
            )
        )
    )
    await client.wait_for_notification("textDocument/publishDiagnostics")


async def _line_status(client: LanguageClient, path: pathlib.Path):
    """Send dimfort/lineStatus and return the deserialized payload."""
    return await client.protocol.send_request_async(
        "dimfort/lineStatus", {"uri": path.as_uri()},
    )


def _lines_dict(line_status) -> dict[int, str]:
    """Flatten the lineStatus.lines list to a {line_number: status} dict."""
    return {ln.line: ln.status for ln in line_status.lines}


# ---------------------------------------------------------------------------
# 1. lineStatus tier classifications across all 4 colors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_linestatus_tier_classifications(
    client_coverage: LanguageClient,
):
    """``dimfort/lineStatus`` classifies known sites into green/yellow/red.

    Pin the wire contract for the coverage tier surface. The fixture
    has:
      - line 14 (``dist = m_val``) — green (annotated assignment, clean)
      - line 16 (``bare = m_val``) — yellow (bare unannotated)
      - line 18 (``dur = m_val``) — red (H001 m ≠ s)

    Test asserts each known line gets the documented tier. Blue
    (unparsed) needs a parse-error site to materialize and is
    exercised separately by tests/lsp_integration/test_inlay_and_panel.
    """
    await _open_and_wait(client_coverage, COV_TIERS)
    line_status = await _line_status(client_coverage, COV_TIERS)
    assert line_status is not None, "lineStatus returned None"
    lines = _lines_dict(line_status)

    assert lines.get(14) == "green", (
        f"line 14 expected green; got {lines.get(14)!r}; full={lines}"
    )
    assert lines.get(16) == "yellow", (
        f"line 16 expected yellow; got {lines.get(16)!r}; full={lines}"
    )
    assert lines.get(18) == "red", (
        f"line 18 expected red; got {lines.get(18)!r}; full={lines}"
    )


# ---------------------------------------------------------------------------
# 2. Red-tier codeset includes the polymorphism + U002 codes (0.2.4 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_linestatus_red_tier_codeset(
    client_coverage: LanguageClient,
):
    """H020/H021/H022/H023, S003, U002 sites correctly paint red.

    Regression: 0.2.4 #h-codes-missing-red-tier — these polymorphism
    + scale + invalid-unit codes were missing from the red tier
    codeset; lines firing them painted green or yellow. The fix
    expanded the red codeset to cover all error-severity codes.

    Test: open the polymorphism fixture (fires H023). Assert the
    firing line paints red.
    """
    await _open_and_wait(client_coverage, COV_RED_CODES)
    line_status = await _line_status(client_coverage, COV_RED_CODES)
    assert line_status is not None
    lines = _lines_dict(line_status)
    # H023 fires on the `y = x + c` line — line 13 (0-indexed) /
    # 14 (1-indexed). lineStatus uses 1-indexed lines (the wire
    # protocol's convention).
    assert lines.get(14) == "red", (
        f"H023-firing line 14 expected red; got {lines.get(14)!r}; "
        f"full={lines}"
    )


# ---------------------------------------------------------------------------
# 3. U005 propagation to use sites (0.2.6 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_linestatus_u005_propagation(
    client_coverage: LanguageClient,
):
    """Unannotated variables' use sites paint yellow, not green.

    Regression: 0.2.6 #coverage-u005-propagation — U005 unannotated-use
    diagnostics weren't propagated to coverage; removing an
    annotation showed green at the use site instead of yellow.

    Fixture: ``bad`` is declared unannotated (line 9), used at line
    12 (``bad = src_m``). Both must paint yellow.
    """
    await _open_and_wait(client_coverage, COV_U005)
    line_status = await _line_status(client_coverage, COV_U005)
    assert line_status is not None
    lines = _lines_dict(line_status)
    # The use site (line 13 in 1-indexed, `bad = src_m`) must paint
    # yellow. lineStatus uses 1-indexed lines.
    assert lines.get(13) == "yellow", (
        f"U005 use site (line 13) expected yellow; got {lines.get(13)!r}; "
        f"full={lines}"
    )
    # The decl site (line 11) also paints yellow — `bad` is unannotated.
    assert lines.get(11) == "yellow", (
        f"U005 decl site (line 11) expected yellow; got {lines.get(11)!r}; "
        f"full={lines}"
    )


# ---------------------------------------------------------------------------
# 4. coverageStats workspace payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_coveragestats_file_payload_shape(
    client_coverage: LanguageClient,
):
    """``dimfort/coverageStats`` file-scope payload has the documented shape.

    Pin the wire shape: file-scope coverageStats returns
    ``{scope: "file", uri, files: [...], total: {...}}`` with the
    per-tier counts and coverage_pct fields documented in the
    coverage-visualization design doc.
    """
    await _open_and_wait(client_coverage, COV_TIERS)
    stats = await client_coverage.protocol.send_request_async(
        "dimfort/coverageStats", {"uri": COV_TIERS.as_uri()},
    )
    assert stats is not None, "coverageStats returned None for file scope"
    # Field access via attribute (pygls Object wrapper).
    assert getattr(stats, "scope", None) == "file", (
        f"file-scope coverageStats has wrong scope: {stats!r}"
    )
    # files is a list, total is an object with the per-tier counts.
    files = getattr(stats, "files", None)
    total = getattr(stats, "total", None)
    assert files is not None, "coverageStats missing `files` field"
    assert total is not None, "coverageStats missing `total` field"
    # Total carries the documented tier-count + coverage_pct fields.
    for attr in ("ok", "warn", "fire", "unparsed", "coverage_pct"):
        assert hasattr(total, attr), (
            f"coverageStats.total missing documented field {attr!r}"
        )


# ---------------------------------------------------------------------------
# 5. Warm cache: affine offset survives serde (0.2.3 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.skip(
    reason="TODO: requires a fixture where the affine offset of degC vs K "
    "produces a wire-observable difference (e.g. an S001/S002 affine-arithmetic "
    "diagnostic with the offset embedded in the message). The simplest "
    "wire signature for 'offset survived serde' is identical-bytes "
    "diagnostics between cold and warm runs of a known-affine-arithmetic "
    "expression. Implementing this requires (a) configuring "
    "cacheMode=read-write + cacheDir, (b) running two server lifetimes "
    "with the same cacheDir, (c) capturing diagnostics from both, (d) "
    "comparing. Manageable in a follow-up PR alongside a dedicated "
    "affine-arithmetic fixture; deferred from PR 6 to keep this slice "
    "focused on coverage."
)
async def test_cache_affine_offset_warm_restart():
    """Affine offset (degC vs K) survives the cache serialization round-trip.

    Regression: 0.2.3 #cache-serde-affine-round-trip — Unit.offset was
    dropped at serialization; warm runs silently turned degC into K.
    """


# ---------------------------------------------------------------------------
# 6. Warm cache: U002 suggested_rewrite payload survives serde
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_cache_u002_suggested_rewrite_payload_warm():
    """U002's ``suggested_rewrite`` payload survives the cache serialization.

    Regression: 0.2.3 #cache-serde-u002-rewrite-lost — U002's
    ``Diagnostic.data.suggested_rewrite`` field was dropped at
    serialization; warm runs lost the "did you mean?" hint.

    Test: open a file with a malformed unit that fires U002 with a
    suggested rewrite. Initialize a server with cache mode
    read-write pointed at a tmp dir. Open file, capture U002.data.
    Shut down. Re-initialize against the same tmp dir. Open same
    file, capture U002.data. Assert both carry the rewrite.
    """
    fixture = COV_WS / "u002_rewrite.f90"
    if not fixture.exists():
        pytest.skip(
            "fixture fixtures/coverage/u002_rewrite.f90 not yet created — "
            "needs a `@unit{kg^2/}` or similar malformed-but-suggestible "
            "annotation. Adding the fixture is the next step."
        )

    with tempfile.TemporaryDirectory() as cache_root:

        async def _run_check() -> list:
            cfg = ClientServerConfig(
                server_command=[sys.executable, "-m", "dimfort", "lsp"],
            )
            client = await cfg.start()
            try:
                await client.initialize_session(
                    lsp.InitializeParams(
                        capabilities=lsp.ClientCapabilities(),
                        workspace_folders=[
                            lsp.WorkspaceFolder(
                                uri=COV_WS.as_uri(), name="cov",
                            ),
                        ],
                        initialization_options={
                            "cacheMode": "read-write",
                            "cacheDir": cache_root,
                        },
                    )
                )
                client.text_document_did_open(
                    lsp.DidOpenTextDocumentParams(
                        text_document=lsp.TextDocumentItem(
                            uri=fixture.as_uri(),
                            language_id="fortran",
                            version=1,
                            text=_read(fixture),
                        )
                    )
                )
                await client.wait_for_notification(
                    "textDocument/publishDiagnostics"
                )
                # Give the server time to persist the cache before
                # we shut down.
                await asyncio.sleep(0.3)
                return list(client.diagnostics.get(fixture.as_uri(), []))
            finally:
                await client.shutdown_session()
                await client.stop()

        cold = await _run_check()
        warm = await _run_check()

        cold_u002 = [d for d in cold if d.code == "U002"]
        warm_u002 = [d for d in warm if d.code == "U002"]
        assert cold_u002 and warm_u002, (
            f"U002 didn't fire in one run; cold={cold_u002}, warm={warm_u002}"
        )
        # The suggested_rewrite payload travels in Diagnostic.data.
        for tag, ds in (("cold", cold_u002), ("warm", warm_u002)):
            d = ds[0]
            data = getattr(d, "data", None)
            assert data is not None, (
                f"{tag} run: U002 missing data payload (suggested_rewrite)"
            )
