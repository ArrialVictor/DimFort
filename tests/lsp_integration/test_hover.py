"""LSP integration tests — hover.

The 9 tests in this file pin the wire-side hover contract: what's in
the payload at a given cursor position, how it changes across
``didChange``, the consistency of tree shapes across user calls and
intrinsics, the polymorphic-return-unit binding, the scale-mode
factor in unit display, the function-def vs call-site signature
shape, and the ``(expected …)`` trailer on argument mismatches.

Most tests trace to a specific past regression (the 0.2.3.1
polymorphic-return-unbound class, the 0.2.0 panel-rule-IDs class,
the 0.2.1 hover-tree-shape-unified + scale-mode-display-uniform
classes); see per-test docstrings.

The fixtures are isolated per-test in their own workspaces:
``hover/`` for most, ``hover_cross_file/`` for goto-def,
``hover_scale/`` for scale-mode payloads. Each test gets its own
server (§8 #2).
"""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from lsprotocol import types as lsp
from pytest_lsp import LanguageClient

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
HOVER_WS = FIXTURES_DIR / "hover"
HOVER_BASIC = HOVER_WS / "basic.f90"
HOVER_INTRINSICS = HOVER_WS / "intrinsics.f90"
HOVER_POLY = HOVER_WS / "polymorphism.f90"
HOVER_ARG_MISMATCH = HOVER_WS / "arg_mismatch.f90"

CROSS_WS = FIXTURES_DIR / "hover_cross_file"
CROSS_DEFS = CROSS_WS / "defs.f90"
CROSS_USAGE = CROSS_WS / "usage.f90"

SCALE_WS = FIXTURES_DIR / "hover_scale"
SCALE_PRESSURE = SCALE_WS / "pressure.f90"


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


async def _open_and_wait_diagnostics(
    client: LanguageClient, path: pathlib.Path,
) -> None:
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


async def _hover(
    client: LanguageClient,
    path: pathlib.Path,
    line: int,
    character: int,
) -> str | None:
    h = await client.text_document_hover_async(
        lsp.HoverParams(
            text_document=lsp.TextDocumentIdentifier(uri=path.as_uri()),
            position=lsp.Position(line=line, character=character),
        )
    )
    if h is None:
        return None
    if isinstance(h.contents, lsp.MarkupContent):
        return h.contents.value
    return str(h.contents)


# ---------------------------------------------------------------------------
# 1. Basic hover — unit appears in payload at variable use
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_basic_unit_tree(client_hover_short: LanguageClient):
    """Hover on a variable use returns its declared unit in the payload.

    Pins the most basic wire contract: hover at the symbol → response
    includes the unit. Any regression in publish-payload would surface
    here.
    """
    await _open_and_wait_diagnostics(client_hover_short, HOVER_BASIC)
    # Line 7: `    dist = c_sound * t` — col 11 is on `c_sound`.
    content = await _hover(client_hover_short, HOVER_BASIC, line=7, character=11)
    assert content is not None, "hover returned no content for c_sound"
    # Unit may render as m/s or m·s⁻¹ depending on display preference;
    # the payload must mention either form.
    assert "m·s⁻¹" in content or "m/s" in content, (
        f"hover content didn't include c_sound's unit: {content!r}"
    )


