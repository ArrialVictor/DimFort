"""Code-action provider for the LSP server.

Two quick-fixes, both delegated to the editor extension via a command so the
client can position the cursor / prompt for input:

- **Add `@unit{}`** on any declaration in range that has no annotation yet.
- **Extract literal to a named PARAMETER** for each H010 (D1.5) implicit-cast
  diagnostic in range.

``server.py`` registers the LSP feature and delegates here.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer
from tree_sitter import Node, Tree

from dimfort.core import ts_parser as _ts
from dimfort.lsp.decl_scan import _last_scan_declarations
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _trees_for, _uri_to_path


def resolve(ls: LanguageServer, params: lsp.CodeActionParams) -> list[lsp.CodeAction] | None:
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None
    path = _uri_to_path(params.text_document.uri)
    if path is None:
        return None
    resolved = path.resolve()
    attached = result.attachments.get(resolved)
    if attached is None:
        return None
    try:
        doc = ls.workspace.get_text_document(params.text_document.uri)
    except Exception:
        return None

    # Decide which DeclarationSites overlap the cursor / selection.
    selection_start = params.range.start.line + 1
    selection_end = params.range.end.line + 1
    actions: list[lsp.CodeAction] = []
    # Reach into the ScanResult to know which decls have no annotation
    # yet. attach.AttachmentResult doesn't track this directly, so we
    # diff: any declaration whose names aren't all in var_units|field_units.
    scan_decls = _last_scan_declarations(path)
    if scan_decls is None:
        return None
    for decl in scan_decls:
        if decl.line_end < selection_start or decl.line_start > selection_end:
            continue
        any_annotated = False
        if decl.enclosing_type is not None:
            any_annotated = any(
                (decl.enclosing_type, name) in attached.field_units
                for name in decl.names
            )
        else:
            any_annotated = any(name in attached.var_units for name in decl.names)
        if any_annotated:
            continue
        # Build the edit: append ` !< @unit{}` at end of the declaration's
        # first source line.
        target_line_idx = decl.line_start - 1
        if target_line_idx >= len(doc.lines):
            continue
        line = doc.lines[target_line_idx].rstrip("\n").rstrip("\r")
        # If the line already has a `!` comment, splice before it; else
        # append at end-of-line.
        comment_col = _comment_column(line)
        insert_col = comment_col if comment_col is not None else len(line)
        # Use a command (handled by the VSCode extension) so the cursor
        # lands inside the braces ready for typing. Plain LSP TextEdits
        # can't position the cursor; non-VSCode clients that don't have
        # the `dimfort.insertSnippet` command registered would see this
        # action as a no-op — acceptable for v1.
        snippet = "  !< @unit{$0}"
        action = lsp.CodeAction(
            title=f"DimFort: Add @unit{{}} to {', '.join(decl.names)}",
            kind=lsp.CodeActionKind.QuickFix,
            command=lsp.Command(
                title="DimFort: insert @unit{} snippet",
                command="dimfort.insertSnippet",
                arguments=[
                    params.text_document.uri,
                    target_line_idx,
                    insert_col,
                    snippet,
                ],
            ),
        )
        actions.append(action)

    # D1.5 quick action — "Extract literal to a named PARAMETER".
    # Reads the H010 diagnostics in the requested range and offers a
    # one-click refactor that lifts the offending literal into a typed
    # PARAMETER declaration.
    actions.extend(
        _h010_extract_to_parameter_actions(params, doc, resolved)
    )
    # U002 suggested-rewrite — "Replace `<old>` with `<new>`" applied
    # as a direct workspace edit. The diagnostic carries the
    # suggestion in ``data["suggested_rewrite"]`` (set by
    # ``server._to_lsp_diagnostic``); we compute the inner-text range
    # by finding the open/close delimiters inside the diagnostic's
    # token span on the source line.
    actions.extend(
        _u002_rewrite_actions(params, doc)
    )
    return actions or None


# group 1: the literal text; group 2: the *display* target unit (pretty
# ``·``/superscript form, shown in the action title); group 3: the
# *parseable* target unit pulled from the message's ``@unit{...}`` example
# (ASCII form, written back into source — the pretty form does not parse).
_H010_CAST_RE = re.compile(
    r"^Implicit cast: literal '([^']+)' to (.+?) \(prefer.*@unit\{(.+?)\}"
)


def _u002_rewrite_actions(
    params: lsp.CodeActionParams, doc: Any,
) -> list[lsp.CodeAction]:
    """Build "Replace with `<suggestion>`" actions for U002
    diagnostics whose payload includes a parsed rewrite candidate.

    The diagnostic's range covers the directive token (e.g.
    ``@unit{m2/s}``); we replace just the inner captured text — the
    substring between the first ``{`` and the matching ``}`` within
    the range — so the directive itself stays intact.
    """
    out: list[lsp.CodeAction] = []
    diagnostics = params.context.diagnostics or []
    for diag in diagnostics:
        if diag.code != "U002":
            continue
        data = getattr(diag, "data", None)
        if not isinstance(data, dict):
            continue
        suggestion = data.get("suggested_rewrite")
        if not isinstance(suggestion, str) or not suggestion:
            continue
        rng = diag.range
        if rng.start.line != rng.end.line:
            continue
        line_idx = rng.start.line
        if line_idx >= len(doc.lines):
            continue
        line = doc.lines[line_idx].rstrip("\n").rstrip("\r")
        open_at = line.find("{", rng.start.character)
        if open_at == -1 or open_at >= rng.end.character:
            continue
        close_at = line.find("}", open_at + 1)
        if close_at == -1 or close_at > rng.end.character:
            continue
        edit = lsp.TextEdit(
            range=lsp.Range(
                start=lsp.Position(line=line_idx, character=open_at + 1),
                end=lsp.Position(line=line_idx, character=close_at),
            ),
            new_text=suggestion,
        )
        action = lsp.CodeAction(
            title=f"DimFort: Replace with {suggestion!r}",
            kind=lsp.CodeActionKind.QuickFix,
            diagnostics=[diag],
            edit=lsp.WorkspaceEdit(
                changes={params.text_document.uri: [edit]},
            ),
            is_preferred=True,
        )
        out.append(action)
    return out


def _h010_extract_to_parameter_actions(
    params: lsp.CodeActionParams, doc: Any, resolved_path: Path,
) -> list[lsp.CodeAction]:
    """Build the 'extract literal to PARAMETER' action for each H010 D1.5
    diagnostic in the requested range.

    The action edits two places: the literal use-site is replaced with
    a generated parameter name, and a ``REAL, PARAMETER :: <name> =
    <literal>   !< @unit{<target>}`` declaration is inserted at the
    end of the enclosing routine's declaration block so the new symbol
    is visible to the executable section under ``IMPLICIT NONE``.
    """
    out: list[lsp.CodeAction] = []
    diagnostics = params.context.diagnostics or []
    for diag in diagnostics:
        if diag.code != "H010":
            continue
        m = _H010_CAST_RE.match(diag.message)
        if m is None:
            continue  # D1.6 untag — separate action below if/when added
        literal_text = m.group(1)
        target_unit_display = m.group(2)  # pretty, for the action title
        target_unit = m.group(3)          # parseable, written into @unit{}
        # Locate the enclosing routine via tree-sitter so the new
        # PARAMETER declaration lands in a syntactically valid spot.
        found = _trees_for(params.text_document.uri)
        if found is None:
            continue
        _path, tree, source_bytes = found
        line_1based = diag.range.start.line + 1
        col_1based = diag.range.start.character + 1
        routine = _smallest_enclosing_routine(tree, line_1based, col_1based)
        if routine is None:
            continue
        insert_line = _routine_decl_insertion_line(routine, source_bytes)
        if insert_line is None:
            continue
        if insert_line >= len(doc.lines):
            continue
        # Match the indent of the row we're inserting before so the
        # declaration sits flush with sibling decls.
        sibling_line = doc.lines[insert_line - 1] if insert_line > 0 else ""
        indent = sibling_line[: len(sibling_line) - len(sibling_line.lstrip())]
        if not indent:
            indent = "  "
        # Suggested default name — the extension shows this in the
        # input box; the user can accept or rewrite before applying.
        default_name = f"c_h010_{diag.range.start.line + 1}"
        action = lsp.CodeAction(
            title=(
                f"DimFort: Extract literal {literal_text!r} into a named "
                f"PARAMETER ({target_unit_display})"
            ),
            kind=lsp.CodeActionKind.QuickFix,
            diagnostics=[diag],
            # Delegate to the extension so it can prompt the user for
            # the parameter name with showInputBox before applying the
            # two-edit refactor. Non-VSCode clients that don't have the
            # command registered see this action as a no-op.
            command=lsp.Command(
                title="DimFort: extract literal to PARAMETER",
                command="dimfort.extractToParameter",
                arguments=[
                    params.text_document.uri,
                    {
                        "line": diag.range.start.line,
                        "character": diag.range.start.character,
                    },
                    {
                        "line": diag.range.end.line,
                        "character": diag.range.end.character,
                    },
                    insert_line,
                    indent,
                    literal_text,
                    target_unit,
                    default_name,
                ],
            ),
        )
        out.append(action)
    return out


def _smallest_enclosing_routine(tree: Tree, line_1based: int, col_1based: int) -> Node | None:
    """Return the innermost ``subroutine`` / ``function`` node enclosing
    the position, or ``None`` if the position isn't inside any routine
    (file-level / module-level code)."""
    best = None
    best_size = None
    for n in _ts.walk(tree.root_node):
        if n.type not in ("subroutine", "function"):
            continue
        sp = _ts.position_for(n)
        ep = _ts.end_position_for(n)
        if (sp.line, sp.column) <= (line_1based, col_1based) <= (ep.line, ep.column):
            size = n.end_byte - n.start_byte
            if best_size is None or size < best_size:
                best, best_size = n, size
    return best


def _routine_decl_insertion_line(routine: Node, source: bytes) -> int | None:
    """Return the 0-based line index right after the last
    ``variable_declaration`` direct child of ``routine``.

    Fallback: the line after the routine's ``*_statement`` header. None
    if neither is locatable.
    """
    last_decl_line = None
    header_line = None
    for c in routine.children:
        if c.type in ("subroutine_statement", "function_statement"):
            header_line = _ts.end_position_for(c).line
        elif c.type == "variable_declaration":
            last_decl_line = _ts.end_position_for(c).line
    target_1based = last_decl_line if last_decl_line is not None else header_line
    if target_1based is None:
        return None
    # tree-sitter's end_point includes the trailing newline; convert to
    # 0-based and add one so the insertion lands on the next line.
    return target_1based


def _comment_column(line: str) -> int | None:
    """Find the column where the line's `!` comment starts, or None."""
    in_quote: str | None = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_quote is None:
            if c == "!":
                return i
            if c in ("'", '"'):
                in_quote = c
        else:
            if c == in_quote:
                if i + 1 < len(line) and line[i + 1] == in_quote:
                    i += 1
                else:
                    in_quote = None
        i += 1
    return None
