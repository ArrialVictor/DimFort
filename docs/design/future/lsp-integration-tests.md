# Server-side LSP integration tests

**Status:** **Planned for 0.2.7** alongside Track D Ring 2. Drafted
after a 2026-06-18 conversation that established the release-cycle
QA bottleneck has a content-vs-display split — wire content can be
asserted in tests; display correctness still needs a human walking
each companion. This note scopes the wire-side automation.

The companion design notes that this decision touches:
[per-variable-continuation-attach.md](per-variable-continuation-attach.md)
(no direct link, but every release-blocking regression in 0.2.3.1
was on this surface) and the `feedback_in_editor_smoke_before_publish`
project rule (which this work is designed to *narrow*, not replace).

## 1. The gap this fills

DimFort already has 10 `test_lsp_*.py` files under `tests/unit/`.
Their pattern: import the handler function from `dimfort.lsp.server`,
populate `state.last_result` by hand, call the handler directly,
assert on its Python return value. They bypass pygls's actual
JSON-RPC dispatch — `importorskip("pygls")` is for the type
imports, not the wire layer.

This catches handler-logic bugs. It does **not** catch bugs in the
layer between the handler and the wire — which is where every
0.2.3.1 release-cycle regression actually lived:

- Marker propagation — handler produced correct objects; the wire
  serialization dropped fields.
- Multi-line message reformat — in-process object was fine; the
  rendered JSON broke the editor's line splitter.
- Cache invalidation v7→v9 — fires on `didChange` notifications +
  ordering of `dimfort/coverageStats`. Handler-level tests can't
  exercise lifecycle events.
- Polymorphic-function return resolution — handler-level worked;
  the LSP response payload had the wrong field shape.

Each of these would have been caught by a test that sends real
LSP messages through the dispatcher and asserts on the wire-format
responses.

## 2. What's in scope vs out of scope

**In scope.** The JSON-RPC contract between the DimFort server and
any LSP client:

- Capability negotiation (`initialize` response shape).
- Document lifecycle (`didOpen` → `publishDiagnostics`; `didChange`
  → recompute → updated diagnostics).
- Request/response: `textDocument/hover`, `textDocument/definition`,
  `textDocument/codeAction`, `textDocument/inlayHint`,
  `textDocument/completion`.
- DimFort-custom requests: `dimfort/panelInfo`,
  `dimfort/interactions`, `dimfort/lineStatus`,
  `dimfort/coverageStats`.
- Notification ordering and timing — does
  `dimfort/workspaceCheckCompleted` fire after the workspace
  check, do `publishDiagnostics` reach the client in the expected
  order, do debounced features (inlay refresh throttle) behave
  under tight `didChange` sequences?
- Cache invalidation across edit sequences — the v7→v9 class.

**Out of scope.** Anything past the wire:

- Companion rendering (panel layout, hover popup styling, inlay
  positioning). Stays in the manual smoke walk.
- Cursor-following behavior (the panel updating as the user moves).
  An LSP test can verify *that* a panel request returns the right
  payload at a given position; whether the companion *fires* a
  panel request as the cursor moves is companion-side.