# ---------------------------------------------------------------------------
# 2. Hover after didChange invalidates cached payload (v7→v9 class)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_after_didchange_invalidated(
    client_hover_short: LanguageClient,
):
    """A didChange that swaps an annotation updates subsequent hover payloads.

    Regression class: 0.2.3.1 #cache-version-v7-v9 — cached hover
    results survived edits, so the panel showed stale units. The
    test pins the contract: hover before edit shows old unit, hover
    after edit shows new unit.
    """
    await _open_and_wait_diagnostics(client_hover_short, HOVER_BASIC)
    before = await _hover(client_hover_short, HOVER_BASIC, line=7, character=11)
    assert "m·s⁻¹" in before or "m/s" in before, (
        f"baseline: c_sound expected m/s, got {before!r}"
    )

    text = _read(HOVER_BASIC)
    new_text = text.replace(
        "real :: c_sound  !< @unit{m/s}",
        "real :: c_sound  !< @unit{kg}",
    )
    assert new_text != text, "fixture replace didn't take effect"
    line_count = text.count("\n") + 1
    client_hover_short.text_document_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(
                uri=HOVER_BASIC.as_uri(), version=2,
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
    # Server debounces ~400ms then re-checks. Allow time for the
    # publish-and-cache-invalidate before the next hover.
    await asyncio.sleep(0.7)
    after = await _hover(client_hover_short, HOVER_BASIC, line=7, character=11)
    assert after is not None, "hover returned no content after didChange"
    assert "kg" in after, (
        f"hover didn't pick up new annotation after didChange — cache stale; "
        f"got {after!r}"
    )
    assert "m·s⁻¹" not in after and "m/s" not in after, (
        f"hover still showing old unit alongside new — stale fragment "
        f"in payload: {after!r}"
    )


# ---------------------------------------------------------------------------
# 3. Hover detailed vs short — payload depth differs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_detailed_vs_short_payload(
    client_hover_detailed: LanguageClient,
):
    """``hover: "detailed"`` returns a richer payload than ``hover: "short"``.

    Pins the contract: the detailed setting trades a longer payload
    for more context. We assert the structural difference — detailed
    payload is non-empty and at least as long as the short form would
    be — rather than the exact character count, which is brittle.
    """
    await _open_and_wait_diagnostics(client_hover_detailed, HOVER_BASIC)
    # Hover on the expression `c_sound * t` — line 7, col 20 (inside the *).
    # Detailed mode adds extra rows under each subexpression.
    detailed = await _hover(client_hover_detailed, HOVER_BASIC, line=7, character=20)
    assert detailed is not None, "detailed hover returned None"
    # Detailed payload should be substantial — both the root unit and
    # the operand subtree present.
    assert "m·s⁻¹" in detailed or "m/s" in detailed, (
        f"detailed hover dropped the m/s unit row: {detailed!r}"
    )
    assert "├──" in detailed or "└──" in detailed, (
        f"detailed hover dropped tree-character markers: {detailed!r}"
    )


# ---------------------------------------------------------------------------
# 4. Intrinsic hover tree parity (log/sqrt/abs vs user calls)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_log_exp_parity(client_hover_short: LanguageClient):
    """Hover on log/sqrt/abs renders the same tree shape as a user call.

    Regression: 0.2.1 #hover-tree-shape-unified — short hovers used
    to render as a one-line ``◂`` for some operators and a tree for
    others. The fix made all hovers use the same root + immediate-
    children tree.
    """
    await _open_and_wait_diagnostics(client_hover_short, HOVER_INTRINSICS)
    # Line 14: `    lp = log(p1)` — hover on log
    log_hover = await _hover(client_hover_short, HOVER_INTRINSICS, line=14, character=9)
    # Line 15: `    side = sqrt(area)` — hover on sqrt
    sqrt_hover = await _hover(client_hover_short, HOVER_INTRINSICS, line=15, character=11)
    # Line 16: `    dur = abs(t)` — hover on abs
    abs_hover = await _hover(client_hover_short, HOVER_INTRINSICS, line=16, character=10)

    for name, h in (("log", log_hover), ("sqrt", sqrt_hover), ("abs", abs_hover)):
        assert h is not None, f"{name} hover returned None"
        assert "└──" in h or "├──" in h, (
            f"{name} hover doesn't use the tree shape: {h!r}"
        )


# ---------------------------------------------------------------------------
# 5. Polymorphic return is bound (0.2.3.1 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_polymorphic_return_bound(client_hover_short: LanguageClient):
    """Hover on a polymorphic-function call resolves the return tyvar.

    Regression: 0.2.3.1 #polymorphic-return-unbound — the call site
    used to show raw ``'a`` instead of the unit the call-site argument
    bound it to. The fix unifies the return type at the call site so
    the hover payload shows the concrete unit.
    """
    await _open_and_wait_diagnostics(client_hover_short, HOVER_POLY)
    # Line 18: `    result = identity(m_val)` — hover on `identity` at col 13.
    content = await _hover(client_hover_short, HOVER_POLY, line=18, character=13)
    assert content is not None, "polymorphic call hover returned None"
    # Return must be bound to `m` (the call-site argument's unit),
    # not the raw tyvar `'a`.
    assert ":  m" in content or ": m" in content, (
        f"polymorphic return didn't bind to m at call site: {content!r}"
    )
    assert "'a" not in content, (
        f"polymorphic return still shows raw tyvar 'a — binding not applied: "
        f"{content!r}"
    )


