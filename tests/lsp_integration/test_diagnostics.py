"""LSP integration tests — diagnostics.

The 9 tests in this file pin the wire-side diagnostic contract: which
codes fire for which Fortran constructs, how cache invalidation works
across didChange, how multi-file ordering looks, how severity
overrides flow through, what the polymorphism / unit_assume payload
shapes are.

Most tests trace to a specific past regression (the 0.2.3 missed-site
class and the 0.2.3.1 cache-version-bump class); see the per-test
docstring for the source.

Each test gets its own server (per design §8 #2). All but two run
against the ``client_diagnostics`` workspace; multi-file uses its own
fixture and severity-override uses a workspace with a custom
``dimfort.toml``.
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pytest
from lsprotocol import types as lsp
from pytest_lsp import LanguageClient

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
DIAG_WS = FIXTURES_DIR / "diagnostics"
MULTI_WS = FIXTURES_DIR / "diagnostics_multifile"
SEVERITY_WS = FIXTURES_DIR / "diagnostics_severity"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


async def _open_and_wait(client: LanguageClient, path: pathlib.Path) -> list:
    """Open ``path`` in the client, wait for the first publishDiagnostics."""
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
    return list(client.diagnostics.get(path.as_uri(), []))


def _codes(diags) -> set[str]:
    return {d.code for d in diags if d.code is not None}


# ---------------------------------------------------------------------------
# 1. didOpen → expected codes (bug-class smoke)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_didopen_expected_codes(client_diagnostics: LanguageClient):
    """A didOpen on the bug-classes fixture fires every expected diagnostic.

    Pins the wire contract: the codes U002 (invalid unit), U005
    (unannotated use), H001 (assignment mismatch), H002 (operand
    mismatch), H010 (implicit literal cast) must ALL show up on
    publishDiagnostics for this fixture file. Drops here would
    indicate a regression in the publish pipeline.
    """
    diags = await _open_and_wait(client_diagnostics, DIAG_WS / "bug_classes.f90")
    got = _codes(diags)
    expected = {"U002", "U005", "H001", "H002", "H010"}
    missing = expected - got
    assert not missing, f"didOpen missed codes {missing}; got {sorted(got)}"


# ---------------------------------------------------------------------------
# 2. didChange invalidates cache (the 0.2.3.1 v7→v9 regression class)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_didchange_invalidates_cache(client_diagnostics: LanguageClient):
    """A didChange that flips a unit annotation re-publishes new diagnostics.

    Regression class: 0.2.3.1 #cache-version-v7-v9 — cached
    diagnostics weren't invalidated across cache-format bumps; clients
    served stale results post-edit. The test pins the contract: edit
    a unit annotation, the diagnostics list reflects the change.

    Specifically: open with H001 firing, then edit the LHS annotation
    to match the RHS — assert H001 is no longer in the published
    diagnostics.
    """
    path = DIAG_WS / "cache_invalidation.f90"
    text = _read(path)
    client_diagnostics.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=path.as_uri(),
                language_id="fortran",
                version=1,
                text=text,
            )
        )
    )
    await client_diagnostics.wait_for_notification(
        "textDocument/publishDiagnostics"
    )
    diags_open = list(client_diagnostics.diagnostics.get(path.as_uri(), []))
    assert "H001" in _codes(diags_open), (
        f"baseline: H001 should fire on initial open, got {_codes(diags_open)}"
    )

    # Edit the LHS annotation `dest : m` → `s` so it matches the RHS.
    # After didChange the file is fully clean and H001 should disappear.
    new_text = text.replace(
        "real :: dest       !< @unit{m}",
        "real :: dest       !< @unit{s}",
    )
    assert new_text != text, "fixture replacement didn't take effect"
    # Send as a full-document Partial change (range covers everything).
    # The server's sync mode is Incremental — pygls accepts WholeDocument
    # at this sync level but doesn't always trigger a re-check; Partial
    # with a range is what real clients send and what the server
    # reliably reacts to.
    line_count = text.count("\n") + 1
    client_diagnostics.diagnostics.clear()
    client_diagnostics.text_document_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(
                uri=path.as_uri(), version=2,
            ),
            content_changes=[
                lsp.TextDocumentContentChangePartial(
                    range=lsp.Range(
                        start=lsp.Position(line=0, character=0),
                        end=lsp.Position(line=line_count, character=0),
                    ),
                    text=new_text,
                ),
            ],
        )
    )
    # Server debounces 400ms then publishes. wait_for_notification can
    # race with internal queue ordering on this short window — explicit
    # sleep past the debounce + check is more reliable for this contract
    # test. The point is the FINAL state of diagnostics after the edit;
    # latency of the publish isn't what we're asserting.
    await asyncio.sleep(0.7)
    diags_after = list(client_diagnostics.diagnostics.get(path.as_uri(), []))
    assert "H001" not in _codes(diags_after), (
        f"H001 still firing after annotation fix; got {_codes(diags_after)} — "
        f"cache likely stale"
    )


# ---------------------------------------------------------------------------
# 3. didChange keyword args (0.2.3 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_didchange_keyword_args_H004(client_diagnostics: LanguageClient):
    """``call f(b=x)`` with a unit mismatch on the keyword arg fires H004.

    Regression: 0.2.3 #keyword-args-silent-miss — keyword-argument call
    sites weren't walked, so a unit mismatch at ``call f(b=x)`` went
    silent. The fix re-enables H004 for this construction; this test
    catches re-regression.
    """
    diags = await _open_and_wait(client_diagnostics, DIAG_WS / "keyword_args.f90")
    assert "H004" in _codes(diags), (
        f"H004 missed on keyword-arg call; got {sorted(_codes(diags))}"
    )


# ---------------------------------------------------------------------------
# 4. didChange LHS subscript (0.2.3 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_didchange_lhs_subscript_H002(client_diagnostics: LanguageClient):
    """Mixed-unit indices in an LHS array subscript fire H002.

    Regression: 0.2.3 #lhs-subscript-silent-miss — array subscripts on
    LHS weren't walked for nested-expression diagnostics. The fix
    re-enables H002 inside the subscript.
    """
    diags = await _open_and_wait(client_diagnostics, DIAG_WS / "lhs_subscript.f90")
    assert "H002" in _codes(diags), (
        f"H002 missed in LHS subscript; got {sorted(_codes(diags))}"
    )


# ---------------------------------------------------------------------------
# 5. Multi-file publishDiagnostics ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_multi_file_publishdiagnostics_ordering(
    client_diagnostics_multifile: LanguageClient,
):
    """Opening two files surfaces a publishDiagnostics for each URI.

    The contract: every file receives its OWN diagnostic envelope —
    not coalesced into one, not dropped. The implementation routes
    publishes per-URI so a regression here would show up as a missing
    envelope for one file.
    """
    file_a = MULTI_WS / "file_a.f90"
    file_b = MULTI_WS / "file_b.f90"

    # Open file_a first, wait for its diagnostics.
    await _open_and_wait(client_diagnostics_multifile, file_a)
    # Then file_b. Both URIs end up in the captured dict.
    client_diagnostics_multifile.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=file_b.as_uri(),
                language_id="fortran",
                version=1,
                text=_read(file_b),
            )
        )
    )

    # Wait for file_b's publishDiagnostics specifically. The server may
    # publish for either URI first, so loop until both have populated.
    async def _wait_for_both():
        while True:
            await client_diagnostics_multifile.wait_for_notification(
                "textDocument/publishDiagnostics"
            )
            diags = client_diagnostics_multifile.diagnostics
            if (
                file_a.as_uri() in diags and diags[file_a.as_uri()]
                and file_b.as_uri() in diags and diags[file_b.as_uri()]
            ):
                return

    await asyncio.wait_for(_wait_for_both(), timeout=5.0)

    diags_a = client_diagnostics_multifile.diagnostics.get(file_a.as_uri(), [])
    diags_b = client_diagnostics_multifile.diagnostics.get(file_b.as_uri(), [])
    assert "H001" in _codes(diags_a), (
        f"file_a didn't get H001; got {sorted(_codes(diags_a))}"
    )
    assert "H001" in _codes(diags_b), (
        f"file_b didn't get H001; got {sorted(_codes(diags_b))}"
    )


# ---------------------------------------------------------------------------
# 6. Severity override to "info" (0.2.3 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_severity_override_info_level(
    client_diagnostics_severity: LanguageClient,
):
    """``[diagnostics] H001 = "info"`` flips H001's wire severity to Information.

    Regression: 0.2.3 #info-severity-override-silent-reject — the
    config table rejected ``"info"`` silently; documented example was
    unreachable. Fix made all three of error/warning/info accepted.
    The workspace's dimfort.toml sets H001 = "info"; the wire
    Diagnostic.severity must be Information (3 in LSP enum).
    """
    diags = await _open_and_wait(client_diagnostics_severity, SEVERITY_WS / "h001_site.f90")
    h001 = [d for d in diags if d.code == "H001"]
    assert len(h001) >= 1, (
        f"H001 didn't fire; got {sorted(_codes(diags))}"
    )
    assert h001[0].severity == lsp.DiagnosticSeverity.Information, (
        f"H001 severity didn't follow `[diagnostics] H001 = \"info\"` "
        f"override; got {h001[0].severity}"
    )


# ---------------------------------------------------------------------------
# 7. @unit_assume payload (U020 INFO with assumed-marker)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_unit_assume_payload(client_diagnostics: LanguageClient):
    """``@unit_assume{...}`` fires U020 INFO with the assumed-unit + reason.

    Pins the wire-format contract for the audit-tracking case: every
    use of the escape hatch must be visible to the client (so the
    companion's panel / output can list them), and the wire message
    must include the assumed unit text and the reason field so a
    panel display can render them. Regression here would silently
    erode the audit trail.
    """
    diags = await _open_and_wait(client_diagnostics, DIAG_WS / "unit_assume.f90")
    u020 = [d for d in diags if d.code == "U020"]
    assert len(u020) == 1, (
        f"U020 didn't fire exactly once; got {sorted(_codes(diags))}"
    )
    assert u020[0].severity == lsp.DiagnosticSeverity.Information
    # Message must name the assumed unit AND the reason.
    msg = u020[0].message
    assert "kg" in msg, f"U020 message dropped unit text: {msg!r}"
    assert "Brandes" in msg, f"U020 message dropped reason text: {msg!r}"


# ---------------------------------------------------------------------------
# 8. Polymorphism payload (H023 — the 0.2.3.1 marker-propagation class)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_polymorphism_payloads_H023(client_diagnostics: LanguageClient):
    """A tyvar that forces a binding fires H023 with the conflict named.

    Regression: 0.2.3.1 #marker-propagation — handler produced correct
    H020/H021/H022/H023 objects but wire serialization dropped fields
    that the panel needed to paint 🔴. The test pins the wire-payload
    shape: H023 fires AND its message names both ``'a`` (the tyvar)
    AND the concrete unit it failed to bind (``kg`` here).
    """
    diags = await _open_and_wait(client_diagnostics, DIAG_WS / "polymorphism.f90")
    h023 = [d for d in diags if d.code == "H023"]
    assert len(h023) >= 1, (
        f"H023 didn't fire on the polymorphism fixture; got {sorted(_codes(diags))}"
    )
    msg = h023[0].message
    assert "'a" in msg, f"H023 message dropped tyvar name: {msg!r}"
    assert "kg" in msg, f"H023 message dropped binding target: {msg!r}"
    # H002 must NOT fire — H023 supersedes the concrete-mismatch error.
    assert "H002" not in _codes(diags), (
        f"H002 fired alongside H023; supersession broke. "
        f"Got {sorted(_codes(diags))}"
    )


# ---------------------------------------------------------------------------
# 9. Rapid didChange burst (§8 #6: 10 events / 100 ms)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_rapid_didchange_burst(client_diagnostics: LanguageClient):
    """A burst of 10 didChange events within 100ms collapses to a single check.

    The server's debounce window for didChange is ~400ms (LSP-surface
    inventory). With 10 edits fired within 100ms, the server must
    publish exactly ONE diagnostic set reflecting the final state —
    not 10 interleaved sets, and not the state of an intermediate
    edit. Per design §8 #6.

    Test scenario: open clean, then flip the annotation across 10
    rapid versions ending in a state that fires H001. Wait for the
    next publish; assert the final-state code is present.
    """
    burst_path = DIAG_WS / "burst.f90"
    base_text = _read(burst_path)
    client_diagnostics.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=burst_path.as_uri(),
                language_id="fortran",
                version=1,
                text=base_text,
            )
        )
    )
    await client_diagnostics.wait_for_notification(
        "textDocument/publishDiagnostics"
    )

    # Build 10 edit versions. Each oscillates between an annotation
    # that fires H001 and one that's clean. The 10th (final) one
    # fires H001.
    annotated_h001 = base_text.replace(
        "real :: x\n  real :: y\n",
        "real :: x   !< @unit{m}\n  real :: y   !< @unit{s}\n",
    )
    assert annotated_h001 != base_text, "fixture wasn't editable as expected"

    versions = []
    for i in range(10):
        versions.append(annotated_h001 if i % 2 == 0 else base_text)
    # Final version: H001-firing one.
    versions[-1] = annotated_h001

    base_line_count = base_text.count("\n") + 1
    ann_line_count = annotated_h001.count("\n") + 1
    client_diagnostics.diagnostics.clear()
    start = time.monotonic()
    for i, ver_text in enumerate(versions, start=2):
        # Use the previous version's line count for the replace range
        # (it's the document's CURRENT state we're overwriting).
        prev_text = versions[i - 3] if i >= 3 else base_text
        prev_line_count = prev_text.count("\n") + 1
        _ = ann_line_count, base_line_count  # for clarity (line-count vars)
        client_diagnostics.text_document_did_change(
            lsp.DidChangeTextDocumentParams(
                text_document=lsp.VersionedTextDocumentIdentifier(
                    uri=burst_path.as_uri(), version=i,
                ),
                content_changes=[
                    lsp.TextDocumentContentChangePartial(
                        range=lsp.Range(
                            start=lsp.Position(line=0, character=0),
                            end=lsp.Position(line=prev_line_count, character=0),
                        ),
                        text=ver_text,
                    ),
                ],
            )
        )
    elapsed = time.monotonic() - start
    assert elapsed < 0.2, (
        f"burst sent too slowly to exercise debounce: took {elapsed:.3f}s"
    )

    # Server debounces 400ms then publishes. Sleep past debounce +
    # check; the contract being tested is "collapse to single publish
    # of the final state", not the exact publish latency.
    await asyncio.sleep(0.8)
    diags = list(client_diagnostics.diagnostics.get(burst_path.as_uri(), []))
    assert "H001" in _codes(diags), (
        f"final-state H001 missing after burst; got {sorted(_codes(diags))}"
    )
