"""Side-panel payload (``dimfort/panelInfo``).

Assembles the cursor-following panel response: the expression tree at the
cursor, one variable table per enclosing scope, the cursor-line diagnostics,
and whole-file counts. Stateless — reads the last cached ``WorksetResult`` and
computes on the fly. See docs/design/panel-info.md. ``server.py`` registers
the LSP feature and delegates here.

Like the interactions handler, this parses a *fresh* tree from the live buffer
for cursor→node mapping, so it does not need ``state.ts_handler_lock``.
"""
from __future__ import annotations

from typing import Any

from pygls.lsp.server import LanguageServer

from dimfort.core import ts_parser as _ts
from dimfort.core.diagnostics import Severity
from dimfort.lsp.decl_scan import _scan_declarations_for_uri
from dimfort.lsp.expr_tree import (
    _build_expression_tree,
    _build_scope_vars,
    build_scope_vars_by_span,
    recover_scopes,
)
from dimfort.lsp.imports import build_imports
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _build_ts_ctx, _trees_for, _uri_to_path
from dimfort.lsp.tree_nav import (
    _enclosing_scopes,
    _find_expression_root,
    _scope_header,
)


def resolve(ls: LanguageServer, params: Any) -> dict[str, Any] | None:
    # pygls passes custom-method params as a plain dict-like object.
    # Accept either attribute access (TypedDict-style) or dict access
    # so we don't depend on a specific wrapper class.
    def _get(obj: Any, key: str) -> Any:
        if hasattr(obj, key):
            return getattr(obj, key)
        if isinstance(obj, dict):
            return obj.get(key)
        return None

    text_document = _get(params, "textDocument") or _get(params, "text_document")
    position = _get(params, "position")
    if text_document is None or position is None:
        return None
    uri = _get(text_document, "uri")
    line = _get(position, "line")
    character = _get(position, "character")
    if uri is None or line is None or character is None:
        return None

    path = _uri_to_path(uri)
    if path is None:
        return None
    resolved = path.resolve()

    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None
    attached = result.attachments.get(resolved)
    if attached is None:
        return None

    # ``_trees_for`` just confirms a tree exists for this URI. We no
    # longer fall back to the cached Tree on parse failure (see the
    # comment below) — but we still want to bail when no tree has been
    # computed for the file yet, which is what ``_trees_for is None``
    # gates.
    if _trees_for(uri) is None:
        return None

    # Parse the LIVE buffer so scope detection + cursor→node mapping
    # track unsaved edits (a just-typed declaration, an inserted line).
    # The cross-file unit tables in ``ctx`` still come from the last
    # workspace check — those are name-keyed and don't shift with local
    # edits — but the *structure* we navigate must match what the user
    # sees on screen.
    #
    # On parse failure we **bail** rather than walking the cached
    # workspace tree: the cached Tree is shared across all read-side
    # LSP entry points (hover / definition / inlay), and tree-sitter
    # walks the same Node objects without the Python GIL across
    # concurrent traversal. Walking it here unsynchronised can crash
    # tree-sitter natively with no Python traceback (the "permanent
    # concurrency gotcha" recorded in the project notes). Bailing means
    # an unparseable buffer renders nothing for this surface — strictly
    # better than racing.
    try:
        doc = ls.workspace.get_text_document(uri)
        source_bytes = doc.source.encode("utf-8")
        tree = _ts.parse_text(source_bytes)
    except Exception:
        return None

    line_1based = int(line) + 1
    col_1based = int(character) + 1
    scope_nodes = _enclosing_scopes(tree, line_1based, col_1based)

    # Reuse the shared ctx builder so identifier-to-unit lookup behaves
    # exactly like every other hover / inlay path.
    ctx = _build_ts_ctx(result, source_bytes, str(resolved), path=resolved)

    # Find the smallest expression-bearing node at the cursor. If the
    # cursor sits on a declaration or other non-expression node, we
    # collapse to a single-node "tree" for the variable identifier
    # under the cursor (per the design decision: show declarations as
    # single-node trees rather than blanking the section).
    scan_decls = _scan_declarations_for_uri(ls, uri, resolved)
    unparseable = result.unparseable_units.get(resolved, frozenset())

    # Markers are diagnostic-driven (docs/design/markers.md): the marker
    # helpers read this file's diagnostics from state.last_result via ctx.file,
    # so no threading is needed here.
    expr_root = _find_expression_root(tree, line_1based, col_1based)
    expression = (
        _build_expression_tree(expr_root, ctx, source_bytes)
        if expr_root is not None
        else None
    )

    # One section per enclosing scope, outermost first. Each carries
    # the scope header fields (name, kind) plus its own ``vars`` list.
    scopes: list[dict[str, Any]] = []
    for sn in scope_nodes:
        header = _scope_header(sn, source_bytes)
        if header is None:
            continue
        scopes.append({
            **header,
            "vars": _build_scope_vars(
                sn, scan_decls, attached, source_bytes, unparseable,
                scale_mode=state.scale_mode,
                tree=tree, signatures=result.signatures,
            ),
        })

    # Fallback: tree-sitter found no scope node, which happens when an
    # unparseable statement collapses the whole routine into an ``ERROR``
    # node. Recover the enclosing scopes line-based from the surviving
    # header statements so the Scope section still lists the routine's
    # declarations instead of blanking. See docs/design/panel-info.md.
    if not scopes:
        recovered = recover_scopes(tree, source_bytes)
        chain = [
            idx for idx, (_k, _n, s, e) in enumerate(recovered)
            if s <= line_1based <= e
        ]
        chain.sort(key=lambda idx: (recovered[idx][2], -recovered[idx][3]))
        for idx in chain:
            kind, name, _s, _e = recovered[idx]
            scopes.append({
                "name": name,
                "kind": kind,
                "vars": build_scope_vars_by_span(
                    idx, recovered, scan_decls, attached,
                    source_bytes, unparseable,
                    scale_mode=state.scale_mode,
                    tree=tree, signatures=result.signatures,
                ),
            })

    # Innermost scope, surfaced as the back-compat ``scope`` /
    # ``scopeVars`` / ``routine`` / ``routineVars`` fields for any
    # consumer that only renders a single scope section.
    innermost = scopes[-1] if scopes else None
    innermost_header = (
        {"name": innermost["name"], "kind": innermost["kind"]}
        if innermost else None
    )
    innermost_vars = innermost["vars"] if innermost else []

    # Diagnostics on the cursor line — so the panel shows *why* a node is
    # marked without a hover/Problems trip. Scoped to the line (not the
    # whole file) to stay relevant and avoid duplicating Problems.
    file_diags = result.diagnostics.get(resolved, [])
    diagnostics = [
        {
            "severity": str(d.severity),  # "error"/"warning"/"info"/"hint"
            "code": d.code,
            "message": d.message,
            # 1-based start/end so a click can land on (and select) the
            # exact span, not the line start — the cursor is usually
            # already on the line.
            "line": d.start.line,
            "column": d.start.column,
            "endLine": d.end.line,
            "endColumn": d.end.column,
        }
        for d in file_diags
        if d.start.line <= line_1based <= d.end.line
    ]
    # Whole-file counts for a panel footer (a mini dashboard).
    # ``info`` / ``hint`` were previously dropped, but the panel is
    # designed to surface U020 (info) and U021 (hint) annotation-quality
    # signals — wire-format change is backward-compatible since
    # companions ignore unknown keys.
    file_diagnostic_counts = {
        "error": sum(1 for d in file_diags if d.severity == Severity.ERROR),
        "warning": sum(1 for d in file_diags if d.severity == Severity.WARNING),
        "info": sum(1 for d in file_diags if d.severity == Severity.INFO),
        "hint": sum(1 for d in file_diags if d.severity == Severity.HINT),
    }

    # Imported symbols visible at the cursor (``use`` clauses): names that
    # are usable here but not declared in an enclosing scope, so the scope
    # tables don't cover them. A local declaration *in the cursor's
    # enclosing scopes* shadows an import (it's already shown under Scope),
    # so exclude those — but not same-named declarations in sibling scopes
    # / other modules in the file, which don't shadow here.
    local_names_lc = frozenset(
        v["name"].lower() for sc in scopes for v in sc["vars"]
    )
    imports = build_imports(
        tree, source_bytes, line_1based, result, local_names_lc,
        scale_mode=state.scale_mode,
    )

    return {
        "expression": expression,
        "scopes": scopes,
        "imports": imports,
        "scope": innermost_header,
        "scopeVars": innermost_vars,
        "routine": innermost_header,
        "routineVars": innermost_vars,
        "diagnostics": diagnostics,
        "fileDiagnosticCounts": file_diagnostic_counts,
    }