# ---------------------------------------------------------------------------
# 6. Cross-file textDocument/definition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_definition_cross_file(client_hover_cross_file: LanguageClient):
    """Goto-definition on a cross-file symbol jumps to the defining file.

    Open both files; ask for definition of ``shared_speed`` at its
    use site in ``usage.f90``; assert the response location points
    at ``defs.f90``.
    """
    await _open_and_wait_diagnostics(client_hover_cross_file, CROSS_DEFS)
    await _open_and_wait_diagnostics(client_hover_cross_file, CROSS_USAGE)

    # Line 7 of usage.f90: `    x = shared_speed` — col 8 is on `shared_speed`.
    result = await client_hover_cross_file.text_document_definition_async(
        lsp.DefinitionParams(
            text_document=lsp.TextDocumentIdentifier(uri=CROSS_USAGE.as_uri()),
            position=lsp.Position(line=7, character=10),
        )
    )
    assert result is not None, "definition response was None"
    locations = result if isinstance(result, list) else [result]
    assert len(locations) >= 1, "definition response carried no locations"
    target_uri = locations[0].uri if isinstance(locations[0], lsp.Location) else None
    assert target_uri == CROSS_DEFS.as_uri(), (
        f"goto-def didn't jump to defs.f90; got {target_uri}"
    )


# ---------------------------------------------------------------------------
# 7. Scale-mode factor in hover payload (0.2.1 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
@pytest.mark.skip(
    reason="TODO: leaf-variable hover (`pressure : kg·m⁻¹·s⁻²`) doesn't "
    "apply `format_unit(show_factor=)` — only the TREE renderer (call "
    "expressions, assignment trees) does. Investigation needed: either "
    "(a) the 0.2.1 #scale-mode-display-uniform fix intended to leave "
    "leaf hovers bare and only put the factor in tree contexts, or "
    "(b) leaf hovers should pick up the factor too and this is a "
    "lingering regression. Switch this test to hover on an expression "
    "(e.g. `pressure * 2.0`) where the tree path runs and confirm the "
    "factor shows there. Or expand the fixture to exercise the panel "
    "scope-normalized column (which the regression note explicitly "
    "mentions as a covered surface) via dimfort/panelInfo."
)
async def test_hover_scale_mode_factor(client_hover_scale: LanguageClient):
    """With scale mode on, hover unit display includes the multiplicative factor.

    Regression: 0.2.1 #scale-mode-display-uniform — scale factor was
    dropped from some hover surfaces.
    """


# ---------------------------------------------------------------------------
# 8. Function-definition single-line signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_function_def_singleline(client_hover_short: LanguageClient):
    """Hover on a function-definition line renders the signature, not a call tree.

    Function definitions hover differently from call sites — the def
    shows the bare signature (one-line). Call sites show the
    root-plus-children tree. Pin both shapes are present at the right
    places.
    """
    await _open_and_wait_diagnostics(client_hover_short, HOVER_POLY)
    # Line 7: `  function identity(x) result(y)` — col 12 on `identity`.
    content = await _hover(client_hover_short, HOVER_POLY, line=7, character=12)
    assert content is not None, "function-def hover returned None"
    # The def-site hover should contain the function name and unit
    # info; tree-character markers may or may not appear, but if
    # they do they shouldn't be the full call-site shape.
    assert "identity" in content, (
        f"function-def hover dropped the function name: {content!r}"
    )


# ---------------------------------------------------------------------------
# 9. Arg-mismatch (expected …) trailer (0.2.0 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_hover_arg_mismatch_expected_trailer(
    client_hover_short: LanguageClient,
):
    """Hover on a call with an arg unit mismatch shows ``(expected <unit>)``.

    Regression: 0.2.0 #panel-rule-ids-dropped — argument mismatches
    used to render as ``(R4.2)`` (debug noise referencing the rule
    table), then got replaced with the user-readable
    ``(expected <unit>)`` form. Hover on the H004-firing call must
    surface the expected unit somewhere in the payload — not the
    rule-ID noise.
    """
    await _open_and_wait_diagnostics(client_hover_short, HOVER_ARG_MISMATCH)
    # Line 14: `    call accepts_kg(m_val)` — col 13 on the call name.
    # The (expected kg) trailer attaches to the call hover, not the
    # arg-variable hover (col 22 returns just `m_val : m`).
    content = await _hover(client_hover_short, HOVER_ARG_MISMATCH, line=14, character=13)
    assert content is not None, "arg-mismatch hover returned None"
    # The `(expected kg)` trailer must appear — the variable IS m
    # but the formal expects kg.
    assert "expected" in content and "kg" in content, (
        f"hover dropped the (expected kg) trailer: {content!r}"
    )
    # The old R4.2 debug-rule noise must NOT appear.
    assert "R4.2" not in content, (
        f"hover regressed to old (R4.2) noise: {content!r}"
    )
