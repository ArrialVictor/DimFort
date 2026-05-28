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
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tree_sitter import Node

from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core.diagnostics import Diagnostic, Severity
from dimfort.lsp import ts_helpers as _ts_h
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

if TYPE_CHECKING:
    from dimfort.core.annotations import DeclarationSite
    from dimfort.core.attach import AttachmentResult
    from dimfort.core.units import UnitExpr

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


def _diags_for_ctx(ctx: ts_checker.Ctx) -> tuple[Diagnostic, ...]:
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
        # frozen/slotted ctx — skip the cache, correctness unaffected.
        # ``_resolved_file`` is an optional render-time cache, not a declared
        # _Ctx field, so stash it dynamically.
        with contextlib.suppress(AttributeError, TypeError):
            ctx._resolved_file = p  # type: ignore[attr-defined]
    return tuple(result.diagnostics.get(p, ()))


def _self_marker(node: Node, kid_nodes: list[Node], ctx: ts_checker.Ctx, source: bytes) -> str:
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


def _node_marker(node: Node, ctx: ts_checker.Ctx, source: bytes) -> str:
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


def _build_expression_tree(
    node: Node | None, ctx: ts_checker.Ctx, source: bytes,
    *, expected_unit: UnitExpr | None = None,
) -> dict[str, Any] | None:
    """Build a structured ExpressionNode for the panel.

    Recursive: each node carries its resolved unit, a marker token, and
    its children. Leaf nodes (identifiers, literals) have an empty
    ``children`` list. When this node is a positional argument of a
    call whose callee signature is known, ``expected_unit`` carries the
    formal's :class:`UnitExpr`; on a dimensional mismatch the payload's
    ``expected`` field renders it pretty so the panel can append
    ``(expected …)`` to the row.

    Defers all assignment-specific logic (verdict, autocast detection)
    to :func:`ts_checker.assignment_homogeneity` — the single source
    of truth shared with the checker and the in-buffer hover.
    """
    if node is None:
        return None
    if node.type == "parenthesized_expression":
        inner = _interesting_children(node)
        if len(inner) == 1:
            return _build_expression_tree(
                inner[0], ctx, source, expected_unit=expected_unit,
            )

    from dimfort.core.units import equal_dim, format_unit

    unit = ts_checker.resolve_unit(node, ctx, source)

    if node.type in ("identifier", "number_literal", "string_literal", "complex_literal"):
        kids: list[Node] = []
        child_nodes: list[dict[str, Any]] = []
    else:
        # ``_interesting_children`` already drops the callee identifier and
        # expands the argument list for calls, so each argument becomes a
        # child here (e.g. ``f(v)`` → child ``v``). Don't re-strip the
        # first child: that used to remove the leading *argument* when it
        # was a bare identifier, collapsing calls to a childless leaf.
        kids = _interesting_children(node)
        # Look up the callee signature so positional args get their
        # formal expected unit propagated. Subroutine_call and
        # call_expression both share this path.
        arg_expected: list[UnitExpr | None] = []
        if node.type in ("call_expression", "subroutine_call"):
            callee_nm = _ts_h.call_name(node, source)
            if callee_nm is not None:
                sig = ctx.signatures.get(callee_nm.lower())
                if sig is not None:
                    arg_expected = list(sig.arg_units)
        built = [
            _build_expression_tree(
                c, ctx, source,
                expected_unit=(
                    arg_expected[i]
                    if arg_expected and i < len(arg_expected) else None
                ),
            )
            for i, c in enumerate(kids)
        ]
        child_nodes = [c for c in built if c is not None]

    # Render the `(expected …)` annotation only when actual and formal
    # disagree dimensionally — matching the call-hover rule.
    expected_render: str | None = None
    if (
        expected_unit is not None
        and unit is not None
        and not equal_dim(unit, expected_unit)
    ):
        expected_render = format_unit(expected_unit)

    payload: dict[str, Any] = {
        "label": _node_label(node, source),
        "unit": format_unit(unit) if unit is not None else None,
        "marker": "ok",  # set below
        "expected": expected_render,
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

    # Call-arg-formal disagreement override: when this row carries an
    # ``expected`` annotation (its actual unit dimensionally differs from
    # the call's formal) AND no diagnostic painted it worse, demote the
    # marker from ``ok`` to ``warn``. The expression itself resolved
    # cleanly here, but its caller disagrees with the formal it's
    # flowing into — worth flagging without painting a hard ``error``
    # (which is reserved for diagnostic-owned mismatches; the enclosing
    # call carries H004's ``error`` already). Mirrors the hover-side
    # override in :func:`_render_ast_tree`.
    if expected_render and payload["marker"] == "ok":
        payload["marker"] = "warn"

    return payload


# Scope kinds that have their own named local declarations keyed by
# ``DeclarationSite.scope`` (the lower-cased routine name). Module /
# program declarations carry ``scope = None`` and are matched by line
# span instead. (The general scope-node set lives in ``tree_nav``.)
_ROUTINE_SCOPE_TYPES = ("subroutine", "function")


def _name_on_first_line(decl: DeclarationSite, source_lines: list[str]) -> bool:
    """Robustness guard: tree-sitter error recovery on a half-typed
    declaration (``real ::`` before a name is typed) scavenges an
    identifier from the *following* statement into ``decl.names``, with
    a span that runs into that next line. Such a decl has none of its
    names on its own first physical line — drop it so the panel doesn't
    flash a bogus row mid-typing. Valid multi-line continuations always
    have at least the first name on the type-spec line, so they survive
    this check."""
    idx = decl.line_start - 1
    if not (0 <= idx < len(source_lines)):
        return True  # can't verify — keep
    line_text = source_lines[idx]
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", line_text)
        for n in decl.names
    )


