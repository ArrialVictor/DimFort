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

This scope was set against a 2026-06-18 audit of the three
companion `MANUAL_QA.md` files (~705 distinct checkable assertions
across ~3,700 lines). Every assertion was classified W/B/D/N
(wire / binding / display / gap); the scope below absorbs every
**W** item plus every **N** gap surfaced by the audit, so the
manual QA residue becomes purely B + D with no wire holes.

**In scope.** The JSON-RPC contract between the DimFort server and
any LSP client. *Server effect* is in scope; *user-trigger binding*
that sends the request is not (see "Out of scope" below).

- Capability negotiation (`initialize` response shape).
- Document lifecycle: `didOpen` → `publishDiagnostics`; `didChange`
  → recompute → updated diagnostics; `didSave` re-check;
  `didClose` per-URI resource release.
- Request/response: `textDocument/hover`,
  `textDocument/definition`, `textDocument/codeAction`,
  `textDocument/inlayHint`, `textDocument/completion`.
- DimFort-custom requests: `dimfort/panelInfo`,
  `dimfort/interactions`, `dimfort/lineStatus`,
  `dimfort/coverageStats`.
- Workspace commands: `workspace/executeCommand`
  (`dimfort/checkWorkspace`) — that the server performs the
  effect, and that the duplicate-trigger guard works. The
  companion-side binding that *sends* the command is out of
  scope (see below).
- Notification ordering and timing —
  `dimfort/workspaceCheckCompleted` arrives after the workspace
  check, `publishDiagnostics` reach the client in the expected
  order, debounced features (inlay refresh throttle) behave
  under tight `didChange` sequences,
  `dimfort/coverageStats` Project segment stale-after-edit
  semantics, `workDoneProgress` `[N/5]` format.
- Cache invalidation across edit sequences — the v7→v9 class.
- LSP lifecycle robustness: `$/cancelRequest` handling on
  in-flight slow requests, request-before-`workspaceCheckCompleted`
  returns a safe partial response (no crash, no stale data),
  `workspace/didChangeWatchedFiles` triggers config auto-reload.
- Concurrency under load: rapid `didChange` burst correctness
  across hover / panelInfo / diagnostics (not just inlay).
- Multi-folder posture pinning: the documented behavior is
  *partial support* — config loaded from the first folder only,
  additional folders accepted but secondary, no
  `workspace/didChangeWorkspaceFolders` registration. Test
  asserts this posture so it stays intentional, not accidental.

**Out of scope.** Anything past the wire:

- Companion rendering (panel layout, hover popup styling, inlay
  positioning, color tiers, decoration overlays). Stays in the
  manual smoke walk.
- Cursor-following behavior (the panel updating as the user moves).
  An LSP test can verify *that* a panel request returns the right
  payload at a given position; whether the companion *fires* a
  panel request as the cursor moves is companion-side.
- **Command/keybinding bindings** — whether the companion's
  command palette entry, keybinding, context menu, or panel-row
  click actually fires the right LSP request. The integration
  tests assert the server's *response* to the request; the
  companion's *trigger* of the request is companion-side and
  remains in QA. Concretely: tests verify `workspace/executeCommand`
  `dimfort/checkWorkspace` does a re-check; the QA verifies
  `:DimFortCheckWorkspace` / `M-x dimfort-check-workspace` /
  "DimFort: Check Workspace" command-palette entries actually
  send that command. The audit counted ~130 such binding checks
  across the three companions; they all stay manual.
- Companion code paths (TypeScript / Lua / Elisp). Remain
  un-automated per the GUI-test-automation decision — the
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

A new `tests/lsp_integration/` directory, ~30 tests across the
following test files. Each test ~20-40 lines, total ~900 lines.
The scope absorbs the 15-gap audit (§2 audit) plus 5 lifecycle-
robustness additions (cancellation, init race, didClose, burst,
multi-folder posture).

| File | Tests | Coverage |
|---|---|---|
| `test_lifecycle.py` | 6-7 | `initialize` capability shape; `shutdown` cleanup; `$/cancelRequest` on in-flight slow requests; request-before-`workspaceCheckCompleted` returns safe partial; `didClose` releases per-URI resources; `workspace/didChangeWatchedFiles` triggers config auto-reload; multi-folder posture pinning (config from first folder only). |
| `test_diagnostics.py` | 5-6 | `didOpen` → expected codes (H001, U002, U005, H010, P001); `didChange` updates incl. U005 propagation; multi-file `publishDiagnostics` ordering; `[diagnostics]` severity override via config; `@unit_assume` U020 INFO + assumed-marker payload field; polymorphism H020/H021/H022/H023 wire payloads; rapid `didChange` burst — latest diagnostics, no interleave. |
| `test_hover.py` | 5-6 | Hover content at known positions; hover after `didChange` (v7→v9 class); detailed-vs-short verbosity differs by setting; LOG/EXP tree-shape parity with user calls; cross-file `textDocument/definition` jump; scale-mode payload difference (`100×kg·m⁻¹·s⁻²` vs `kg·m⁻¹·s⁻²`); function-definition pure-signature single-line format vs call-site tree. |
| `test_inlay_and_panel.py` | 4-5 | `inlayHint` request returns expected hints; `dimfort/panelInfo` shape matches spec; inlay refresh throttle under rapid `didChange`; `dimfort/interactions` X001 + Declaration/Read grouping; Imports transitive re-export shape (`via phys_base`, mixed kinds, `density : ? 🟡`). |
| `test_code_actions.py` | 3 | `textDocument/codeAction` returns suggested-rewrite for U002; quick-fix `WorkspaceEdit` applies correctly; snippet `$0` cursor placement on Add-`@unit{}` action. |
| `test_workspace.py` | 5 | `dimfort/checkWorkspace` command triggers a full re-check; `dimfort/coverageStats` notification arrives after completion; Project segment stale-after-edit semantics; duplicate-trigger guard (second `dimfort/checkWorkspace` returns ack without spawning); `workDoneProgress` `[N/5]` format. |
| `test_completion.py` | 1-2 | `textDocument/completion` for unit names after typing `!< @unit{` returns expected entries. |

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

