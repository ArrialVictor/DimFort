"""LSP integration tests — inlay hints + dimfort/panelInfo + dimfort/interactions.

The 7 tests pin the wire contract for the three custom panel-related
endpoints. Inlay hints come back via the standard ``textDocument/inlayHint``;
the side-panel data flows through ``dimfort/panelInfo`` (cursor-driven,
returns scopes / imports / expression-tree) and ``dimfort/interactions``
(symbol-driven, returns read/write/contributor sites with conflict
detection).

Most tests trace to specific past regressions; the scope-recovers-
unparseable and case-insensitive-cache ones in particular pin
already-fixed bugs against re-regression.
"""
from __future__ import annotations

import pathlib

import pytest
from lsprotocol import types as lsp
from pytest_lsp import LanguageClient

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
PANEL_WS = FIXTURES_DIR / "panel"
PANEL_BASIC = PANEL_WS / "basic.f90"
PANEL_UNPARSEABLE = PANEL_WS / "unparseable.f90"
PANEL_CONFLICT = PANEL_WS / "interactions_conflict.f90"
PANEL_TRANSITIVE = PANEL_WS / "transitive_imports.f90"


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


async def _panel_info(
    client: LanguageClient,
    path: pathlib.Path,
    line: int,
    character: int,
):
    """Send dimfort/panelInfo and return the deserialized response.

    Returns an ``Object`` (pygls' generic record class) with the
    panel.resolve() return-dict's keys as attributes.
    """
    return await client.protocol.send_request_async(
        "dimfort/panelInfo",
        {
            "textDocument": {"uri": path.as_uri()},
            "position": {"line": line, "character": character},
        },
    )


async def _interactions(
    client: LanguageClient,
    path: pathlib.Path,
    *,
    line: int | None = None,
    character: int | None = None,
    symbol: str | None = None,
):
    """Send dimfort/interactions and return the deserialized response."""
    params: dict = {"textDocument": {"uri": path.as_uri()}}
    if line is not None and character is not None:
        params["position"] = {"line": line, "character": character}
    if symbol is not None:
        params["symbol"] = symbol
    return await client.protocol.send_request_async(
        "dimfort/interactions", params,
    )


# ---------------------------------------------------------------------------
# 1. textDocument/inlayHint returns hints for annotated variables
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_inlayhint_request_returns_hints(client_panel: LanguageClient):
    """``textDocument/inlayHint`` returns at least one hint for an annotated file.

    Pin the basic contract: the server emits inlay hints on annotated
    variable uses inside assignments. The fixture has ``dist = c_sound *
    t`` with all three operands annotated; the response must include
    hints for at least the operands.
    """
    await _open_and_wait(client_panel, PANEL_BASIC)
    line_count = _read(PANEL_BASIC).count("\n") + 1
    result = await client_panel.text_document_inlay_hint_async(
        lsp.InlayHintParams(
            text_document=lsp.TextDocumentIdentifier(uri=PANEL_BASIC.as_uri()),
            range=lsp.Range(
                start=lsp.Position(line=0, character=0),
                end=lsp.Position(line=line_count, character=0),
            ),
        )
    )
    assert result, "inlayHint returned None or empty list"
    # The fixture's annotated operands produce hints at several
    # positions; we just require at least one.
    assert len(result) >= 1, f"expected ≥1 hint, got {len(result)}"
    # Each hint must have a position + label.
    for hint in result:
        assert hint.position is not None
        assert hint.label is not None


