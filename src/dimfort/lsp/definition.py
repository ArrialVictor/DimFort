"""Go-to-definition resolution for the LSP server.

Resolves the identifier, call-callee, or ``use`` module under the cursor to
its declaration site(s). Reads the workset-wide name index
(``result.symbols_by_name_lc``) built by
:mod:`dimfort.lsp.symbols_index` after every ``check_files`` pass,
filters candidates by the current file's ``use`` clauses + same-file
declarations, and returns one or more :class:`lsp.Location` records.

F90's case-insensitive name resolution is a lower-cased compare on both
ends. Multiple matches return as a list; the editor picks (VSCode shows
a picker; nvim/Emacs route to a quickfix list).

``server.py`` registers the LSP feature and — holding
``state.ts_handler_lock`` — delegates the traversal here. The handler
only walks the live buffer's tree to identify the cursor target; the
candidate enumeration is now O(1) dict lookup.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from lsprotocol import types as lsp

from dimfort.core import ts_parser as _ts
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _trees_for, _uri_for_path

if TYPE_CHECKING:
    from pathlib import Path

    from dimfort.core.multifile import SymbolEntry


def resolve(params: lsp.DefinitionParams) -> list[lsp.Location] | None:
    """Resolve the identifier under the cursor to its declaration site(s).

    Classifies the token under the cursor as a ``use`` module name, a
    call callee, or a plain identifier (in that priority order), then
    looks the name up in ``result.symbols_by_name_lc`` (built once per
    workspace check). Filters candidates by kind, then by visibility:
    same-file declarations and modules reachable via the current file's
    ``use`` clauses (per :class:`WorkspaceIndex`) win over global noise.

    Args:
        params: LSP ``DefinitionParams`` carrying the document URI and
            the cursor position.

    Returns:
        A list of :class:`lsp.Location` records. Single match for the
        common case; multi-match when the name appears in more than
        one visible scope (editor presents a picker). ``None`` when no
        tree is loaded for the URI, no workset result exists, no
        identifier sits under the cursor, or no matching declaration
        survives filtering.

    Note:
        Caller in ``server.py`` holds ``state.ts_handler_lock`` around
        this call. The cursor-target walk runs against the live tree;
        candidate enumeration is an O(1) dict lookup against the
        pre-built name index. When the cursor was on a "callable",
        ``var`` candidates also qualify because ``a(1)`` is
        syntactically ambiguous (array index vs function call) in
        Fortran.
    """
    found = _trees_for(params.text_document.uri)
    if found is None:
        return None
    current_path, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None

    line = params.position.line + 1
    col = params.position.character + 1

    # Identify the target. Order matters: the most specific node
    # type wins. ``use foo`` first (its module_name token isn't an
    # identifier or call-callee), then call-callees, then plain
    # identifiers.
    target_name: str | None = None
    target_kind: str | None = None  # "module", "var", or "callable"
    for use_node in _ts_h.walk_use_statements(tree):
        nm = _ts_h.use_statement_module_name(use_node, source)
        if nm is None:
            continue
        mod_name, mod_name_node = nm
        if _ts_h.node_contains(mod_name_node, line, col):
            target_name = mod_name
            target_kind = "module"
            break
    if target_name is None:
        for call in _ts_h.walk_calls(tree):
            name = _ts_h.call_name(call, source)
            if name is None:
                continue
            # Match only if the cursor is on the callee identifier
            # (not on an argument inside the call).
            for c in call.children:
                if c.type == "identifier" and _ts_h.node_contains(c, line, col):
                    target_name = name
                    target_kind = "callable"
                    break
            if target_name:
                break
    if target_name is None:
        for ident in _ts_h.walk_identifiers(tree):
            if not _ts_h.node_contains(ident, line, col):
                continue
            if _ts_h.is_inside_type_qualifier(ident):
                continue
            target_name = _ts.node_text(ident, source)
            target_kind = "var"
            break
    if target_name is None:
        return None
    target_lc = target_name.lower()

    # Index lookup — O(1). Empty tuple when nothing matches.
    candidates = result.symbols_by_name_lc.get(target_lc, ())
    if not candidates:
        return None

    # Filter by kind. Callable falls through to var because ``a(1)``
    # could be either; var only matches var (the cursor walk above
    # already ruled out a call-syntax context).
    if target_kind == "module":
        kind_filter = {"module"}
    elif target_kind == "callable":
        kind_filter = {"callable", "var"}
    else:
        kind_filter = {"var"}
    candidates = tuple(c for c in candidates if c.kind in kind_filter)
    if not candidates:
        return None

    # Visibility filter: prefer same-file declarations and any file
    # that declares a module the current file imports via ``use``.
    # Falls back to all candidates when the filter would empty the set
    # (e.g. workspace index not yet built, or no matching declaring file).
    visible_files = _visible_files(current_path)
    if visible_files is not None:
        narrowed = tuple(
            c for c in candidates
            if c.file == current_path or c.file in visible_files
        )
        if narrowed:
            candidates = narrowed

    return [_entry_location(entry) for entry in candidates]


def _visible_files(current_path: Path) -> frozenset[Path] | None:
    """Return the set of files reachable from ``current_path`` via ``use`` clauses.

    Returns ``None`` when the workspace index hasn't been built or has
    no entry for the current file — caller treats that as "no filter".
    Reads are guarded by ``state.workspace_index_lock`` because
    :func:`update_index` mutates the dicts in place from the pipeline
    worker thread.
    """
    with state.workspace_index_lock:
        ws_index = state.workspace_index
        if ws_index is None:
            return None
        uses = ws_index.uses_by_file.get(current_path)
        if not uses:
            return None
        seen: set[Path] = set()
        for use_ref in uses:
            declaring = ws_index.modules.get(use_ref.module)
            if declaring is not None:
                seen.add(declaring)
    return frozenset(seen) if seen else None


def _entry_location(entry: SymbolEntry) -> lsp.Location:
    """Build an :class:`lsp.Location` from a pre-indexed declaration site."""
    return lsp.Location(
        uri=_uri_for_path(entry.file),
        range=lsp.Range(
            start=lsp.Position(line=entry.start_row, character=entry.start_col),
            end=lsp.Position(line=entry.end_row, character=entry.end_col),
        ),
    )
