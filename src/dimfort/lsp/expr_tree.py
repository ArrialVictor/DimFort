"""Expression-tree building and diagnostic-driven markers.

The shared *analysis* layer between the hover surfaces and the side panel: it
walks a resolved expression, attaches each node's unit + the 🟢/🟡/🔴 marker
(per docs/design/markers.md), and builds the panel's scope-variable table.
Markers read this file's diagnostics from the cached ``state.last_result`` —
the single source of truth, no per-render overlay. Depends on the checker,
tree-sitter, and the pure tree_nav/markers helpers; never imports ``server``.
"""
from __future__ import annotations

import contextlib
import re
from pathlib import Path

from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core.diagnostics import Diagnostic, Severity
from dimfort.lsp.markers import _marker_token, _worst_emoji, _worst_token
from dimfort.lsp.state import state
from dimfort.lsp.tree_nav import (
    _interesting_children,
    _node_label,
    _node_span_lc,
    _normalized_unit,
    _scope_name,
    _span_within,
)

# The unit-consistency family — the only codes that colour a marker.
_MARKER_DIAG_CODES = frozenset(
    {"H001", "H002", "H003", "H004", "S001", "S002", "S003"}
)

_SEVERITY_EMOJI = {
    Severity.ERROR: "🔴",
    Severity.WARNING: "🟡",
    Severity.INFO: "🟢",
    Severity.HINT: "🟢",
}

# Node types that are statements/relations, not expressions: they carry no
# unit of their own, so their resolution-axis base is 🟢 (a clean assignment
# is not "unresolved"). Their marker comes from the diagnostic axis + children.
_NO_UNIT_NODE_TYPES = frozenset({"assignment_statement", "relational_expression"})


def _diags_for_ctx(ctx) -> tuple[Diagnostic, ...]:
    """This file's diagnostics from the last cached workspace result, keyed
    by ``ctx.file``. The single source the markers read — no per-render
    threading: hover/panel already populate ``state.last_result`` (and the
    publish path keeps it current). Empty when nothing's cached.

    The expensive ``Path.resolve()`` (a disk stat) is cached on the ctx
    object — fresh per render, so no cross-render staleness — while the
    current ``state.last_result`` is always re-read (the diagnostics axis must
    never go stale)."""
    with state.last_result_lock:
        result = state.last_result
    if result is None:
        return ()
    p = getattr(ctx, "_resolved_file", None)
    if p is None:
        try:
            p = Path(ctx.file).resolve()
        except (OSError, TypeError, ValueError):
            return ()
        # frozen/slotted ctx — skip the cache, correctness unaffected
        with contextlib.suppress(AttributeError, TypeError):
            ctx._resolved_file = p
    return tuple(result.diagnostics.get(p, ()))


def _self_marker(node, kid_nodes, ctx, source: bytes) -> str:
    """The node's own marker (pre-aggregation): resolution-axis base worst-of
    the consistency-family diagnostics that *own* this node. A diagnostic owns
    the node when its range sits within the node's span but not within any
    child's span (tightest-enclosing); upward propagation is the caller's
    worst-of-children. See docs/design/markers.md §2–§4."""
    if node.type in _NO_UNIT_NODE_TYPES:
        base = "🟢"  # statement/relation: no unit of its own
    else:
        base = "🟢" if ts_checker.resolve_unit(node, ctx, source) is not None else "🟡"
    diags = _diags_for_ctx(ctx)
    if not diags:
        return base
    nspan = _node_span_lc(node)
    kid_spans = [_node_span_lc(k) for k in kid_nodes]
    worst = base
    for d in diags:
        if d.code not in _MARKER_DIAG_CODES:
            continue
        dspan = (d.start.line, d.start.column), (d.end.line, d.end.column)
        if _span_within(dspan, nspan) and not any(
            _span_within(dspan, ks) for ks in kid_spans
        ):
            worst = _worst_emoji(worst, _SEVERITY_EMOJI.get(d.severity, "🟡"))
    return worst


def _node_marker(node, ctx, source: bytes) -> str:
    """Aggregated marker for a node: its own marker worst-of its children,
    recursively. Used where rows are emitted top-down (the detailed-hover
    tree, the short hovers) and built child payloads aren't on hand.
    ``_build_expression_tree`` aggregates inline from child payloads
    instead, but both reduce to the same §2 model."""
    kids = _interesting_children(node)
    m = _self_marker(node, kids, ctx, source)
    for k in kids:
        m = _worst_emoji(m, _node_marker(k, ctx, source))
    return m


