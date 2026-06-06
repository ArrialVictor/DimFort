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
    from dimfort.core.units import UnitExpr

# A rendered unit-algebra tree row: (label, unit-or-None, marker, extra).
# ``unit`` is ``None`` only for the synthetic assignment root row (a statement,
# which has no unit of its own). ``extra`` is an optional trailing annotation
# — currently the ``(expected <formal_unit>)`` tag on a mismatching
# call-argument row; empty string otherwise.
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
                    max_depth = None if level == "detailed" else 1
                    text = _render_call_tree(
                        call_hit, rctx, source, max_depth=max_depth,
                    )
                    if text is None:
                        text = _hover_signature(callee_nm, sig)
                    return text, _node_lsp_range(callee)
            # No user-defined signature — but the call might be a known
            # Fortran intrinsic (log, exp, sqrt, sin, sum, ...). Render
            # it through the same call-tree path as a user call so the
            # two surfaces look identical. Intrinsics aren't in
            # ``ctx.signatures``, so ``_render_ast_tree`` won't attach
            # an ``(expected …)`` annotation to any arg — that's
            # accurate (we don't have formal-arg units for intrinsics),
            # and unit resolution still works because the checker's
            # ``resolve_unit`` handles intrinsics natively.
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
                if _ts_h.node_contains(callee, line_1based, col_1based):
                    rctx = _build_ts_ctx(
                        result, source, str(resolved_path), path=resolved_path,
                    )
                    rctx.var_types.update(ts_checker.collect_var_types(tree, source))
                    rctx.parameter_values.update(ts_checker.collect_parameter_values(tree, source))
                    rctx.type_field_types.update(
                        ts_checker.collect_type_field_types(tree, source)
                    )
                    max_depth = None if hover_mode == "detailed" else 1
                    text = _render_call_tree(
                        call_hit, rctx, source, max_depth=max_depth,
                    )
                    if text is not None:
                        return text, _node_lsp_range(callee)

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
        # Owning-diagnostic marker so an identifier flagged 🔴 doesn't
        # render 🟢 here. ``_node_marker`` returns the worst-of of the
        # node and its children — for a bare identifier the children
        # are usually empty, so the marker is the node's own severity.
        ident_marker = _node_marker(ident, ident_ctx, source)
        if unit is not None:
            unit_src = _unit_source_for(
                result, resolved_path, name, ident_ctx.scope_at(ident.start_byte),
            )
            return (
                _hover_text(
                    name, _unit_pretty(unit),
                    unit_source=unit_src, marker=ident_marker,
                ),
                _node_lsp_range(ident),
            )
        # Lower-case fallback for var_units keyed by original case
        # (covers names whose annotation lives only in the flat view).
        for k, u in result.merged_var_units.items():
            if k.lower() == name.lower():
                return (
                    _hover_text(name, _unit_pretty(u), marker=ident_marker),
                    _node_lsp_range(ident),
                )
        return (
            _hover_text(
                name, "no unit annotation",
                show_unit_label=False, marker=ident_marker,
            ),
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
        # Owning-diagnostic marker — same fix as the bare-identifier
        # path: a literal sitting inside a flagged expression should
        # show 🔴 / 🟡 / 🔵 alongside the rest of the rendered tree,
        # not always paint 🟢.
        n_marker = _node_marker(n, ctx, source)
        text = f"**{n_marker} DimFort**\n\n```\n{body}\n```"
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
        # Assignment short hover = the same tree shape as every other
        # hover: root row (assignment statement, structural-no-unit
        # ``-``) + one row per immediate child (LHS, RHS). The RHS row
        # picks up its ``(expected <lhs_unit>)`` annotation on a
        # homogeneity violation via ``_render_ast_tree``'s assignment
        # propagation rule.
        return _render_subexpr_short(asn, ctx, source)
    rows: list[_TreeRow] = []
    lhs_unit = ts_checker.resolve_unit(lhs, ctx, source)
    from dimfort.core.units import format_unit
    # Header marker is diagnostic-driven (docs/design/markers.md): the
    # assignment's aggregated marker already folds in H001/S001/S002 and any
    # nested RHS mismatch, so no separate row re-aggregation is needed.
    match_tag = _node_marker(asn, ctx, source)
    # Root (assignment) row: structural-no-unit, so its unit column
    # renders ``-`` (matching the panel and the unified renderer at
    # ``_render_ast_tree``). The marker still sits in the rightmost
    # column alongside the children's markers.
    from dimfort.lsp.expr_tree import _NO_UNIT_GLYPH
    rows.append((_node_label(asn, source), _NO_UNIT_GLYPH, match_tag, ""))
    # LHS leaf: variable + annotated unit, with its own diagnostic-driven
    # marker (resolution axis, since the LHS rarely owns a diagnostic).
    lhs_mark = _node_marker(lhs, ctx, source)
    rows.append((
        "├── " + _node_label(lhs, source),
        format_unit(lhs_unit, show_factor=ctx.scale_mode)
        if lhs_unit is not None else "?",
        lhs_mark,
        "",
    ))
    # Detailed-mode assembly assembles the root + LHS rows manually
    # and then calls _render_ast_tree on the RHS — bypassing the
    # assignment node's iteration loop where ``assumed_overlay``,
    # autocast propagation, and ``expected_unit`` propagation are
    # normally set. Compute them here and pass explicitly so the RHS
    # row picks up:
    #   * 🔵 + asserted-unit + ``(assumed: …)`` when @unit_assume;
    #   * the LHS unit on a literal RHS in autocast (R4.4);
    #   * ``(expected <lhs_unit>)`` + 🟡-on-expected on a real
    #     homogeneity mismatch (H001) — same shape as a call-arg
    #     mismatch, mirroring short-hover and panel.
    from dimfort.lsp.expr_tree import _assumed_for
    rhs_assumed_overlay = _assumed_for(asn, ctx)
    rhs_expected: UnitExpr | None = None
    rhs_target: UnitExpr | None = None
    verdict, vlhs, _ = ts_checker.assignment_homogeneity(lhs, rhs, ctx, source)
    if verdict == "mismatch" and vlhs is not None:
        rhs_expected = vlhs
    elif verdict == "autocast" and vlhs is not None:
        rhs_target = vlhs
    _render_ast_tree(
        rhs, ctx, source,
        prefix="", is_last=True, is_root=False, rows=rows,
        assumed_overlay=rhs_assumed_overlay,
        expected_unit=rhs_expected,
        target_unit_for_literal=rhs_target,
    )
    if not rows:
        return None
    # Now that every row carries a unit string (``-`` for structural-
    # no-unit, ``?`` for unresolved, formatted unit otherwise), the
    # rendering loop is uniform and matches ``_format_tree_rows``.
    body = _format_tree_rows(rows)
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
        # All short hovers — including relational expressions, which
        # have no unit of their own — use the same root-plus-immediate-
        # children tree shape so the user never has to learn a special
        # case per surface.
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


def _render_subexpr_short(
    expr: Node, ctx: ts_checker.Ctx, source: bytes
) -> tuple[str, lsp.Range] | None:
    """Short hover for a computed sub-expression or a numeric literal:
    render the same root-plus-immediate-children tree shape as the
    call hover, so every short hover means "root unit, with one level
    of how it got there". Bare leaves (identifiers, literals) collapse
    to a single row naturally because `_render_ast_tree` returns early
    on those node types — no children to enumerate.
    """
    rows: list[_TreeRow] = []
    _render_ast_tree(
        expr, ctx, source,
        prefix="", is_last=True, is_root=True, rows=rows,
        max_depth=1,
    )
    if not rows:
        return None
    body = _format_tree_rows(rows)
    marker = _worst_marker(rows)
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(expr)


def _render_call_tree(
    call_node: Node, rctx: ts_checker.Ctx, source: bytes,
    *, max_depth: int | None,
) -> str | None:
    """Render a call hover as a tree rooted at the call node.

    Same shape and renderer as the side panel's Expression section, so
    the two surfaces are guaranteed to agree. The root row reads
    ``name(arg1, arg2, …) : ret  🟢/🟡/🔴`` (subroutines have no unit
    column on the root) and each immediate child is one actual
    argument; computed actuals expand under their row when
    ``max_depth`` permits.

    ``max_depth=1`` gives the short call hover (call + immediate
    arguments only); ``max_depth=None`` gives the detailed view (full
    sub-tree under each computed actual). The per-arg `(expected …)`
    annotation and the 🟡-on-expected marker override both come from
    :func:`_render_ast_tree`; nothing call-specific lives in this
    function beyond depth selection and outer markdown wrapping.
    """
    rows: list[_TreeRow] = []
    _render_ast_tree(
        call_node, rctx, source,
        prefix="", is_last=True, is_root=True, rows=rows,
        max_depth=max_depth,
    )
    if not rows:
        return None
    body = _format_tree_rows(rows)
    marker = _worst_marker(rows)
    return f"**{marker} DimFort**\n\n```\n{body}\n```"


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
    rows: list[_TreeRow] = []  # (label, unit, mark, extra)
    # Same plumbing as the detailed-mode assignment path: when the
    # enclosing assignment carries @unit_assume, the RHS tree's root
    # row picks up the 🔵 + asserted-unit + (assumed: …) overlay.
    from dimfort.lsp.expr_tree import _assumed_for
    rhs_assumed_overlay = _assumed_for(asn, ctx)
    _render_ast_tree(
        rhs, ctx, source,
        prefix="", is_last=True, is_root=True, rows=rows,
        assumed_overlay=rhs_assumed_overlay,
    )
    if not rows:
        return None
    body = _format_tree_rows(rows)
    return "**Unit-algebra trace**\n\n```\n" + body + "\n```"


def _format_tree_rows(rows: list[_TreeRow]) -> str:
    """Render tree rows with global column alignment.

    Shared between the call hover and the unit-algebra trace section so
    both render with identical width math — same source of truth as the
    panel companions, just rendered server-side as markdown for the
    hover surfaces.

    ``unit`` of ``""`` marks a row that should not display a unit at all
    (e.g. the assignment_statement row — a statement, not an
    expression). Column widths are computed only over rows that DO show
    a unit; unit-less rows skip the ``: unit`` block entirely so the
    marker still aligns.
    """
    max_label = max(len(r[0]) for r in rows)
    units_present = [r[1] for r in rows if r[1]]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, extra in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit else ""
        if extra:
            lines.append(f"{head}{mid}  {mark}  {extra}")
        else:
            lines.append(f"{head}{mid}  {mark}".rstrip())
    return "\n".join(lines)


def _worst_marker(rows: list[_TreeRow]) -> str:
    """Header marker for a tree: worst-of all row markers."""
    found = {r[2] for r in rows}
    if "🔴" in found:
        return "🔴"
    if "🟡" in found:
        return "🟡"
    return "🟢"


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
    expected_unit: UnitExpr | None = None,
    assumed_overlay: tuple[str, str] | None = None,
    polymorphism_conflict_row: tuple[str, tuple[int, ...]] | None = None,
    max_depth: int | None = None,
    _depth: int = 0,
) -> None:
    """Recursively collect ``(label, unit, mark, extra)`` rows for the tree.

    The caller pads each column to the global max so the marker and the
    trailing annotation align vertically across nodes.

    ``target_unit_for_literal`` carries the initialization-autocast
    target down the recursion: when we recurse into the RHS of an
    assignment whose RHS is a bare literal, the literal node uses
    this unit and a 🟢 marker (matching the checker's leniency rule).

    ``expected_unit`` is the formal unit this node is expected to satisfy
    (only set when this node is an argument of a call whose callee
    signature is known). When the resolved unit doesn't match, the row
    gets an ``(expected <formal>)`` annotation so the reader can see
    what the call-site demanded without round-tripping through the
    diagnostic message.

    ``assumed_overlay`` is the ``(asserted_unit_str, reason)`` pair
    from the ``@unit_assume`` directive on this node's parent
    assignment. Only the **RHS child** of an assumed assignment
    receives it; the row displays the asserted unit, paints 🔵 (the
    overlay tier — markers.md §4.6), and gets a
    ``(assumed: <reason>)`` row tail. The assignment row itself
    never carries the overlay.
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
                expected_unit=expected_unit,
                assumed_overlay=assumed_overlay,
                max_depth=max_depth, _depth=_depth,
            )
            return

    unit = ts_checker.resolve_unit(node, ctx, source)

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
    # Unit-column rendering — three glyphs, three meanings (see
    # docs/design/markers.md §4.5):
    #   ``-`` — structural-no-unit (assignment / relation / subroutine
    #           call); the row has no unit *by design*, not because we
    #           couldn't resolve one.
    #   <fmt> — resolved unit, formatted.
    #   ``?`` — unknown (unannotated identifier, unsupported intrinsic,
    #           partial resolution).
    from dimfort.core.units import equal_dim, format_unit
    from dimfort.lsp.expr_tree import _NO_UNIT_GLYPH, _NO_UNIT_NODE_TYPES
    # Surface scale factors when scale checking is on — uniform rule
    # across every panel/hover surface (see ``_build_expression_tree``
    # and ``_normalized_unit`` for the same gate). Off-mode hides the
    # factor so displays don't claim significance the checker ignores.
    sf = ctx.scale_mode
    if node.type in _NO_UNIT_NODE_TYPES:
        unit_str = _NO_UNIT_GLYPH
    elif unit is not None:
        unit_str = format_unit(unit, show_factor=sf)
    else:
        unit_str = "?"
    extra_str = ""
    if (
        expected_unit is not None
        and unit is not None
        and not ts_checker._unit_expr_has_tyvars(expected_unit)
        and not equal_dim(unit, expected_unit)
    ):
        # ``(expected …)`` only when the formal is concrete. A
        # polymorphic formal (tyvar-bearing) unifies with any actual
        # — the dimensional comparison is irrelevant, the unifier
        # decides, and either an H020 fires (handled below) or the
        # call is clean (no trailer, marker stays 🟢). Mirrors the
        # panel-side gate in :func:`_build_expression_tree`.
        extra_str = f"(expected {format_unit(expected_unit, show_factor=sf)})"
    # H020 polymorphic-conflict override. The unit column renders
    # ``'a = <actual>`` (the binding this slot would force) and the
    # row tail flips from ``(expected 'a)`` to the spec's ``(collides
    # with arg N)`` form. See docs/design/shipped/polymorphic-units.md
    # §H020. Mirrors :func:`_build_expression_tree` for panel/hover
    # parity.
    if (
        polymorphism_conflict_row is not None
        and expected_unit is not None
        and unit is not None
    ):
        _binding_text, partner_indices = polymorphism_conflict_row
        formal_render = format_unit(expected_unit, show_factor=sf)
        actual_render = format_unit(unit, show_factor=sf)
        unit_str = f"{formal_render} = {actual_render}"
        if partner_indices:
            partners = ", ".join(f"arg {p + 1}" for p in partner_indices)
            extra_str = f"(collides with {partners})"
        else:
            extra_str = ""
    # `@unit_assume` overlay — applied to the RHS row of an assumed
    # assignment (the parent's loop passes ``assumed_overlay`` to that
    # one child; this node itself never carries the overlay because
    # the directive applies to the RHS expression, not the assignment
    # statement). When set:
    #   * Override the unit column to the *asserted* unit, not the
    #     computed one (typically ``?`` for empirical fits).
    #   * Paint the marker 🔵 unless an honest diagnostic (🔴) owns
    #     this node — declared-unit conflicts aren't masked.
    #   * Append ``(assumed: <reason>)`` to the row tail.
    if assumed_overlay is not None:
        asserted_unit_str, reason = assumed_overlay
        unit_str = asserted_unit_str
        extra_str = (
            f"{extra_str}  (assumed: {reason})" if extra_str
            else f"(assumed: {reason})"
        )
    # Marker (docs/design/markers.md): the diagnostic-driven aggregated
    # marker — this node's own (resolution ∨ owned consistency diagnostics)
    # worst-of its descendants. An R4.4 autocast leaf emits nothing and
    # resolves cleanly, so it falls out 🟢 without a special case.
    mark = _node_marker(node, ctx, source)
    # Call-arg-formal disagreement override: when this row carries an
    # ``(expected …)`` annotation AND would otherwise paint 🟢, demote
    # to 🟡. Rationale: the expression resolved cleanly, but its caller
    # disagrees with the formal — worth flagging without painting a
    # hard 🔴 (reserved for diagnostic-owned mismatches). The 🔴 already
    # sits on the enclosing call via H004's diagnostic.
    if extra_str and mark == "🟢" and assumed_overlay is None:
        mark = "🟡"
    # H020 polymorphic-conflict override: every contributing arg row
    # owns part of the conflict and renders 🔴 — strictly stronger
    # than the 🟡-on-expected demote above (which is suppressed
    # alongside its trailer when this branch fires). Mirrors the
    # panel-side override in :func:`_build_expression_tree`. The
    # diagnostic-owned 🔴 on the enclosing call still propagates
    # independently through ``_node_marker``.
    if polymorphism_conflict_row is not None:
        mark = "🔴"
    # `@unit_assume` overlay wins the marker column (after the
    # 🟡-on-expected step above) on 🟢/🟡 rows — the assumption is
    # the headline at this row. A 🔴 from a diagnostic owning *this*
    # node still wins.
    if assumed_overlay is not None and mark in ("🟢", "🟡"):
        mark = "🔵"
    # Mark is a separate column so the unit can be ljust-padded
    # independently; markers then align vertically on the right.
    rows.append((prefix + connector + label, unit_str, mark, extra_str))

    # Leaves stop here. Identifiers / numeric literals are atomic.
    if node.type in ("identifier", "number_literal", "string_literal", "complex_literal"):
        return
    # Depth cap: short call hover renders only call + immediate children
    # (no recursion into computed arguments). ``None`` = unbounded.
    if max_depth is not None and _depth >= max_depth:
        return

    children = _interesting_children(node)
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
    # Per-child expected_unit propagation. Two sources:
    #   * Call: each positional arg's expected unit = the callee's
    #     formal unit (from ``ctx.signatures``).
    #   * Assignment: the RHS's expected unit = the LHS unit (the
    #     declared type of what we're assigning into). The LHS itself
    #     has no expected — it's the source of truth here.
    # A child whose resolved unit dimensionally disagrees with its
    # ``expected_unit`` paints 🟡 + ``(expected <formal>)`` per the
    # override in this same function above.
    arg_expected: list[UnitExpr | None] = []
    poly_conflict_map: dict[int, tuple[str, tuple[int, ...]]] | None = None
    if node.type in ("call_expression", "subroutine_call"):
        callee_nm = _ts_h.call_name(node, source)
        if callee_nm is not None:
            sig = ctx.signatures.get(callee_nm.lower())
            if sig is not None:
                arg_expected = list(sig.arg_units)
        # H020 conflict data, if this call fires one. Threaded per-slot
        # to the child render so each conflicting arg renders the
        # spec's ``(collides with arg N)`` trailer. Reuses the
        # panel-side helper for single-source-of-truth diagnostic
        # extraction.
        from dimfort.lsp.expr_tree import _h020_conflict_map_for_call
        poly_conflict_map = _h020_conflict_map_for_call(node, ctx)
    elif node.type == "assignment_statement" and len(children) >= 2:
        # ``assignment_homogeneity`` already does the autocast vs
        # mismatch decision; in autocast the RHS resolves to LHS unit
        # (via ``target_unit_for_literal`` propagation above), so the
        # equal_dim check yields no annotation. For a real mismatch
        # the annotation surfaces and the RHS row paints 🟡 +
        # ``(expected <lhs_unit>)`` — same shape as a call-arg
        # mismatch.
        _, lhs_for_expected, _ = ts_checker.assignment_homogeneity(
            children[0], children[-1], ctx, source,
        )
        if lhs_for_expected is not None:
            arg_expected = [None] * (len(children) - 1) + [lhs_for_expected]
    # ``@unit_assume`` propagation: if THIS node is an assumed
    # assignment_statement, the RHS child gets the overlay (asserted
    # unit + reason). The assignment row itself stays clean — the
    # directive's syntactic subject is the RHS expression.
    rhs_assumed_overlay: tuple[str, str] | None = None
    if node.type == "assignment_statement":
        from dimfort.lsp.expr_tree import _assumed_for
        rhs_assumed_overlay = _assumed_for(node, ctx)
    for i, c in enumerate(children):
        is_last_child = (i == len(children) - 1)
        # For assignments, only the last child (RHS) gets the target.
        # For the unary-minus passthrough, the single inner child gets it.
        per_child_target = None
        is_asn_rhs = node.type == "assignment_statement" and is_last_child
        if is_asn_rhs or node.type == "unary_expression":
            per_child_target = child_target
        per_child_expected: UnitExpr | None = None
        if arg_expected and i < len(arg_expected):
            per_child_expected = arg_expected[i]
        per_child_assumed: tuple[str, str] | None = None
        if rhs_assumed_overlay is not None and is_last_child:
            per_child_assumed = rhs_assumed_overlay
        per_child_poly_conflict: tuple[str, tuple[int, ...]] | None = (
            poly_conflict_map.get(i)
            if poly_conflict_map is not None else None
        )
        _render_ast_tree(
            c, ctx, source,
            prefix=next_prefix, is_last=(i == len(children) - 1),
            is_root=False, rows=rows,
            target_unit_for_literal=per_child_target,
            expected_unit=per_child_expected,
            assumed_overlay=per_child_assumed,
            polymorphism_conflict_row=per_child_poly_conflict,
            max_depth=max_depth, _depth=_depth + 1,
        )