- Companion code paths (TypeScript / Lua / Elisp). These remain
  un-automated per the [GUI-test-automation decision](#) — the
  industry baseline for non-VSCode companions is user bug reports
  + community maintainers + the LSP wire contract this note tests.
- Cross-editor display consistency. Each companion renders LSP
  payloads its own way; the test only asserts the server's
  contribution is correct.

## 3. Tooling — `pytest-lsp`

The [`pytest-lsp`](https://github.com/swyddfa/lsprotocol) library
(by the lsprotocol maintainers) boots a real LSP server as a
subprocess and exposes a `LanguageClient` fixture that does the
LSP dance. Idiomatic test:

```python
import pytest
import pytest_lsp
from lsprotocol import types as lsp

@pytest_lsp.fixture(
    config=pytest_lsp.ClientServerConfig(server_command=["dimfort", "lsp"]),
)
async def client(lsp_client):
    await lsp_client.initialize_session(
        lsp.InitializeParams(
            capabilities=lsp.ClientCapabilities(),
            root_uri=str(FIXTURES.as_uri()),
        )
    )
    yield
    await lsp_client.shutdown_session()


async def test_hover_after_didchange_returns_updated_unit(client):
    uri = (FIXTURES / "hover_smoke.f90").as_uri()
    client.text_document_did_open(...)
    await client.wait_for_notification(lsp.PUBLISH_DIAGNOSTICS)

    h1 = await client.text_document_hover_async(...)
    assert "m/s" in h1.contents.value

    client.text_document_did_change(...)        # change the annotation
    await client.wait_for_notification(lsp.PUBLISH_DIAGNOSTICS)

    h2 = await client.text_document_hover_async(...)
    assert "kg" in h2.contents.value             # cache invalidated correctly
```

**Why not alternatives:**

- **Raw subprocess + manual JSON-RPC.** Possible, but reinvents
  framing, ID correlation, async cancellation. ~200 lines of
  harness before the first test.
- **`pygls` in-process.** Skips the subprocess + real stdio — but
  also skips the layer where serialization bugs hide. Defeats the
  reason for adding this work.
- **A custom subclass of the existing `tests/unit/test_lsp_*.py`
  pattern.** Already explored — that pattern bypasses the wire
  by design. Extending it would mean reimplementing pygls's
  dispatch.

## 4. Initial-suite scope

A new `tests/lsp_integration/` directory, ~15-20 tests across the
following test files. Each test ~20-40 lines, total ~600 lines.

| File | Tests | Coverage |
|---|---|---|
| `test_lifecycle.py` | 2-3 | `initialize` capability shape, `shutdown` cleanup. |
| `test_diagnostics.py` | 3-4 | `didOpen` → expected diagnostic codes; `didChange` updates; multi-file `publishDiagnostics` ordering. |
| `test_hover.py` | 3-4 | Hover content at known positions; hover after `didChange` (the v7→v9 class). |
| `test_inlay_and_panel.py` | 3-4 | `inlayHint` request returns expected hints; `dimfort/panelInfo` shape matches the spec; debounce / throttle behavior under rapid `didChange`. |
| `test_code_actions.py` | 2-3 | `textDocument/codeAction` returns suggested-rewrite for a U002; quick-fix applies correctly. |
| `test_workspace.py` | 2-3 | `dimfort/checkWorkspace` command triggers a full re-check; `dimfort/coverageStats` notification arrives after completion. |

**Fixtures.** A handful of `.f90` files under
`tests/lsp_integration/fixtures/` — small, narrowly-scoped per
test concern (one with a known H001 site for diagnostics, one with
a known polymorphic function for hover, etc.). No reuse of the
big `tests/fixtures/` corpus — those exist for checker tests, not
LSP wire tests.

## 5. CI integration

`pytest-lsp` runs in CI without trouble — the LSP server is just a
Python subprocess (no Electron, no Qt, no DBus). Expected runtime
~30-60s for the full suite. Add to the existing `pytest.yml`
workflow as a parallel job, gated on the `lsp` extra:

```yaml
- name: LSP integration tests
  run: uv run --extra lsp --extra dev pytest tests/lsp_integration/ -v
```

No companion toolchain dependencies. No flakiness expected (no
Qt+VTK or Electron in the picture).

## 6. Interaction with Track D Ring 2

Two of the three Track D Ring 2 items have a partial overlap with
this work; flagging here so neither duplicates the other:

- **Silent-failure audit** (Track D item 3). The proposed CI grep
  gate (`grep _notify | grep -v audited`) is a static check. LSP
  integration tests assert *behavior* — e.g., when a worker
  silently fails the server still sends a diagnostic, not a
  silent dropped notification. The two are complementary:
  static gate catches missing annotations, integration tests
  catch missing-effect bugs.
- **Cache audit completion** (Track D item 2). The
  `cache_memory_churn.py` → pytest fixture turning into a CI gate
  (per-iteration RSS growth < 50 KB) lives at a lower layer than
  the LSP wire. The LSP integration tests catch the
  *user-visible* end of cache bugs (stale hover after edit);
  the churn gate catches the *resource* end.

The cache audit + silent-failure audit should still ship — the
integration tests reduce the symptoms surface but don't replace
either.

## 7. What this enables on the QA side

Once the integration suite is in, the manual smoke walk per
companion can shrink to display-only:

- Hover popup renders the wire payload's `contents.value`
  legibly (Markdown formatting, line wrapping, color).
- Panel sections render the `dimfort/panelInfo` payload — the
  *content* of each section is wire-asserted; the walker checks
  layout, scroll, collapse state.
- Inlay hints appear at the right *visual* positions (the LSP
  test already asserted the offsets).
- Coverage bar renders the `dimfort/coverageStats` payload — the
  values are wire-asserted; the walker checks color tiers, %
  formatting.
- Diagnostic squiggles render the `publishDiagnostics` payload
  at the right line — content asserted upstream.

Concretely: a release-time smoke walk should become "5 minutes
per companion, no spec to follow because the spec-faithful checks
already ran in CI." That's the 0.2.7 outcome this work targets.

## 8. Open questions

1. **Async test framework.** `pytest-lsp` is async-native. DimFort's
   existing test suite is sync. Add `pytest-asyncio` or
   `pytest-anyio`? Either works; preference noted at
   implementation time.
2. **Per-test fixture isolation.** Each test currently restarts
   the server (~1s overhead). Could share a session-scoped server
   across tests for speed — at the cost of leaking state. Default
   to per-test until a real perf problem appears.
3. **Wire-format fixtures vs hand-coded payloads.** Some tests
   would benefit from a recorded `.jsonrpc` golden file rather
   than constructing payloads in Python. Defer the recording
   harness to a follow-up; initial suite uses hand-coded payloads.
4. **What goes into the existing handler-level tests vs the new
   integration tests?** Suggested rule: a bug found by the
   smoke walk that wasn't caught by handler tests gets an
   integration test, not a handler test. Handler tests stay for
   logic-only assertions where the wire layer adds nothing.