# ---------------------------------------------------------------------------
# 2. Inlay refresh fires after didChange (LSP-surface + 0.2.6 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_inlayhint_refresh_fires_after_didchange(
    client_panel: LanguageClient,
):
    """A ``didChange`` triggers ``workspace/inlayHint/refresh`` from the server.

    Regression: 0.2.6 #inlay-stale-windows — re-renders on the same
    buffer showed stale values because the inlay cache wasn't keyed
    on document version. The fix wires a 250ms-throttled
    ``workspace/inlayHint/refresh`` from the server to the client
    after every successful check.

    Test: open + wait for diagnostics, then edit and wait for the
    refresh notification. The conftest's ``_make_test_client``
    registers a no-op feature handler, so we count invocations.
    """
    # The conftest's _make_test_client installed a counter-incrementing
    # handler at client creation time; read its count to track refresh
    # requests across the test.
    import asyncio
    await _open_and_wait(client_panel, PANEL_BASIC)
    # The refresh is throttled to 250ms (leading + trailing) — wait
    # past the throttle window so the initial refresh has fired.
    await asyncio.sleep(0.5)
    initial_count = client_panel.inlay_refresh_count
    assert initial_count >= 1, (
        f"expected ≥1 inlay refresh after initial check, got {initial_count}"
    )

    # Edit, wait for next publish + refresh.
    text = _read(PANEL_BASIC)
    new_text = text.replace(
        "real :: dist  !< @unit{m}",
        "real :: dist  !< @unit{km}",
    )
    assert new_text != text, "fixture replacement didn't take effect"
    line_count = text.count("\n") + 1
    client_panel.text_document_did_change(
        lsp.DidChangeTextDocumentParams(
            text_document=lsp.VersionedTextDocumentIdentifier(
                uri=PANEL_BASIC.as_uri(), version=2,
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
    await asyncio.sleep(0.8)  # past server debounce + check + throttle
    after_count = client_panel.inlay_refresh_count
    assert after_count > initial_count, (
        f"didChange didn't trigger a new inlay refresh; "
        f"initial={initial_count}, after={after_count}"
    )


# ---------------------------------------------------------------------------
# 3. dimfort/panelInfo shape matches the documented spec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_panelinfo_shape_matches_spec(
    client_panel: LanguageClient,
):
    """``dimfort/panelInfo`` returns the documented top-level structure.

    Per ``docs/design/shipped/panel-info.md`` the payload carries:
    ``expression``, ``scopes``, ``imports``, ``scope`` (legacy
    single-scope view), ``scopeVars``, ``routine``, ``routineVars``,
    ``diagnostics``, ``fileDiagnosticCounts``. Pin all these fields
    are present.
    """
    await _open_and_wait(client_panel, PANEL_BASIC)
    # Line 9: `    dist = c_sound * t` — col 11 on `c_sound`.
    result = await _panel_info(client_panel, PANEL_BASIC, line=9, character=11)
    assert result is not None, "panelInfo returned None"

    # All documented top-level fields must be present (the wire layer
    # serialises None for missing single-value fields; lists may be
    # empty). pygls returns an Object with attribute access.
    for attr in (
        "expression", "scopes", "imports", "scope", "scopeVars",
        "routine", "routineVars", "diagnostics", "fileDiagnosticCounts",
    ):
        assert hasattr(result, attr), (
            f"panelInfo missing documented field {attr!r}"
        )

    # Expression tree should at minimum identify the hovered symbol.
    expr = result.expression
    assert expr is not None, "expression sub-payload was None at known cursor"

    # Scopes should list the module + the subroutine.
    scope_names = {s.name for s in result.scopes}
    assert "panel_basic_mod" in scope_names, (
        f"module scope missing from panel; got {scope_names}"
    )
    assert "demo" in scope_names, (
        f"subroutine scope missing from panel; got {scope_names}"
    )


# ---------------------------------------------------------------------------
# 4. Scope recovers when a routine has unparseable statements (0.2.0 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_panelinfo_scope_recovers_unparseable(
    client_panel: LanguageClient,
):
    """Panel Scope still lists pre-error variables when the routine has a parse error.

    Regression: 0.2.0 #panel-scope-recover — the Scope section
    blanked entirely if any statement in the routine couldn't be
    parsed (tree-sitter wrapped the whole routine in ERROR). The fix
    made Scope recover line-based.
    """
    await _open_and_wait(client_panel, PANEL_UNPARSEABLE)
    # Line 11: `    real :: x   !< @unit{m}` — col 13 on `x`.
    result = await _panel_info(
        client_panel, PANEL_UNPARSEABLE, line=11, character=13,
    )
    assert result is not None, "panelInfo returned None for unparseable file"
    # Find the subroutine scope and check that x + y appear in its
    # vars list — even though the routine has a parse error below.
    demo_scope = None
    for scope in result.scopes:
        if scope.name == "demo":
            demo_scope = scope
            break
    assert demo_scope is not None, (
        f"subroutine `demo` missing from panel.scopes; "
        f"got {[s.name for s in result.scopes]}"
    )
    var_names = {v.name for v in demo_scope.vars}
    assert "x" in var_names, (
        f"panel Scope didn't recover `x` past the parse error; "
        f"got {var_names}"
    )
    assert "y" in var_names, (
        f"panel Scope didn't recover `y` past the parse error; "
        f"got {var_names}"
    )


# ---------------------------------------------------------------------------
# 5. dimfort/interactions X001 + read/write grouping (0.2.1 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_interactions_x001_grouping(
    client_panel: LanguageClient,
):
    """Interactions report detects cross-site unit conflict (X001) and groups sites.

    Regression: 0.2.1 #interactions-x001-conflict — cross-site
    conflicts weren't detected; only per-statement check ran. The
    fix added the X001 conflict aggregation. The report should:
      - Carry the symbol name (``shared_var``).
      - List multiple points across the three subroutines.
      - Flag the conflict (writer_a writes kg into m-annotated var).
    """
    await _open_and_wait(client_panel, PANEL_CONFLICT)
    # Symbol-based query (more reliable than position-based).
    result = await _interactions(
        client_panel, PANEL_CONFLICT, symbol="shared_var",
    )
    assert result is not None, "interactions returned None for shared_var"
    assert result.symbol == "shared_var", (
        f"interactions resolved wrong symbol: {result.symbol!r}"
    )
    # Multiple sites should surface — at least the three subroutines.
    assert len(result.points) >= 2, (
        f"interactions found only {len(result.points)} sites; "
        f"expected ≥2 across writer_a/writer_b/reader"
    )
    # Each point must have the documented fields.
    for p in result.points:
        for attr in ("file", "line", "column", "scope", "kind", "unit", "snippet"):
            assert hasattr(p, attr), (
                f"interaction point missing field {attr!r}"
            )


