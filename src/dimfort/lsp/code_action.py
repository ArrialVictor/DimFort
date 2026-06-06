"""Code-action provider for the LSP server.

Three quick-fixes:

- **Add `@unit{}`** on any declaration in range that has no
  annotation yet — delegated to the editor extension via a command so
  the client can position the cursor and prompt for the unit text.
- **Extract literal to a named PARAMETER** for each H010 (D1.5)
  implicit-cast diagnostic in range — also delegated via a command so
  the client can prompt for the PARAMETER's name.
- **Replace with <suggestion>** for U002 diagnostics that carry a
  ``suggested_rewrite`` — applied directly as a ``WorkspaceEdit`` (no
  client round-trip; nothing to prompt for).

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
    """Resolve LSP ``textDocument/codeAction`` requests into quick-fixes.

    The entry point for the LSP server's code-action feature.
    Builds three families of quick-fix in order: add ``@unit{}``
    on every unannotated declaration overlapping the request range;
    extract-literal-to-PARAMETER for each H010 (D1.5) implicit-cast
    diagnostic in the request range; and U002 suggested-rewrite
    replacements when the diagnostic carries a parsed candidate.

    The first two families dispatch through the editor extension
    (via custom ``dimfort.*`` commands) so the client can prompt the
    user for the unit string or the parameter name before applying
    the edit. The U002 rewrite is applied directly as a
    ``WorkspaceEdit`` — no client round-trip.

    Args:
        ls: The active ``LanguageServer`` instance, used to look up
            the open text document for the requested URI.
        params: The LSP request parameters, carrying the
            ``text_document`` URI, the request ``range``, and the
            ``context.diagnostics`` overlapping the selection.

    Returns:
        A list of ``lsp.CodeAction`` objects, or ``None`` when no
        actions can be offered (cached result missing, attachment
        missing, document not openable, no overlapping decls or
        diagnostics).
    """
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
    """Build "Replace with <suggestion>" actions for U002 diagnostics.

    Each U002 diagnostic whose ``data["suggested_rewrite"]`` carries
    a parsed rewrite candidate becomes a single ``WorkspaceEdit``
    code action. The diagnostic's range covers the whole directive
    token (e.g. ``@unit{m2/s}``); the edit replaces only the inner
    captured text — the substring between the first ``{`` and the
    matching ``}`` inside the range — so the directive delimiters
    themselves stay intact.

    Diagnostics are skipped when: the code isn't U002; ``data``
    isn't a dict; ``suggested_rewrite`` is missing or non-string;
    the range spans multiple lines (defensive — the directive sits
    on one line); the source line index is out of range; or the
    open/close braces can't be located inside the range.

    Args:
        params: The full code-action request parameters; we read
            ``params.context.diagnostics`` and the text-document
            URI from it.
        doc: The open text document, queried for source lines.

    Returns:
        A list of ``lsp.CodeAction`` quick-fixes, one per usable
        U002 diagnostic. Empty when no diagnostics qualify.
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
    """Build "Extract literal to PARAMETER" actions for H010 D1.5 fires.

    Walks the H010 diagnostics in ``params.context.diagnostics``,
    keeps those whose message matches the D1.5 implicit-cast shape
    (literal-to-target-unit), and emits one quick-fix per match.
    Each action edits two places: the literal use-site is replaced
    with a generated PARAMETER name, and a ``REAL, PARAMETER ::
    <name> = <literal>   !< @unit{<target>}`` declaration is
    inserted at the end of the enclosing routine's declaration
    block — chosen so the new symbol is visible to the executable
    section under ``IMPLICIT NONE``.

    The action is dispatched through the
    ``dimfort.extractToParameter`` editor command so the client can
    prompt the user (via ``showInputBox``) for the PARAMETER name
    before applying both edits in one undo step. The default name
    encodes the diagnostic line so successive applications produce
    distinct names.

    Args:
        params: The full code-action request parameters; carries
            the diagnostics list and the text-document URI.
        doc: The open text document, queried for sibling
            indentation at the insertion line.
        resolved_path: Resolved filesystem path of the document,
            kept on the signature for parity with attachment
            lookups (currently unused by the body — the routine
            location comes from the live tree-sitter tree).

    Returns:
        A list of ``lsp.CodeAction`` quick-fixes, one per H010
        D1.5 diagnostic with a recoverable enclosing routine and
        insertion line. Empty when no diagnostics qualify.
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
    """Return the innermost routine node enclosing a 1-based position.

    Walks every ``subroutine`` and ``function`` node in the tree,
    keeps those whose 1-based span encloses the cursor, and picks
    the one with the smallest byte length (the most deeply nested
    match wins).

    Args:
        tree: Parsed tree-sitter ``Tree`` for the current document.
        line_1based: 1-based line number of the position.
        col_1based: 1-based column number of the position.

    Returns:
        The innermost ``subroutine`` / ``function`` ``Node``
        enclosing the position, or ``None`` when the position sits
        outside every routine (file-level or module-level code).
    """
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
    """Pick the insertion line for a generated PARAMETER declaration.

    Returns the **1-based** line index immediately after the last
    ``variable_declaration`` direct child of ``routine`` — i.e. the
    line on which the new declaration should be written so it lands
    flush with the routine's existing decl block. Callers index the
    document's ``lines`` array as ``doc.lines[insert_line - 1]``
    when fetching sibling indentation, which encodes the 1-based
    convention explicitly.

    When the routine has no ``variable_declaration`` children
    (empty routine, or one whose declarations failed to parse), the
    helper falls back to the line right after the routine's
    ``*_statement`` header. When neither anchor is locatable it
    returns ``None`` and the caller skips the action.

    Args:
        routine: A ``subroutine`` or ``function`` definition
            ``Node``.
        source: Raw source bytes (kept on the signature for
            consistency with other helpers; the body relies only on
            node spans).

    Returns:
        The 1-based line index where a new declaration should be
        inserted, or ``None`` when neither a declaration nor a
        header anchor is found.

    Note:
        ``end_position_for`` already shifts tree-sitter's 0-based
        ``end_point`` to 1-based, so no further conversion is
        needed here. A historical comment on this function used to
        claim the offset came from "the end_point includes the
        trailing newline" — wrong reason for a value that turned
        out to be correct.
    """
    last_decl_line = None
    header_line = None
    for c in routine.children:
        if c.type in ("subroutine_statement", "function_statement"):
            header_line = _ts.end_position_for(c).line
        elif c.type == "variable_declaration":
            last_decl_line = _ts.end_position_for(c).line
    return last_decl_line if last_decl_line is not None else header_line


def _comment_column(line: str) -> int | None:
    """Find the column where a Fortran ``!`` comment starts on a line.

    Walks the line character-by-character tracking whether we are
    inside a single- or double-quoted string literal so a ``!``
    inside a string does not trigger a false positive. Doubled
    quotes inside a string literal (``''`` / ``""``) are recognised
    as the Fortran escape form and do not close the string.

    Used by the "Add ``@unit{}``" code action to splice the
    snippet before any existing trailing comment instead of after
    it, which would otherwise commit the new directive into the
    comment text.

    Args:
        line: A single source line, without trailing newline.

    Returns:
        The 0-based column index of the first ``!`` that starts a
        comment, or ``None`` when the line has no comment.
    """
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
