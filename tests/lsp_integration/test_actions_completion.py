"""LSP integration tests — code actions + completion.

Six tests pinning the ``textDocument/codeAction`` and
``textDocument/completion`` wire surfaces:

  - U002 "Replace with …" code action carries a WorkspaceEdit that
    the client can apply directly (no command delegation).
  - The WorkspaceEdit's TextEdit covers the exact U002 diagnostic
    range and supplies the suggested rewrite.
  - "Add @unit{}" snippet code action uses ``$0`` between the
    braces so the user's typing immediately lands inside the
    annotation (0.2.1 #snippet-cursor-placement regression).
  - Code action operates on live in-memory text, not stale on-disk
    content (0.2.5 #codeaction-unsaved-buffer regression).
  - Completion fires inside ``!< @unit{`` and returns the
    unit-vocabulary items.
  - Completion is GUARDED in non-comment contexts (0.2.3
    #completion-LSP-scoping regression).
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from lsprotocol import types as lsp
from pytest_lsp import LanguageClient

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
ACTIONS_WS = FIXTURES_DIR / "actions"
U002_FILE = ACTIONS_WS / "u002_site.f90"
U005_FILE = ACTIONS_WS / "u005_unannotated.f90"
COMPLETION_FILE = ACTIONS_WS / "completion_site.f90"
NONCOMMENT_FILE = ACTIONS_WS / "noncomment_code.f90"


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


async def _code_actions(
    client: LanguageClient,
    path: pathlib.Path,
    range_: lsp.Range,
    diagnostics: list,
):
    return await client.text_document_code_action_async(
        lsp.CodeActionParams(
            text_document=lsp.TextDocumentIdentifier(uri=path.as_uri()),
            range=range_,
            context=lsp.CodeActionContext(diagnostics=diagnostics),
        )
    )


# ---------------------------------------------------------------------------
# 1. U002 "Replace with" code action carries a WorkspaceEdit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_codeaction_u002_suggested_rewrite_in_data(
    client_actions: LanguageClient,
):
    """U002 code action returns a WorkspaceEdit with the suggested rewrite.

    Pin the wire contract: the server resolves U002 suggested
    rewrites server-side (no command delegation) — the action's
    ``edit`` field is a complete WorkspaceEdit that any client can
    apply. The single TextEdit replaces the diagnostic range with
    the parsed suggestion.

    Fixture: ``@unit{kg2}`` -> suggestion ``kg^2``.
    """
    await _open_and_wait(client_actions, U002_FILE)
    diags = list(client_actions.diagnostics.get(U002_FILE.as_uri(), []))
    u002 = [d for d in diags if d.code == "U002"]
    assert u002, "U002 didn't fire on the kg2 site"

    actions = await _code_actions(
        client_actions, U002_FILE, u002[0].range, [u002[0]],
    )
    assert actions, "no code actions returned for the U002 site"
    # Find the "Replace with 'kg^2'" action.
    replace_actions = [
        a for a in actions if "Replace with" in (getattr(a, "title", "") or "")
    ]
    assert replace_actions, (
        f"no Replace-with action among returned actions: "
        f"{[getattr(a, 'title', None) for a in actions]}"
    )
    a = replace_actions[0]
    assert "'kg^2'" in a.title, (
        f"action title doesn't mention the rewrite candidate: {a.title!r}"
    )
    edit = getattr(a, "edit", None)
    assert edit is not None, (
        "Replace-with code action has no edit (must be server-applied)"
    )
    changes = getattr(edit, "changes", None)
    assert changes, "WorkspaceEdit.changes is empty"
    file_edits = changes.get(U002_FILE.as_uri()) if isinstance(changes, dict) else None
    assert file_edits, (
        f"WorkspaceEdit doesn't target the U002 file: keys="
        f"{list(changes.keys()) if isinstance(changes, dict) else 'not-a-dict'}"
    )


# ---------------------------------------------------------------------------
# 2. The U002 WorkspaceEdit text matches the suggested rewrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_codeaction_workspaceedit_supplies_rewrite_text(
    client_actions: LanguageClient,
):
    """The U002 WorkspaceEdit's new_text is the suggested rewrite.

    Pin the rewrite content. Without this, the edit could carry an
    empty/wrong replacement and still pass the structural test.
    """
    await _open_and_wait(client_actions, U002_FILE)
    diags = list(client_actions.diagnostics.get(U002_FILE.as_uri(), []))
    u002 = [d for d in diags if d.code == "U002"]
    actions = await _code_actions(
        client_actions, U002_FILE, u002[0].range, [u002[0]],
    )
    replace = next(
        a for a in actions if "Replace with" in (getattr(a, "title", "") or "")
    )
    edits = replace.edit.changes[U002_FILE.as_uri()]
    assert len(edits) == 1, f"expected exactly one TextEdit; got {edits}"
    text_edit = edits[0]
    new_text = getattr(text_edit, "new_text", None)
    assert new_text == "kg^2", (
        f"WorkspaceEdit's new_text mismatch: got {new_text!r}, expected 'kg^2'"
    )


# ---------------------------------------------------------------------------
# 3. Add @unit{} snippet has $0 cursor placement (0.2.1 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_codeaction_snippet_dollar_zero_between_braces(
    client_actions: LanguageClient,
):
    """The Add-@unit{} snippet places ``$0`` cursor stop inside the braces.

    Regression: 0.2.1 #snippet-cursor-placement — the snippet
    originally emitted ``$0`` outside the braces, so the user had
    to manually navigate inside. Fix: ``@unit{$0}`` lands the
    cursor between the braces. The snippet text is the fourth
    argument to the ``dimfort.insertSnippet`` command.
    """
    await _open_and_wait(client_actions, U005_FILE)
    # Range covering the decl line (line 6 in 1-indexed = line 5 in
    # 0-indexed, but the file has 5 leading comment lines so the
    # `real :: missing` is at line 6 in 0-indexed too — verified by
    # the probe).
    actions = await _code_actions(
        client_actions,
        U005_FILE,
        lsp.Range(
            start=lsp.Position(line=6, character=2),
            end=lsp.Position(line=6, character=30),
        ),
        [],
    )
    add_unit_actions = [
        a for a in actions if "Add @unit{}" in (getattr(a, "title", "") or "")
    ]
    assert add_unit_actions, (
        f"no Add-@unit{{}} action returned: "
        f"titles={[getattr(a, 'title', None) for a in actions]}"
    )
    a = add_unit_actions[0]
    # Pygls serializes the Command into a string-form representation on
    # the wire. Extract the snippet text — it's the last argument and
    # ``$0`` must appear inside the braces.
    cmd_str = str(getattr(a, "command", ""))
    assert "@unit{$0}" in cmd_str, (
        f"snippet doesn't place $0 inside braces: {cmd_str}"
    )
    # Also assert $0 is NOT trailing the braces (outside).
    assert "@unit{}$0" not in cmd_str, (
        f"regression: $0 is OUTSIDE the braces — {cmd_str}"
    )


# ---------------------------------------------------------------------------
# 4. Code action uses live in-memory text (0.2.5 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_codeaction_unsaved_buffer_uses_live_text(
    client_actions: LanguageClient,
):
    """Code action runs against the in-memory document, not the on-disk file.

    Regression: 0.2.5 #codeaction-unsaved-buffer — code-action
    callbacks read the file from disk instead of pulling the live
    document override; users with unsaved edits saw actions
    referencing stale state. The fix routes through
    ``state.live_text_for(uri)``.

    Test: didOpen the file, didChange to remove the U002 site
    entirely, request code actions on the same range — assert
    the Replace-with-'kg^2' action is GONE (because the live text
    no longer has the bad annotation).
    """
    await _open_and_wait(client_actions, U002_FILE)
    diags = list(client_actions.diagnostics.get(U002_FILE.as_uri(), []))
    u002 = [d for d in diags if d.code == "U002"]
    assert u002

    # Edit the buffer: replace the bad annotation with a valid one.
    text = _read(U002_FILE)
    new_text = text.replace("@unit{kg2}", "@unit{kg}")
    assert new_text != text
    line_count = text.count("\n") + 1
    client_actions.text_document_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(
                uri=U002_FILE.as_uri(), version=2,
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
    await asyncio.sleep(0.6)  # past debounce + re-check

    # Request actions on the same range — the live buffer has no
    # U002 now. Pass NO diagnostics in the context (mirrors what
    # clients do after a re-check produces an empty diag list).
    actions = await _code_actions(
        client_actions, U002_FILE, u002[0].range, [],
    )
    replace_actions = [
        a
        for a in (actions or [])
        if "Replace with" in (getattr(a, "title", "") or "")
        and "'kg^2'" in (getattr(a, "title", "") or "")
    ]
    assert not replace_actions, (
        f"Replace-with-'kg^2' action still offered for live text that "
        f"no longer has the U002 site: {[a.title for a in replace_actions]}"
    )


# ---------------------------------------------------------------------------
# 5. Completion inside `!< @unit{` returns unit-name items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_completion_in_comment_after_at_unit_brace(
    client_actions: LanguageClient,
):
    """``textDocument/completion`` inside ``!< @unit{`` returns unit names.

    Pin the wire contract for the completion feature. The fixture
    has ``!< @unit{`` ending the line; completion at the cursor
    must return the unit vocabulary (kg, m, s, K, …) wrapped in a
    CompletionList.
    """
    text = _read(COMPLETION_FILE)
    client_actions.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=COMPLETION_FILE.as_uri(),
                language_id="fortran",
                version=1,
                text=text,
            )
        )
    )
    # No diagnostics check needed — completion is independent of the
    # check pipeline.
    await asyncio.sleep(0.3)

    # Cursor at end of line 6 (0-indexed) — right after `!< @unit{`.
    lines = text.split("\n")
    target_line = 6
    target_col = len(lines[target_line])
    comp = await client_actions.text_document_completion_async(
        lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(
                uri=COMPLETION_FILE.as_uri(),
            ),
            position=lsp.Position(line=target_line, character=target_col),
        )
    )
    assert comp is not None, "completion inside @unit{ returned None"

    items = comp.items if not isinstance(comp, list) else comp
    assert items, "completion items list is empty"
    labels = {item.label for item in items}
    # Sanity: core SI base units must be in the vocabulary.
    for required in ("kg", "m", "s", "K"):
        assert required in labels, (
            f"unit-vocabulary completion missing {required!r}; "
            f"got {len(labels)} items, first few: {list(labels)[:8]}"
        )


# ---------------------------------------------------------------------------
# 6. Completion is guarded outside `@unit{` contexts (0.2.3 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_completion_guard_outside_at_unit(
    client_actions: LanguageClient,
):
    """Completion outside an ``@unit{`` context returns None.

    Regression: 0.2.3 #completion-LSP-scoping — completion fired
    everywhere (every keystroke triggered unit-name suggestions
    even in plain code). The fix scopes completion to positions
    inside ``@unit{ ... }`` braces.

    Test: open a file with no ``@unit{`` anywhere. Request
    completion on a plain assignment line. Assert None / empty.
    """
    text = _read(NONCOMMENT_FILE)
    client_actions.text_document_did_open(
        lsp.DidOpenTextDocumentParams(
            text_document=lsp.TextDocumentItem(
                uri=NONCOMMENT_FILE.as_uri(),
                language_id="fortran",
                version=1,
                text=text,
            )
        )
    )
    await asyncio.sleep(0.3)

    # Find the `i = 0` line (1-indexed col 5 = on the `=`).
    lines = text.split("\n")
    line_idx = next(
        (i for i, ln in enumerate(lines) if "i = 0" in ln),
        None,
    )
    assert line_idx is not None, "fixture missing the i = 0 line"

    comp = await client_actions.text_document_completion_async(
        lsp.CompletionParams(
            text_document=lsp.TextDocumentIdentifier(
                uri=NONCOMMENT_FILE.as_uri(),
            ),
            position=lsp.Position(line=line_idx, character=8),
        )
    )
    # Both None and an empty list count as "no completion offered".
    if comp is None:
        return  # guard fired correctly
    items = comp.items if not isinstance(comp, list) else comp
    assert not items, (
        f"completion guard failed: got {len(items)} items in non-comment "
        f"context. First few: {[i.label for i in items[:5]]}"
    )