The audit (§2) classified ~705 distinct assertions across the
three companion `MANUAL_QA.md` files. The reduction this work
enables is summarized below; each companion's residue covers
display rendering + companion-side bindings + the few items the
audit explicitly recommended leaving manual.

| Companion | Current QA lines | Projected residue | Reduction |
|---|---|---|---|
| VSCompanion | ~1,304 | ~530-580 | ~55% |
| EmacsCompanion | ~1,244 | ~500-550 | ~55% |
| NvimCompanion | ~1,174 | ~480-520 | ~55% |

The three residue categories:

**(D) Display rendering — must stay manual.** The walker checks
the *appearance* of the wire payloads the integration tests
asserted:

- Hover popup renders the wire payload's `contents.value`
  legibly (Markdown formatting, line wrapping, tree characters
  `├──`/`└──`, circle glyphs 🟢🟡🔴🔵, `🔵 assumed:` placement,
  `(expected …)` trailer).
- Panel sections render the `dimfort/panelInfo` payload —
  section order, collapsible headers, dividers, sub-section
  indent, column alignment, stacked-scope indent.
- Inlay hints appear at the right visual positions and weight
  (full-weight `'a`, not dimmed).
- Coverage decorations match the wire-asserted tier (gutter-dot
  color, background-tint alpha, gutter-vs-background mutual
  exclusion, multi-pane paint, reload persistence).
- Coverage footer / status-bar item formatting (kilo-formatted
  counts, hover-tooltip File/Project table, Project-dim-on-stale
  codicon, 200 ms tab-switch debounce, Braille spinner).
- Diagnostic styling (squiggle colors, fringe styling, sign-column
  letters, faint blue P001 underline distinct from H001 red).
- Progress UI rendering (`[N/5]` mode-line / status-bar / fidget).

**(B) Companion-side command bindings — must stay manual.** The
walker confirms each user-facing trigger fires the right LSP
request. The integration tests assert the server's *response* to
the request; the companion's *trigger* is companion-specific.

- Command palette / `:Cmd` / `M-x` entries for every documented
  command: `CheckWorkspace`, `Restart`, `ClearCache`, `CycleCache`,
  `OpenConfig`, `Status`, `CycleHover`, `ToggleInlayHints`,
  `TogglePanel`, `CycleScale`, `CycleCoverage`, `CycleSortMode`,
  `CycleUnitDisplay`, `CoverageReport`, `ToggleCursor/Scope/Imports`,
  `ScopeFilter`, `ImportsFilter`. ~17 commands × 3 companions.
- Native LSP bindings the companion wires up: `Cmd+.` / `gra`
  code-action shortcut, `K` / `Cmd+K Cmd+I` hover, `F12` / `M-.`
  go-to-definition, `<C-x><C-o>` / `ESC TAB` completion,
  context-menu entries, activity-bar icon, settings-UI enum
  picker.
- Panel-internal bindings: click-to-navigate / RET on row,
  sort-icon click, per-View drag/dock/hide.
- Settings-persistence bindings: companion-specific config keys
  reflecting in toggles.

Total: ~130 binding checks across the three companions
(~40-50 per companion). Cheap to walk — most reduce to "open
command palette, check entry is present, invoke it, observe
behavior matches the documented effect."

**(M) Manual residue beyond B + D — small.** A handful of items
the audit recommended leaving manual rather than absorbing into
the integration suite, because the wire isn't the right surface:

- Cache-mode-at-restart drift (companion-side restart logic;
  covered upstream by existing `tests/unit/` cache tests +
  Track D Ring 2's RSS churn gate).
- Companion-side restart UX after server crash recovery.
- Any check that fundamentally needs an editor process to
  exercise (e.g., webview HTML rendering correctness in
  VSCompanion).

Concretely: a release-time smoke walk per companion should be
~5-10 minutes — open the fixture, walk the B trigger list, eyeball
the D rendering, no spec-faithful content assertions because those
all ran in CI. That's the 0.2.7 outcome this work targets.

## 7a. Release-procedure QA — not replaced by this work

Three release-time checks remain a release-procedure concern,
unchanged by the integration suite. Flagged here so they don't get
silently dropped during the QA rewrite:

- **Pre-publish install smoke.** `pipx install dimfort==<candidate>`
  on a clean shell — does the wheel install, does the `dimfort`
  entrypoint resolve, does `pipx install 'dimfort[lsp]'` pull
  pygls. Same for the VSCode `.vsix` (`vsce package` → install
  into a fresh VSCode → server boots). Has bitten release-day in
  other projects (PyPI metadata issues, missing wheel entries,
  optional-dep wheels).
- **Cross-version companion compatibility.** Run the *previous*
  companion version against the *new* server. If a wire-format
  bump silently breaks the prev-companion's parse, every user
  with auto-update-off sees crashes after a server bump. Semver
  discipline check; should land in the release procedure as a
  one-line gate.
- **Companion `.vsix` / `ovsx` / Open VSX publish parity.**
  After publish, install from each marketplace and boot — catches
  packaging-pipeline drift between publish targets (per the
  documented dual-registry posture).

These remain release-procedure items, not test-suite items, because
each requires a clean install environment. Could be automated as a
release-day script, but the integration suite is not the right home.

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