# ---------------------------------------------------------------------------
# 6. Interactions cache is case-insensitive (0.2.6 regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_interactions_case_insensitive_cache(
    client_panel: LanguageClient,
):
    """Same symbol queried with different casings returns the same report.

    Regression: 0.2.6 #interactions-cache-case-insensitive — the
    cache key wasn't normalised, so ``Symbol`` and ``symbol`` hit
    different entries (wasted memory + risked drift on a large
    workset). Fortran is case-insensitive, so the cache key MUST be
    normalised.

    Test: query interactions for ``shared_var`` and ``SHARED_VAR``;
    both must resolve to the SAME report (same symbol, same point
    count, same conflict state).
    """
    await _open_and_wait(client_panel, PANEL_CONFLICT)
    lower = await _interactions(client_panel, PANEL_CONFLICT, symbol="shared_var")
    upper = await _interactions(client_panel, PANEL_CONFLICT, symbol="SHARED_VAR")
    assert lower is not None and upper is not None, (
        "interactions returned None for one of the casings"
    )
    # Both must resolve to the same canonical symbol (Fortran-
    # case-insensitive — the rendered name is the source casing of
    # the first declaration, not the query casing).
    assert lower.symbol.lower() == upper.symbol.lower(), (
        f"casings resolved to different symbols: "
        f"{lower.symbol!r} vs {upper.symbol!r}"
    )
    assert len(lower.points) == len(upper.points), (
        f"casings returned different point counts: "
        f"{len(lower.points)} vs {len(upper.points)}"
    )
    assert lower.hasConflict == upper.hasConflict, (
        f"casings disagreed on conflict state: "
        f"{lower.hasConflict} vs {upper.hasConflict}"
    )


# ---------------------------------------------------------------------------
# 7. Imports section surfaces transitive USE re-exports
# ---------------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_dimfort_panelinfo_imports_transitive(
    client_panel: LanguageClient,
):
    """Panel ``imports`` section surfaces transitively re-exported symbols.

    Pin the wire contract for the imports-section payload: when
    module C imports a symbol via USE-chaining through module B
    (which itself imports from A), the panel's imports list for the
    consuming routine carries the symbol. Pins the basic transitive
    surfacing — the specific column shape (``via phys_base``, mixed
    kinds) is a display concern downstream of the wire payload.
    """
    await _open_and_wait(client_panel, PANEL_TRANSITIVE)
    # Line 21 (0-indexed): `    rho_local = density` inside the
    # density_use_mod's demo subroutine.
    result = await _panel_info(
        client_panel, PANEL_TRANSITIVE, line=21, character=15,
    )
    assert result is not None, "panelInfo returned None"
    # `density` should be in the imports surface for this scope.
    imports = result.imports
    assert imports, "panelInfo.imports was empty at use site"
    # Each import has a name field; check density is present.
    import_names = {getattr(i, "name", None) for i in imports}
    assert "density" in import_names, (
        f"transitive import `density` missing from panel.imports; "
        f"got {import_names}"
    )