def _decl_rows(
    decl: DeclarationSite,
    var_units: dict[str, str],
    unparseable: frozenset[str],
) -> list[dict[str, Any]]:
    """One ScopeVar row per name on a declaration, tagged annotated /
    error / unannotated. Shared by the node-based and span-based scope
    builders so both emit identical row shapes."""
    rows: list[dict[str, Any]] = []
    for vname in decl.names:
        unit_text = var_units.get(vname)
        if not unit_text:
            kind = "unannotated"
        elif vname.lower() in unparseable:
            kind = "error"
        else:
            kind = "annotated"
        rows.append({
            "name": vname,
            "unit": unit_text if unit_text else None,
            "unitNormalized": (
                _normalized_unit(unit_text)
                if unit_text and kind == "annotated" else None
            ),
            "line": decl.line_start,
            "kind": kind,
        })
    return rows


def _build_scope_vars(
    scope_node: Node | None,
    scan_decls: Iterable[DeclarationSite] | None,
    attached: AttachmentResult | None,
    source: bytes,
    unparseable: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
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

    out: list[dict[str, Any]] = []
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
        if decl.names and not _name_on_first_line(decl, source_lines):
            continue
        out.extend(_decl_rows(decl, var_units, unparseable))
    return out


# Scope-opening header statement nodes that survive tree-sitter error
# recovery even when the enclosing routine collapses into an ``ERROR``
# node (the full ``subroutine`` / ``function`` node is gone, but its
# header statement is still emitted inside the ERROR). Maps the
# ``*_statement`` node type to the scope ``kind`` the panel reports.
_SCOPE_HEADER_TYPES = {
    "subroutine_statement": "subroutine",
    "function_statement": "function",
    "module_statement": "module",
    "program_statement": "program",
}

# A line that closes a program unit: bare ``end`` or ``end <kind>``.
# Deliberately excludes block ends (``end do`` / ``end if`` / ``end
# type`` / ``end select`` / …) so they don't pop a routine scope.
_SCOPE_END_RE = re.compile(
    r"^\s*end"
    r"(?:"
    r"\s*(?:!.*)?$"  # bare end (optional trailing comment)
    r"|\s*(?:subroutine|function|module|submodule|program)\b"  # end <kind>
    r")",
    re.IGNORECASE,
)


def recover_scopes(tree: Any, source: bytes) -> list[tuple[str, str, int, int]]:
    """Reconstruct enclosing scopes when tree-sitter has no scope node.

    A single unparseable statement makes tree-sitter wrap the whole
    routine in an ``ERROR`` node, so ``_enclosing_scopes`` finds nothing
    and the panel's Scope section would blank. But the routine's *header*
    statement still survives inside the ERROR, so we recover each scope's
    name + kind from the surviving headers and pair them with the closing
    ``end`` lines (line-based, since the ``end`` may have been absorbed by
    the error region). Returns ``(kind, name, start_line, end_line)``
    tuples (1-based, inclusive), one per recovered scope.
    """
    headers: dict[int, tuple[str, str]] = {}  # start_line -> (kind, name)
    for n in _ts.walk(tree.root_node):
        kind = _SCOPE_HEADER_TYPES.get(n.type)
        if kind is None:
            continue
        name_node = next((c for c in n.children if c.type == "name"), None)
        name = (
            (name_node.text or b"").decode("utf-8", "replace")
            if name_node is not None else "?"
        )
        headers[_ts.position_for(n).line] = (kind, name)

    source_lines = source.decode("utf-8", "replace").splitlines()
    out: list[tuple[str, str, int, int]] = []
    stack: list[tuple[str, str, int]] = []  # (kind, name, start_line)
    for line_no in range(1, len(source_lines) + 1):
        hdr = headers.get(line_no)
        if hdr is not None:
            stack.append((hdr[0], hdr[1], line_no))
            continue
        if stack and _SCOPE_END_RE.match(source_lines[line_no - 1]):
            kind, name, start = stack.pop()
            out.append((kind, name, start, line_no))
    # Any scope left open (no matching end found) runs to end of file.
    last_line = len(source_lines)
    for kind, name, start in stack:
        out.append((kind, name, start, last_line))
    return out


def _innermost_scope_idx(
    line: int, scopes: list[tuple[str, str, int, int]]
) -> int | None:
    """Index of the smallest recovered scope containing ``line``, or None."""
    best: int | None = None
    best_size: int | None = None
    for idx, (_kind, _name, s, e) in enumerate(scopes):
        if s <= line <= e:
            size = e - s
            if best is None or best_size is None or size < best_size:
                best, best_size = idx, size
    return best


def build_scope_vars_by_span(
    scope_idx: int,
    recovered: list[tuple[str, str, int, int]],
    scan_decls: Iterable[DeclarationSite] | None,
    attached: AttachmentResult | None,
    source: bytes,
    unparseable: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Span-based scope variables for a recovered scope (the ERROR-node
    fallback). A declaration belongs to the recovered scope that most
    tightly encloses it, so a module section excludes its contained
    routines' locals (and sibling routines don't bleed into each other).
    Matches by line span because the ERROR collapse strips
    ``DeclarationSite.scope`` to ``None``."""
    if scan_decls is None:
        return []
    var_units = attached.var_units if attached is not None else {}
    source_lines = source.decode("utf-8", "replace").splitlines()
    out: list[dict[str, Any]] = []
    for decl in scan_decls:
        if decl.enclosing_type is not None:
            continue
        if _innermost_scope_idx(decl.line_start, recovered) != scope_idx:
            continue
        if decl.names and not _name_on_first_line(decl, source_lines):
            continue
        out.extend(_decl_rows(decl, var_units, unparseable))
    return out
