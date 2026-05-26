"""Hover rendering for the DimFort language server.

Computes the ``textDocument/hover`` payload for the cursor position: the
resolved unit of a variable / derived-type member / call, or the unit-algebra
breakdown of an enclosing expression. ``server.py`` registers the LSP feature
and — holding ``state.ts_handler_lock`` — delegates here via :func:`resolve`,
passing the current hover verbosity (``"short"`` / ``"detailed"``) so this
module never reaches back into ``server`` for the ``_features`` toggle.

Dispatch is most-specific-wins: :func:`_resolve_hover` handles the precise
surfaces (use-statement, function header, member access, call callee, bare
identifier, numeric literal); when none match, :func:`_expression_hover_for`
renders the enclosing assignment / relational / sub-expression. The ``short``
renderers emit a one-line summary; ``detailed`` emits the ASCII unit-algebra
tree built by :func:`_render_ast_tree`. All markers are diagnostic-driven via
``expr_tree._node_marker`` (see docs/design/markers.md).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from lsprotocol import types as lsp
from tree_sitter import Node, Tree

from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.expr_tree import _node_marker
from dimfort.lsp.hover_render import (
    _hover_signature,
    _hover_text,
    _module_hover_md,
    _unit_pretty,
)
from dimfort.lsp.markers import _aggregate_marker
from dimfort.lsp.state import state
from dimfort.lsp.tree_access import _build_ts_ctx, _trees_for
from dimfort.lsp.tree_nav import (
    _SKIP_TOKEN_TYPES,
    _interesting_children,
    _node_label,
    _node_lsp_range,
)

if TYPE_CHECKING:
    from dimfort.core.multifile import WorksetResult
    from dimfort.core.symbols import FuncSig
    from dimfort.core.units import UnitExpr

# A rendered unit-algebra tree row: (label, unit-or-None, marker, rule-tag).
# ``unit`` is ``None`` only for the synthetic assignment root row (a statement,
# which has no unit of its own).
_TreeRow = tuple[str, str | None, str, str]


def resolve(
    uri: str,
    line_1based: int,
    col_1based: int,
    source_text: str | None,
    *,
    hover_mode: str,
) -> tuple[str, lsp.Range] | None:
    """Resolve the hover at ``(line, col)``: specific surfaces first, then
    the expression-context fallback. ``hover_mode`` is the live verbosity
    (``"short"`` / ``"detailed"``) the server read off its ``_features``.
    """
    hit = _resolve_hover(uri, line_1based, col_1based, source_text, hover_mode=hover_mode)
    if hit is None:
        hit = _expression_hover_for(uri, line_1based, col_1based, hover_mode=hover_mode)
    return hit


def _resolve_hover(
    uri: str,
    line_1based: int,
    col_1based: int,
    source_text: str | None,  # accepted for caller compatibility; unused
    *,
    hover_mode: str = "short",
) -> tuple[str, lsp.Range] | None:
    """Return ``(markdown_text, range)`` for the hover at ``(line, col)``.

    Returning the range alongside the text is what lets VSCode display
    the "Go to Definition" / "Peek" affordances at the bottom of the
    hover popup. Without it, VSCode doesn't know which symbol the
    hover is for and suppresses those links.

    Dispatch order, tightest-fit wins inside each category:

    1. **Function/Subroutine definition header** — the cursor is on the
       ``name`` token of a function or subroutine declaration.
    2. **Derived-type member access** (``a%b``) — show the field's unit.
    3. **Call expression / subroutine call** — show the callee's signature.
    4. **Plain identifier** — variable reference; show its unit.

    Less specific matches (assignment LHS/RHS hovers, BinOp hovers
    showing the resolved expression unit) used to live here on the
    LFortran-AST path. They are intentionally not ported in this pass:
    they degrade gracefully (no hover at that exact position) and the
    diagnostic-driven information is unchanged.
    """
    found = _trees_for(uri)
    if found is None:
        return None
    resolved_path, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None

    # 0. ``use foo`` — cursor on the module-name token of a use
    # statement renders a module summary (exports + signatures).
    # Sits before the function-header branch because a `use` line
    # never overlaps a definition header.
    for use_node in _ts_h.walk_use_statements(tree):
        nm = _ts_h.use_statement_module_name(use_node, source)
        if nm is None:
            continue
        mod_name, mod_name_node = nm
        if not _ts_h.node_contains(mod_name_node, line_1based, col_1based):
            continue
        mod_lc = mod_name.lower()
        exports = result.module_exports.get(mod_lc)
        external = mod_lc in state.external_modules
        return (
            _module_hover_md(
                mod_name, exports,
                external=external,
                unresolved=exports is None and not external,
            ),
            _node_lsp_range(mod_name_node),
        )

    # 1. Function / subroutine definition header on this line.
    for func_or_sub in _ts_h.walk_function_definitions(tree):
        if _ts_h.function_definition_header_line(func_or_sub) != line_1based:
            continue
        nm = _ts_h.function_definition_name(func_or_sub, source)
        if nm is None:
            continue
        name, name_node = nm
        if not _ts_h.node_contains(name_node, line_1based, col_1based):
            continue
        sig = result.signatures.get(name.lower())
        if sig is None:
            continue
        return _hover_signature(name, sig), _node_lsp_range(name_node)

    # 2. Derived-type member access — tightest enclosing wins so the
    #    innermost ``a%b`` in ``a%b%c`` doesn't shadow the outer.
    member_hit = _ts_h.smallest_enclosing(
        _ts_h.walk_member_exprs(tree), line_1based, col_1based
    )
    if member_hit is not None:
        ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
        ctx.var_types.update(ts_checker.collect_var_types(tree, source))
        ctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
        ctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))
        unit = ts_checker.resolve_member_chain(member_hit, ctx, source)
        base, path = _ts_h.member_expr_chain(member_hit, source)
        if base is not None and path:
            display = f"{base}%{'%'.join(path)}"
            return _hover_text(display, _unit_pretty(unit)), _node_lsp_range(member_hit)

    # 3. Call expression / subroutine call.
    call_hit = _ts_h.smallest_enclosing(
        _ts_h.walk_calls(tree), line_1based, col_1based
    )
    if call_hit is not None:
        callee_nm = _ts_h.call_name(call_hit, source)
        if callee_nm is not None:
            sig = result.signatures.get(callee_nm.lower())
            if sig is not None:
                # Range the callee identifier specifically so the
                # "Go to Definition" link targets the callable name,
                # not the whole call expression including its args.
                callee = next(
                    (c for c in call_hit.children if c.type == "identifier"),
                    call_hit,
                )
                # Only fire the call-pairing hover when the cursor is
                # actually on the callee identifier — hovering on an
                # arg expression should fall through to that arg's
                # own hover (or the trace path).
                if _ts_h.node_contains(callee, line_1based, col_1based):
                    level = hover_mode
                    rctx = _build_ts_ctx(
                        result, source, str(resolved_path), path=resolved_path,
                    )
                    rctx.var_types.update(ts_checker.collect_var_types(tree, source))
                    rctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
                    rctx.type_field_types.update(
                        ts_checker.collect_type_field_types(tree, source)
                    )
                    if level == "detailed":
                        text = _render_call_pairing_c(
                            callee_nm, call_hit, sig, rctx, source,
                        )
                    else:
                        text = _render_call_pairing_a(
                            callee_nm, call_hit, sig, rctx, source,
                        )
                    if text is None:
                        text = _hover_signature(callee_nm, sig)
                    return text, _node_lsp_range(callee)
            # No user-defined signature — but the call might be a known
            # Fortran intrinsic (log, exp, sqrt, sin, sum, ...). Show
            # the resolved result unit instead of falling through to the
            # bare-identifier path which would say "no annotation".
            from dimfort.core.symbols import (
                DIMENSIONLESS_INTRINSICS,
                EXP_INTRINSICS,
                LOG_INTRINSICS,
                PRODUCT_INTRINSICS,
                REDUCTION_INTRINSICS,
                SAME_UNIT_ARG_INTRINSICS,
                TRANSFORMING_INTRINSICS,
                TRANSPARENT_INTRINSICS,
            )
            name_lc = callee_nm.lower()
            is_known_intrinsic = (
                name_lc in DIMENSIONLESS_INTRINSICS
                or name_lc in EXP_INTRINSICS
                or name_lc in LOG_INTRINSICS
                or name_lc in TRANSFORMING_INTRINSICS
                or name_lc in TRANSPARENT_INTRINSICS
                or name_lc in SAME_UNIT_ARG_INTRINSICS
                or name_lc in PRODUCT_INTRINSICS
                or name_lc in REDUCTION_INTRINSICS
            )
            if is_known_intrinsic:
                callee = next(
                    (c for c in call_hit.children if c.type == "identifier"),
                    call_hit,
                )
                ctx = _build_ts_ctx(
                    result, source, str(resolved_path), path=resolved_path,
                )
                unit = ts_checker.resolve_unit(call_hit, ctx, source)
                # Show the full source text of the call rather than
                # `name(...)` — the user sees the exact expression
                # whose unit is being reported.
                label = _ts.node_text(call_hit, source)
                label = " ".join(label.split())  # collapse stray whitespace
                return _hover_text(label, _unit_pretty(unit)), _node_lsp_range(callee)

    # 4. Bare identifier — variable reference. Includes call-callee
    # identifiers as a fallback: if step 3 already returned a
    # signature hover we won't reach here, but if no signature was
    # found we still want to show *something* (the variable's unit if
    # known, or "no annotation"). Without this fallback, hovering on
    # the callee of an intrinsic or an unindexed call shows nothing.
    ident_ctx: ts_checker.Ctx | None = None
    for ident in _ts_h.walk_identifiers(tree):
        if not _ts_h.node_contains(ident, line_1based, col_1based):
            continue
        if _ts_h.is_inside_type_qualifier(ident):
            continue
        name = _ts.node_text(ident, source)
        # Scope-aware lookup: same-named params in two routines no
        # longer alias. Falls back to flat merged_var_units (which
        # carries imports) when no scoped entry matches.
        if ident_ctx is None:
            ident_ctx = _build_ts_ctx(
                result, source, str(resolved_path), path=resolved_path,
            )
        unit = ident_ctx.unit_for(name, ident.start_byte)
        if unit is not None:
            unit_src = _unit_source_for(
                result, resolved_path, name, ident_ctx.scope_at(ident.start_byte),
            )
            return (
                _hover_text(name, _unit_pretty(unit), unit_source=unit_src),
                _node_lsp_range(ident),
            )
        # Lower-case fallback for var_units keyed by original case
        # (covers names whose annotation lives only in the flat view).
        for k, u in result.merged_var_units.items():
            if k.lower() == name.lower():
                return _hover_text(name, _unit_pretty(u)), _node_lsp_range(ident)
        return (
            _hover_text(name, "no unit annotation", show_unit_label=False),
            _node_lsp_range(ident),
        )

    # 5. Numeric literal — dim'less by construction. Most-specific
    # match wins over the enclosing assignment / expression context.
    for n in _ts.walk(tree.root_node):
        if n.type != "number_literal":
            continue
        if not _ts_h.node_contains(n, line_1based, col_1based):
            continue
        from dimfort.core.units import format_unit
        ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
        nu = ts_checker.resolve_unit(n, ctx, source)
        u_s = format_unit(nu) if nu is not None else "1"
        body = f"{_node_label(n, source)} : {u_s}"
        text = f"**🟢 DimFort**\n\n```\n{body}\n```"
        return text, _node_lsp_range(n)
    return None


def _unit_source_for(
    result: WorksetResult, resolved_path: Path, name: str, scope_lc: str | None,
) -> str | None:
    """Return the provenance tag (``"explicit"`` / ``"intrinsic_default"``)
    for a variable's annotation, or ``None`` if unknown.

    Looks up the file's :class:`AttachmentResult` via the workset
    result; falls back to ``None`` for variables that came in through
    a ``use`` clause (the source-file tag isn't accessible at the
    consumer site without a deeper rewrite).
    """
    attached = result.attachments.get(resolved_path)
    if attached is None:
        return None
    sources: dict[tuple[str | None, str], str] | None = getattr(
        attached, "var_unit_sources", None
    )
    if not sources:
        return None
    # Scope-aware lookup first, then module-level, then any-scope.
    if scope_lc is not None:
        s = sources.get((scope_lc, name))
        if s is not None:
            return s
    s = sources.get((None, name))
    if s is not None:
        return s
    # Loose fallback: any scope that knows this name.
    for (_, n), src in sources.items():
        if n == name:
            return src
    return None


def _expression_hover_for(
    uri: str, line_1based: int, col_1based: int,
    *,
    hover_mode: str = "short",
) -> tuple[str, lsp.Range] | None:
    """Expression hover. Fires when no more-specific hover matched
    (i.e. cursor isn't on an identifier or callee). Renders Short or
    Detailed depending on ``hover_mode``.

    Surfaces handled:

    - Enclosing assignment (cursor on ``=``, operator, whitespace).
    - Enclosing relational expression (homogeneity check on operands).
    - Computed sub-expression (call arg, IF/DO/WHERE condition, ...).
    - Numeric literal.
    """
    found = _trees_for(uri)
    if found is None:
        return None
    resolved_path, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None
    # Most-specific wins: a cursor directly on a ``+`` / ``-`` / ``*``
    # / ``/`` / ``**`` token should report that operator's own check,
    # not the enclosing assignment. ``+`` and ``-`` are homogeneity-
    # checked (operands must be unit-equal); the rest just report the
    # sub-expression's resolved unit.
    op_hit = _math_op_at_cursor(tree, line_1based, col_1based)
    if op_hit is not None:
        op_node, parent = op_hit
        ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
        ctx.var_types.update(ts_checker.collect_var_types(tree, source))
        ctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
        ctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))
        if hover_mode == "short":
            if op_node.type in ("+", "-"):
                return _render_mathop_short(parent, ctx, source)
            return _render_subexpr_short(parent, ctx, source)
        # Detailed: fall through to the tree path with parent as the root.
        return _expression_hover_render_tree(
            parent, ctx, source, range_node=parent,
        )
    asn = _ts_h.smallest_enclosing(
        _ts_h.walk_assignments(tree), line_1based, col_1based
    )
    if asn is None:
        return _expression_hover_for_context(
            tree, source, resolved_path, result, line_1based, col_1based,
            hover_mode=hover_mode,
        )
    lhs = None
    rhs = None
    saw_eq = False
    for c in asn.children:
        if c.type == "=":
            saw_eq = True
            continue
        # Fortran line-continuation tokens (``&`` at end of one line
        # and start of the next) appear as children alongside the
        # actual RHS expression. Skip them so the RHS picker lands on
        # the real expression instead of the continuation glyph.
        if c.type == "&":
            continue
        if not saw_eq:
            lhs = lhs or c
        elif saw_eq:
            rhs = c
            break
    if lhs is None or rhs is None:
        return None
    ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
    ctx.var_types.update(ts_checker.collect_var_types(tree, source))
    ctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
    ctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))
    if hover_mode == "short":
        return _render_assignment_short(asn, lhs, rhs, ctx, source)
    rows: list[_TreeRow] = []
    lhs_unit = ts_checker.resolve_unit(lhs, ctx, source)
    from dimfort.core.units import format_unit
    # Header marker is diagnostic-driven (docs/design/markers.md): the
    # assignment's aggregated marker already folds in H001/S001/S002 and any
    # nested RHS mismatch, so no separate row re-aggregation is needed.
    match_tag = _node_marker(asn, ctx, source)
    # Root row has no unit / mark column — the verdict lives in the
    # bold header above. Pass ``None`` so the renderer omits the row.
    rows.append((_node_label(asn, source), None, "", ""))
    # LHS leaf: variable + annotated unit, with its own diagnostic-driven
    # marker (resolution axis, since the LHS rarely owns a diagnostic).
    lhs_mark = _node_marker(lhs, ctx, source)
    rows.append((
        "├── " + _node_label(lhs, source),
        format_unit(lhs_unit) if lhs_unit is not None else "?",
        lhs_mark,
        "",
    ))
    _render_ast_tree(
        rhs, ctx, source,
        prefix="", is_last=True, is_root=False, rows=rows,
    )
    if not rows:
        return None
    max_label = max(len(r[0]) for r in rows)
    max_unit = max(len(r[1]) for r in rows if r[1] is not None)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        if unit is None:
            # Root row: no unit / mark column.
            lines.append(f"{label.ljust(max_label)}  {rule}".rstrip())
        elif rule:
            lines.append(
                f"{label.ljust(max_label)}  :  {unit.ljust(max_unit)}  {mark}  {rule}"
            )
        else:
            lines.append(
                f"{label.ljust(max_label)}  :  {unit.ljust(max_unit)}  {mark}".rstrip()
            )
    body = "\n".join(lines)
    # No horizontal rule between header and code fence: VSCode places a
    # natural paragraph margin between a bold paragraph and a code
    # block already, and every markdown spacer we tried beneath ``---``
    # was either one full line (too tall) or collapsed (no gap). The
    # default margin is the cleanest compromise.
    text = f"**{match_tag} DimFort**\n\n```\n" + body + "\n```"
    return text, _node_lsp_range(asn)


def _expression_hover_for_context(
    tree: Tree, source: bytes, resolved_path: Path, result: WorksetResult,
    line_1based: int, col_1based: int,
    *,
    hover_mode: str = "short",
) -> tuple[str, lsp.Range] | None:
    """Trace-mode hover for non-assignment contexts.

    Fires when the cursor sits inside a call argument, IF/ELSEIF/WHERE
    condition, DO loop bound, or SELECT CASE selector. Renders the
    sub-expression as a unit-algebra tree with a neutral 🟡 marker —
    no LHS to compare against, so there's no homogeneity verdict.
    """
    ctx = _ts_h.smallest_enclosing(
        (n for n in _ts.walk(tree.root_node) if n.type in _TRACE_CONTEXT_TYPES),
        line_1based, col_1based,
    )
    if ctx is None:
        return None
    expr = _pick_trace_subexpr(ctx, line_1based, col_1based)
    if expr is None:
        return None
    rctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
    rctx.var_types.update(ts_checker.collect_var_types(tree, source))
    rctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
    rctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))
    # The callee-on-call case is handled by ``_resolve_hover`` (which
    # dispatches to layout B or C based on the per-surface setting).
    # Here we only render for actual expression contexts (arg
    # expressions, conditions, loop bounds, selectors).
    if expr is ctx and ctx.type in ("call_expression", "subroutine_call"):
        return None
    if hover_mode == "short":
        # Relational expressions are a homogeneity check on their
        # operands — the relation itself has no unit.
        if expr.type == "relational_expression":
            return _render_relational_short(expr, rctx, source)
        return _render_subexpr_short(expr, rctx, source)
    rows: list[_TreeRow] = []
    _render_ast_tree(
        expr, rctx, source,
        prefix="", is_last=True, is_root=True, rows=rows,
    )
    if not rows:
        return None
    max_label = max(len(r[0]) for r in rows)
    # ``unit`` of ``""`` marks a row that should not display a unit at
    # all (e.g. the assignment_statement row — a statement, not an
    # expression). Compute column width only over rows that DO show
    # a unit; unit-less rows skip the ``: unit`` block entirely.
    units_present = [r[1] for r in rows if r[1]]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit else ""
        if rule:
            lines.append(f"{head}{mid}  {mark}  {rule}")
        else:
            lines.append(f"{head}{mid}  {mark}".rstrip())
    body = "\n".join(lines)
    header_marker = _aggregate_marker(r[2] for r in rows)
    text = f"**{header_marker} DimFort**\n\n```\n" + body + "\n```"
    return text, _node_lsp_range(expr)


_MATH_OP_TYPES = frozenset({"+", "-", "*", "/", "**"})


def _math_op_at_cursor(tree: Tree, line: int, col: int) -> tuple[Node, Node] | None:
    """Find a math-expression operator token at the cursor.

    Returns ``(op_node, parent_math_expression)`` if the cursor sits
    directly on a ``+``/``-``/``*``/``/``/``**`` token whose parent
    is a ``math_expression``, else ``None``.
    """
    for n in _ts.walk(tree.root_node):
        if n.type not in _MATH_OP_TYPES:
            continue
        if not _ts_h.node_contains(n, line, col):
            continue
        parent = n.parent
        if parent is None or parent.type != "math_expression":
            continue
        return n, parent
    return None


def _render_mathop_short(
    math_expr: Node, ctx: ts_checker.Ctx, source: bytes
) -> tuple[str, lsp.Range] | None:
    """One-line homogeneity hover for a ``+`` / ``-`` math expression."""
    from dimfort.core.units import format_unit
    operands = [c for c in math_expr.children if c.type not in _SKIP_TOKEN_TYPES]
    if len(operands) < 2:
        return None
    lhs, rhs = operands[0], operands[1]
    marker = _node_marker(math_expr, ctx, source)
    lu = ts_checker.resolve_unit(lhs, ctx, source)
    ru = ts_checker.resolve_unit(rhs, ctx, source)
    lhs_s = format_unit(lu) if lu is not None else "?"
    rhs_s = format_unit(ru) if ru is not None else "?"
    body = (
        f"{_node_label(lhs, source)} : {lhs_s}"
        f"   ◂   {_node_label(rhs, source)} : {rhs_s}"
    )
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(math_expr)


def _expression_hover_render_tree(
    root: Node, ctx: ts_checker.Ctx, source: bytes, *, range_node: Node,
) -> tuple[str, lsp.Range] | None:
    """Detailed-mode tree render rooted at ``root``. Shared by the
    operator-specific path and the generic expression-context path."""
    rows: list[_TreeRow] = []
    _render_ast_tree(
        root, ctx, source,
        prefix="", is_last=True, is_root=True, rows=rows,
    )
    if not rows:
        return None
    max_label = max(len(r[0]) for r in rows)
    # ``unit`` of ``""`` marks a row that should not display a unit at
    # all (e.g. the assignment_statement row — a statement, not an
    # expression). Compute column width only over rows that DO show
    # a unit; unit-less rows skip the ``: unit`` block entirely.
    units_present = [r[1] for r in rows if r[1]]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit else ""
        if rule:
            lines.append(f"{head}{mid}  {mark}  {rule}")
        else:
            lines.append(f"{head}{mid}  {mark}".rstrip())
    body = "\n".join(lines)
    header_marker = _aggregate_marker(r[2] for r in rows)
    text = f"**{header_marker} DimFort**\n\n```\n" + body + "\n```"
    return text, _node_lsp_range(range_node)


def _render_assignment_short(
    asn: Node, lhs: Node, rhs: Node, ctx: ts_checker.Ctx, source: bytes
) -> tuple[str, lsp.Range] | None:
    """One-line homogeneity hover for an assignment cursor position.

    Delegates the assignment-specific logic (autocast detection,
    unit comparison) to ``ts_checker.assignment_homogeneity`` — the
    single source of truth shared with the panel and the checker."""
    from dimfort.core.units import format_unit
    verdict, lhs_u, rhs_u = ts_checker.assignment_homogeneity(
        lhs, rhs, ctx, source,
    )
    # Marker is diagnostic-driven (docs/design/markers.md): the assignment's
    # aggregated marker reflects H001/S001/S002 (and any nested mismatch in
    # the RHS) consistently with the panel + detailed header. ``verdict`` is
    # kept only for the unit display below.
    marker = _node_marker(asn, ctx, source)
    lhs_s = format_unit(lhs_u) if lhs_u is not None else "?"
    rhs_s = format_unit(rhs_u) if rhs_u is not None else "?"
    body = (
        f"{_node_label(lhs, source)} : {lhs_s}"
        f"   ◂   {_node_label(rhs, source)} : {rhs_s}"
    )
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(asn)


def _render_relational_short(
    rel: Node, ctx: ts_checker.Ctx, source: bytes
) -> tuple[str, lsp.Range] | None:
    """One-line homogeneity hover for a relational expression
    (``<``, ``<=``, ``==``, ``/=``, ``>``, ``>=``). The relation
    itself has no unit; only the operands must agree."""
    from dimfort.core.units import format_unit
    # Operands are the non-token children, in source order.
    operands = [c for c in rel.children if c.type not in _SKIP_TOKEN_TYPES
                and c.type not in {"<", "<=", "==", "/=", ">", ">=",
                                   ".lt.", ".le.", ".eq.", ".ne.", ".gt.", ".ge."}]
    if len(operands) < 2:
        return None
    lhs, rhs = operands[0], operands[1]
    # Relational is not an emission site (markers.md §6.1), so its marker is
    # diagnostic-driven like everything else: no consistency diagnostic → 🟡
    # (no unit / not checked), never a re-derived 🔴.
    marker = _node_marker(rel, ctx, source)
    lhs_u = ts_checker.resolve_unit(lhs, ctx, source)
    rhs_u = ts_checker.resolve_unit(rhs, ctx, source)
    lhs_s = format_unit(lhs_u) if lhs_u is not None else "?"
    rhs_s = format_unit(rhs_u) if rhs_u is not None else "?"
    body = (
        f"{_node_label(lhs, source)} : {lhs_s}"
        f"   ◂   {_node_label(rhs, source)} : {rhs_s}"
    )
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(rel)


def _render_subexpr_short(
    expr: Node, ctx: ts_checker.Ctx, source: bytes
) -> tuple[str, lsp.Range] | None:
    """One-line resolved-unit hover for a computed sub-expression or
    a numeric literal. Marker uses propagated-mark logic so a nested
    homogeneity violation surfaces as 🔴 even though the wrapping
    operator has no unit either."""
    from dimfort.core.units import format_unit
    u = ts_checker.resolve_unit(expr, ctx, source)
    marker = _node_marker(expr, ctx, source)
    u_s = format_unit(u) if u is not None else "?"
    body = f"{_node_label(expr, source)} : {u_s}"
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(expr)


def _call_actual_args(call_node: Node) -> list[Node]:
    """Return the actual argument expression nodes of a call, in order."""
    arglist = next(
        (c for c in call_node.children if c.type == "argument_list"), None,
    )
    if arglist is None:
        return []
    out = []
    for c in arglist.children:
        if c.type in _SKIP_TOKEN_TYPES:
            continue
        if c.type == "keyword_argument":
            continue
        out.append(c)
    return out


def _render_call_pairing_a(
    callee_name: str, call_node: Node, sig: FuncSig, rctx: ts_checker.Ctx, source: bytes,
) -> str | None:
    """Layout B: one row per argument, vertical pairing.

    Each row shows ``marker  formal_name : formal_unit  ←  actual_text : actual_unit``.
    Per-arg marker: ✓ match, ✗ mismatch, ? unknown (either side missing).
    Header marker aggregates: 🟢 all match, 🟡 any unknown, 🔴 any mismatch.
    """
    from dimfort.core.units import format_unit
    actuals = _call_actual_args(call_node)
    formal_names = list(sig.arg_names)
    formal_units = list(sig.arg_units)
    n = max(len(formal_names), len(actuals))
    if n == 0:
        return None
    rows: list[tuple[str, str, str, str]] = []  # (mark, formal_lhs, formal_unit, actual)
    any_unknown = False
    any_mismatch = False
    for i in range(n):
        if i < len(formal_names):
            fname = formal_names[i]
            funit = formal_units[i]
            funit_s = format_unit(funit) if funit is not None else "?"
        else:
            fname, funit, funit_s = "—", None, "—"
        if i < len(actuals):
            an = actuals[i]
            atext = _node_label(an, source)
            aunit = ts_checker.resolve_unit(an, rctx, source)
            aunit_s = format_unit(aunit) if aunit is not None else "?"
            actual = f"{atext} : {aunit_s}"
        else:
            an, aunit, actual = None, None, "—"
        # Per-arg marker is intentionally NOT diagnostic-driven (cf.
        # docs/design/markers.md): H004 is emitted on the *whole call*, not
        # per argument, and the checker emits no scale/offset diagnostic at
        # call-arg sites — so there is no per-arg diagnostic to read. A local
        # dimension comparison (matching exactly what H004 checks,
        # ``equal_dim``) is the right tool here; using ``compare()`` would
        # paint scale/offset mismatches with no backing squiggle (the orphan-
        # marker anti-pattern). So this surface stays a local per-arg check.
        if funit is None or aunit is None:
            mark = "🟡"
            any_unknown = True
        elif _checker_equal(funit, aunit):
            mark = "🟢"
        else:
            mark = "🔴"
            any_mismatch = True
        rows.append((mark, fname, funit_s, actual))
    fname_w = max(len(r[1]) for r in rows)
    funit_w = max(len(r[2]) for r in rows)
    if sig.is_subroutine:
        header = f"{callee_name}:"
    else:
        ret_s = format_unit(sig.return_unit) if sig.return_unit is not None else "?"
        header = f"{callee_name}: {ret_s}"
    # Column labels — Unicode mathematical-italic glyphs render italic
    # inside the monospace fence. Each glyph is one codepoint, so
    # ``str.ljust`` width math stays correct.
    sig_label = "Signature"
    call_label = "Call"
    sig_cell_w = max(fname_w + 3 + funit_w, len(sig_label))  # "name : unit"
    col_header = (
        "     "
        + sig_label.ljust(sig_cell_w)
        + "    "
        + call_label
    )
    lines: list[str] = [header, col_header]
    for mark, fname, funit_s, actual in rows:
        lines.append(
            f"  {mark}  {fname.ljust(fname_w)} : {funit_s.ljust(funit_w)}  ◂  {actual}"
        )
    body = "\n".join(lines)
    if any_mismatch:
        marker = "🔴"
    elif any_unknown:
        marker = "🟡"
    else:
        marker = "🟢"
    return f"**{marker} DimFort**\n\n```\n{body}\n```"


def _render_call_pairing_c(
    callee_name: str, call_node: Node, sig: FuncSig, rctx: ts_checker.Ctx, source: bytes,
) -> str | None:
    """Layout C: B's row layout, plus sub-trees expanded under any
    computed argument so the reader can see how each non-trivial actual
    unit was derived.
    """
    from dimfort.core.units import format_unit
    actuals = _call_actual_args(call_node)
    formal_names = list(sig.arg_names)
    formal_units = list(sig.arg_units)
    n = max(len(formal_names), len(actuals))
    if n == 0:
        return None

    # Pre-compute the row triples so we can width-align before emitting,
    # then attach per-arg sub-trees underneath.
    @dataclass
    class _Row:
        mark: str
        fname: str
        funit_s: str
        actual_text: str
        actual_unit_s: str
        sub_lines: list[str]  # indented sub-tree lines (already prefixed)

    rows: list[_Row] = []
    any_unknown = False
    any_mismatch = False
    for i in range(n):
        if i < len(formal_names):
            fname = formal_names[i]
            funit = formal_units[i]
            funit_s = format_unit(funit) if funit is not None else "?"
        else:
            fname, funit, funit_s = "—", None, "—"
        if i < len(actuals):
            an = actuals[i]
            atext = _node_label(an, source)
            aunit = ts_checker.resolve_unit(an, rctx, source)
            aunit_s = format_unit(aunit) if aunit is not None else "?"
        else:
            an, aunit, atext, aunit_s = None, None, "—", "—"
        if funit is None or aunit is None:
            mark = "🟡"
            any_unknown = True
        elif _checker_equal(funit, aunit):
            mark = "🟢"
        else:
            mark = "🔴"
            any_mismatch = True
        sub_lines: list[str] = []
        # Expand sub-tree for computed args only — a bare identifier or
        # literal would just repeat what the actual cell already says.
        if an is not None and an.type not in ("identifier", "number_literal"):
            sub_rows: list[_TreeRow] = []
            _render_ast_tree(
                an, rctx, source,
                prefix="", is_last=True, is_root=True, rows=sub_rows,
            )
            # Drop the root row (== the actual cell we already render);
            # keep only the descendants.
            if len(sub_rows) > 1:
                max_l = max(len(r[0]) for r in sub_rows[1:])
                max_u = max(len(r[1] or "") for r in sub_rows[1:])
                for label, unit, mk, rule in sub_rows[1:]:
                    us = (unit or "").ljust(max_u)
                    if rule:
                        sub_lines.append(
                            f"      {label.ljust(max_l)}  :  {us}  {mk}  {rule}"
                        )
                    else:
                        sub_lines.append(
                            f"      {label.ljust(max_l)}  :  {us}  {mk}".rstrip()
                        )
        rows.append(_Row(mark, fname, funit_s, atext, aunit_s, sub_lines))

    fname_w = max(len(r.fname) for r in rows)
    funit_w = max(len(r.funit_s) for r in rows)
    if sig.is_subroutine:
        header = f"{callee_name}:"
    else:
        ret_s = format_unit(sig.return_unit) if sig.return_unit is not None else "?"
        header = f"{callee_name}: {ret_s}"
    sig_label = "Signature"
    call_label = "Call"
    sig_cell_w = max(fname_w + 3 + funit_w, len(sig_label))
    col_header = (
        "     "
        + sig_label.ljust(sig_cell_w)
        + "    "
        + call_label
    )
    lines: list[str] = [header, col_header]
    for r in rows:
        lines.append(
            f"  {r.mark}  {r.fname.ljust(fname_w)} : {r.funit_s.ljust(funit_w)}  ◂  "
            f"{r.actual_text} : {r.actual_unit_s}"
        )
        lines.extend(r.sub_lines)
    body = "\n".join(lines)
    if any_mismatch:
        marker = "🔴"
    elif any_unknown:
        marker = "🟡"
    else:
        marker = "🟢"
    return f"**{marker} DimFort**\n\n```\n{body}\n```"


def _checker_equal(a: UnitExpr, b: UnitExpr) -> bool:
    """Wrapper-aware dimension equality (delegates to units.equal_dim)."""
    from dimfort.core.units import equal_dim
    return equal_dim(a, b)


def _trace_section_for(uri: str, line_1based: int, col_1based: int) -> str | None:
    """Render the unit-algebra trace as an ASCII tree of the RHS expression.

    Walks the tree, finds the smallest enclosing ``assignment_statement``
    around ``(line, col)``, then renders the RHS as a tree where each
    node carries its resolved unit and the rule that produced it. The
    tree mirrors the source's nesting so readers can map each step to
    a subexpression visually.
    """
    found = _trees_for(uri)
    if found is None:
        return None
    resolved_path, tree, source = found
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return None
    asn = _ts_h.smallest_enclosing(
        _ts_h.walk_assignments(tree), line_1based, col_1based
    )
    if asn is None:
        return None
    rhs = None
    saw_eq = False
    for c in asn.children:
        if c.type == "=":
            saw_eq = True
            continue
        # Skip Fortran line-continuation tokens — see _expression_hover_for.
        if c.type == "&":
            continue
        if saw_eq:
            rhs = c
            break
    if rhs is None:
        return None
    ctx = _build_ts_ctx(result, source, str(resolved_path), path=resolved_path)
    ctx.var_types.update(ts_checker.collect_var_types(tree, source))
    ctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
    ctx.type_field_types.update(ts_checker.collect_type_field_types(tree, source))
    rows: list[_TreeRow] = []  # (label, unit, mark, rule)
    _render_ast_tree(rhs, ctx, source, prefix="", is_last=True, is_root=True, rows=rows)
    if not rows:
        return None
    max_label = max(len(r[0]) for r in rows)
    # ``unit`` of ``""`` marks a row that should not display a unit at
    # all (e.g. the assignment_statement row — a statement, not an
    # expression). Compute column width only over rows that DO show
    # a unit; unit-less rows skip the ``: unit`` block entirely.
    units_present = [r[1] for r in rows if r[1]]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit else ""
        if rule:
            lines.append(f"{head}{mid}  {mark}  {rule}")
        else:
            lines.append(f"{head}{mid}  {mark}".rstrip())
    body = "\n".join(lines)
    return "**Unit-algebra trace**\n\n```\n" + body + "\n```"


# Beyond bare assignments, the trace hover also fires inside these
# expression-bearing contexts. Header keywords ("if", "call", "do", ...)
# get filtered out via _SKIP_TRACE_CHILD_TYPES so the cursor only
# descends into the actual sub-expression.
_TRACE_CONTEXT_TYPES = frozenset({
    "call_expression", "subroutine_call",
    "if_statement", "elseif_clause",
    "where_statement",
    "do_loop", "do_statement",
    "select_case_statement",
})


# Wrapper nodes whose only purpose is grouping — peel through them when
# locating the sub-expression at the cursor inside a context node.
_TRACE_WRAPPER_TYPES = frozenset({
    "parenthesized_expression",
    "argument_list",
    "loop_control_expression",
    "selector",
})


# Statement-keyword / block children that exist alongside the
# sub-expression in a context node. They contain the cursor too if the
# user hovers the keyword itself, but they aren't worth tracing.
_SKIP_TRACE_CHILD_TYPES = frozenset({
    "if", "then", "else", "elseif", "end_if_statement",
    "do", "end_do_loop_statement", "end_do_loop",
    "where", "end_where_statement", "elsewhere_clause",
    "call", "name",
    "select", "case", "end_select_statement", "case_statement",
    "block",
})


def _pick_trace_subexpr(ctx_node: Node, line: int, col: int) -> Node | None:
    """Find the cursor-containing sub-expression inside a trace context.

    Descends through wrapper nodes (parens, argument lists, loop
    control, case selector) so the rendered tree starts at the
    user-visible expression rather than the syntactic shell.
    Returns ``None`` if the cursor sits on a keyword or in an
    assignment_statement (which is handled by the primary trace path).
    """
    target = ctx_node
    is_call = ctx_node.type in ("call_expression", "subroutine_call")
    while True:
        candidate = None
        for c in target.children:
            if c.type in _SKIP_TOKEN_TYPES:
                continue
            if c.type in _SKIP_TRACE_CHILD_TYPES:
                continue
            # Cursor on the callee identifier — root the trace at the
            # whole call so each argument shows up as a branch. The
            # callee itself is filtered out of the rendered children
            # by _interesting_children.
            if target is ctx_node and is_call and c.type == "identifier":
                if _ts_h.node_contains(c, line, col):
                    return ctx_node
                continue
            if not _ts_h.node_contains(c, line, col):
                continue
            candidate = c
            break
        if candidate is None:
            return None
        if candidate.type in _TRACE_WRAPPER_TYPES:
            target = candidate
            continue
        # Don't double-trace: if the cursor is in a nested assignment
        # (e.g. inside a WHERE body), let the assignment branch handle it.
        if candidate.type == "assignment_statement":
            return None
        return candidate


def _render_ast_tree(
    node: Node, ctx: ts_checker.Ctx, source: bytes,
    *,
    prefix: str, is_last: bool, is_root: bool,
    rows: list[_TreeRow],
    target_unit_for_literal: UnitExpr | None = None,
) -> None:
    """Recursively collect ``(label, unit, rule)`` rows for the tree.

    The caller pads each column to the global max so ``⇒`` and the
    rule tag align vertically across nodes.

    ``target_unit_for_literal`` carries the initialization-autocast
    target down the recursion: when we recurse into the RHS of an
    assignment whose RHS is a bare literal, the literal node uses
    this unit and a 🟢 marker (matching the checker's leniency rule).
    """
    # Skip wrapper-only nodes (parenthesised exprs) so the tree doesn't
    # explode with structural-only intermediate nodes — descend straight
    # into their inner expression instead.
    if node.type == "parenthesized_expression":
        inner = _interesting_children(node)
        if len(inner) == 1:
            _render_ast_tree(
                inner[0], ctx, source,
                prefix=prefix, is_last=is_last, is_root=is_root, rows=rows,
                target_unit_for_literal=target_unit_for_literal,
            )
            return

    from dimfort.core.trace import with_trace
    with with_trace() as trace:
        unit = ts_checker.resolve_unit(node, ctx, source)
    snap = trace.snapshot()
    rule_id = snap[-1].rule_id if snap else None

    # Initialization autocast: a pure-numeric-constant subtree (literal,
    # unary-minus literal, math of literals) in a propagated target
    # context takes on the target unit and is marked 🟢. Uses the same
    # predicate as the checker's R4.4 — :func:`ts_checker.is_pure_numeric_constant`
    # — so all three sites (checker, hover, panel) agree on the set of
    # nodes that autocast.
    apply_autocast = (
        target_unit_for_literal is not None
        and ts_checker.is_pure_numeric_constant(node)
    )
    if apply_autocast:
        unit = target_unit_for_literal

    if is_root:
        connector = ""
        next_prefix = prefix
    else:
        connector = "└── " if is_last else "├── "
        next_prefix = prefix + ("    " if is_last else "│   ")

    label = _node_label(node, source)
    # Assignments are statements, not expressions — render no unit
    # column for them (only the marker matters). Other unit-less
    # nodes show ``?``.
    if node.type == "assignment_statement":
        unit_str = ""
    elif unit is None:
        unit_str = "?"
    else:
        from dimfort.core.units import format_unit
        unit_str = format_unit(unit)
    rule_str = f"({rule_id})" if rule_id else ""
    # Marker (docs/design/markers.md): the diagnostic-driven aggregated
    # marker — this node's own (resolution ∨ owned consistency diagnostics)
    # worst-of its descendants. An R4.4 autocast leaf emits nothing and
    # resolves cleanly, so it falls out 🟢 without a special case.
    mark = _node_marker(node, ctx, source)
    # Mark is a separate column so the unit can be ljust-padded
    # independently; markers then align vertically on the right.
    rows.append((prefix + connector + label, unit_str, mark, rule_str))

    # Leaves stop here. Identifiers / numeric literals are atomic.
    if node.type in ("identifier", "number_literal", "string_literal", "complex_literal"):
        return

    children = _interesting_children(node)
    # call_expression: drop the callee identifier from the child list —
    # the parent line already reads ``log(p)`` etc., so re-rendering the
    # bare ``log`` identifier is noise.
    if node.type == "call_expression" and children:
        first = children[0]
        if first.type == "identifier":
            children = children[1:]
    # Compute the autocast target to propagate into children.
    # - Assignment: ask ``ts_checker.assignment_homogeneity`` for the effective
    #   RHS unit; pass it to the last child (the RHS) when the verdict
    #   says we're in autocast mode.
    # - Unary-minus: if THIS node is already being autocast (i.e. it's
    #   a unary-minus wrapping a literal in an autocast context), pass
    #   the target through to the inner literal.
    child_target = None
    if node.type == "assignment_statement" and children:
        verdict, lhs_u, _ = ts_checker.assignment_homogeneity(
            children[0], children[-1], ctx, source,
        )
        if verdict == "autocast" and lhs_u is not None:
            child_target = lhs_u
    elif apply_autocast and node.type == "unary_expression":
        child_target = target_unit_for_literal
    for i, c in enumerate(children):
        is_last_child = (i == len(children) - 1)
        # For assignments, only the last child (RHS) gets the target.
        # For the unary-minus passthrough, the single inner child gets it.
        per_child_target = None
        is_asn_rhs = node.type == "assignment_statement" and is_last_child
        if is_asn_rhs or node.type == "unary_expression":
            per_child_target = child_target
        _render_ast_tree(
            c, ctx, source,
            prefix=next_prefix, is_last=(i == len(children) - 1),
            is_root=False, rows=rows,
            target_unit_for_literal=per_child_target,
        )