def _build_expression_tree(node, ctx, source: bytes) -> dict | None:
    """Build a structured ExpressionNode for the panel.

    Recursive: each node carries its resolved unit, the rule ID that
    produced it (if any), a marker token, and its children. Leaf
    nodes (identifiers, literals) have an empty ``children`` list.

    Defers all assignment-specific logic (verdict, autocast detection)
    to :func:`ts_checker.assignment_homogeneity` — the single source
    of truth shared with the checker and the in-buffer hover.
    """
    if node is None:
        return None
    if node.type == "parenthesized_expression":
        inner = _interesting_children(node)
        if len(inner) == 1:
            return _build_expression_tree(inner[0], ctx, source)

    from dimfort.core.trace import with_trace
    from dimfort.core.units import format_unit

    with with_trace() as trace:
        unit = ts_checker.resolve_unit(node, ctx, source)
    snap = trace.snapshot()
    rule_id = snap[-1].rule_id if snap else None

    if node.type in ("identifier", "number_literal", "string_literal", "complex_literal"):
        kids: list = []
        child_nodes = []
    else:
        # ``_interesting_children`` already drops the callee identifier and
        # expands the argument list for calls, so each argument becomes a
        # child here (e.g. ``f(v)`` → child ``v``). Don't re-strip the
        # first child: that used to remove the leading *argument* when it
        # was a bare identifier, collapsing calls to a childless leaf.
        kids = _interesting_children(node)
        child_nodes = [_build_expression_tree(c, ctx, source) for c in kids]
        child_nodes = [c for c in child_nodes if c is not None]

    payload = {
        "label": _node_label(node, source),
        "unit": format_unit(unit) if unit is not None else None,
        "marker": "ok",  # set below
        "ruleId": rule_id,
        "children": child_nodes,
    }

    # Assignments are statements, not expressions — they carry no unit of
    # their own; clear it so renderers omit the ``: ?`` column. For an
    # initialization autocast (R4.4) show the LHS unit on the RHS subtree
    # root (the literal takes the LHS's unit). The *marker* is left entirely
    # to the diagnostic model below — autocast emits nothing, so it resolves
    # 🟢 on its own; a real mismatch fires H001 and the model paints it 🔴.
    if node.type == "assignment_statement":
        payload["unit"] = None
        if len(kids) >= 2 and child_nodes:
            verdict, lhs_u, _rhs_u = ts_checker.assignment_homogeneity(
                kids[0], kids[-1], ctx, source,
            )
            if verdict == "autocast" and lhs_u is not None:
                child_nodes[-1]["unit"] = format_unit(lhs_u)

    # Marker (docs/design/markers.md): this node's own marker (resolution
    # axis worst-of the consistency-family diagnostics it owns) worst-of its
    # children. Single source of truth = the diagnostic stream — S001/S002
    # and dimension mismatches all flow through here, no per-check overlay.
    self_token = _marker_token(_self_marker(node, kids, ctx, source))
    payload["marker"] = _worst_token(self_token, *(c["marker"] for c in child_nodes))

    return payload


# Scope kinds that have their own named local declarations keyed by
# ``DeclarationSite.scope`` (the lower-cased routine name). Module /
# program declarations carry ``scope = None`` and are matched by line
# span instead. (The general scope-node set lives in ``tree_nav``.)
_ROUTINE_SCOPE_TYPES = ("subroutine", "function")


def _build_scope_vars(
    scope_node, scan_decls, attached, source: bytes,
    unparseable: frozenset[str] = frozenset(),
) -> list[dict]:
    """Build the declarations table for the enclosing scope.

    Returns one row per declared variable visible in ``scope_node``,
    ordered by declaration line. Each row carries its annotated unit
    text (or ``None``) and a kind tag: ``annotated`` (valid unit),
    ``error`` (has ``@unit{}`` but it failed to parse — names in
    ``unparseable``), or ``unannotated`` (no annotation).

    Matching strategy:
    - For ``subroutine`` / ``function``: ``DeclarationSite.scope`` is
      the routine name, so filter by name.
    - For ``module`` / ``program``: module-level decls carry
      ``scope = None``; filter by ``scope is None`` AND the decl
      falling inside the scope node's line span (so nested routines'
      decls — which have a non-None scope — are excluded).

    Type-field decls inside a ``type :: T`` block are filtered out —
    they're shown via field hover on the parent variable.
    """
    if scope_node is None or scan_decls is None:
        return []
    var_units = attached.var_units if attached is not None else {}
    is_routine = scope_node.type in _ROUTINE_SCOPE_TYPES
    scope_name = _scope_name(scope_node, source)
    if is_routine and scope_name is None:
        return []
    scope_name_lc = scope_name.lower() if scope_name else None
    sp = _ts.position_for(scope_node).line
    ep = _ts.end_position_for(scope_node).line
    source_lines = source.decode("utf-8", "replace").splitlines()

    def _name_on_first_line(decl) -> bool:
        """Robustness guard: tree-sitter error recovery on a half-typed
        declaration (``real ::`` before a name is typed) scavenges an
        identifier from the *following* statement into ``decl.names``,
        with a span that runs into that next line. Such a decl has none
        of its names on its own first physical line — drop it so the
        panel doesn't flash a bogus row mid-typing. Valid multi-line
        continuations always have at least the first name on the
        type-spec line, so they survive this check."""
        idx = decl.line_start - 1
        if not (0 <= idx < len(source_lines)):
            return True  # can't verify — keep
        line_text = source_lines[idx]
        return any(
            re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])",
                      line_text)
            for n in decl.names
        )

    out: list[dict] = []
    for decl in scan_decls:
        if decl.enclosing_type is not None:
            continue
        if is_routine:
            if decl.scope != scope_name_lc:
                continue
        else:
            # Module / program: top-level decls only (scope is None),
            # inside this scope node's line span.
            if decl.scope is not None:
                continue
            if not (sp <= decl.line_start <= ep):
                continue
        if decl.names and not _name_on_first_line(decl):
            continue
        for vname in decl.names:
            unit_text = var_units.get(vname)
            if not unit_text:
                kind = "unannotated"
            elif vname.lower() in unparseable:
                kind = "error"
            else:
                kind = "annotated"
            out.append({
                "name": vname,
                "unit": unit_text if unit_text else None,
                "unitNormalized": _normalized_unit(unit_text) if kind == "annotated" else None,
                "line": decl.line_start,
                "kind": kind,
            })
    return out
