"""DimFort language server.

Speaks LSP over stdio. On ``initialize`` the server picks up the
workspace folders and scans them for Fortran sources; thereafter every
relevant event re-runs the pipeline over the whole workset so that
``use mod_other`` resolves and cross-file H004 lights up in the editor
exactly as it does on the command line.

Triggers:

- ``textDocument/didOpen`` and ``didSave``: immediate check.
- ``textDocument/didChange``: debounced live check (in-memory buffer
  text is passed to the pipeline so unsaved edits are honoured).
- ``textDocument/didClose``: republish the cached workspace
  diagnostics so the Problems panel keeps showing them after the
  file is closed.

Provides:

- ``textDocument/publishDiagnostics`` — H-series + U-series.
- ``textDocument/hover`` — resolved unit for the variable or
  derived-type member under the cursor.

This module is the *spine* of the LSP package: it owns the pygls
``LanguageServer`` instance and every ``@server.feature`` registration, the
lifecycle handlers (``initialize`` / ``initialized`` / document-sync), the
diagnostic publish side (``_publish_for_uri`` / ``_ensure_uri_loaded`` /
``_refresh_inlay_hints``), the feature toggles, and the entry point
(``run_stdio``). The hover machinery (``_resolve_hover`` + the ``_render_*``
renderers) also still lives here, pending extraction into ``hover.py``.

Each ``@server.feature`` handler here is a *thin wrapper*: it does the
feature-flag check, calls ``_ensure_uri_loaded`` if needed, acquires
``state.ts_handler_lock`` if it traverses the cached tree, then delegates to a
logic function in a feature module (``completion`` / ``definition`` / ``inlay``
/ ``interactions`` / ``code_action`` / ``panel``). Shared logic lives in
``state`` / ``tree_access`` / ``tree_nav`` / ``decl_scan`` / ``expr_tree`` /
``hover_render`` / ``markers``. See ``docs/design/lsp-architecture.md`` for the
full module map and the three load-bearing patterns (singleton state, handler
delegation, lock discipline).

Cross-cutting concerns:

- Handlers go through ``_ensure_uri_loaded(ls, uri)`` first so tab switches
  don't leave them querying a stale workset.
- Tree-walking handlers that read the *cached* tree (hover, definition, inlay)
  acquire ``state.ts_handler_lock`` so they can't race on tree-sitter's
  not-thread-safe traversal. Handlers that parse a *fresh* tree (interactions,
  panel) and code-action do not.
- Mutations of ``state.last_result`` / ``state.workspace_index`` /
  ``state.doc_versions`` / ``state.opened_uris`` are guarded by the matching
  ``state.*_lock`` and never accessed without it.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from dimfort import __version__
from dimfort.config import load_config
from dimfort.core import (
    ts_checker,
    unit_config,  # noqa: F401  populates DEFAULT_TABLE
)
from dimfort.core import ts_parser as _ts
from dimfort.core._source_io import FORTRAN_EXTS as _FORTRAN_EXTS
from dimfort.core.cache_store import CacheStore
from dimfort.core.diagnostics import (
    Diagnostic,
    Severity,
    set_severity_overrides,
)
from dimfort.core.multifile import check_files
from dimfort.core.workspace_index import (
    resolve_workset,
    scan_workspace,
    update_index,
)
from dimfort.lsp import (
    code_action,
    completion,
    definition,
    inlay,
    interactions,
    panel,
)
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.code_action import (
    _h010_extract_to_parameter_actions as _h010_extract_to_parameter_actions,
)
from dimfort.lsp.expr_tree import (
    _build_expression_tree as _build_expression_tree,
)
from dimfort.lsp.expr_tree import (
    _build_scope_vars as _build_scope_vars,
)
from dimfort.lsp.expr_tree import _node_marker
from dimfort.lsp.hover_render import (
    _hover_signature,
    _hover_text,
    _module_hover_md,
    _unit_pretty,
)
from dimfort.lsp.interactions import (
    _serialize_interaction_point as _serialize_interaction_point,
)
from dimfort.lsp.markers import (
    _aggregate_marker,
)
from dimfort.lsp.markers import (
    _marker_token as _marker_token,
)
from dimfort.lsp.state import DEFAULT_EXTERNAL_MODULES, state
from dimfort.lsp.tree_access import (
    _build_ts_ctx,
    _trees_for,
    _uri_for_path,
    _uri_to_path,
)
from dimfort.lsp.tree_nav import (
    _SKIP_TOKEN_TYPES,
    _interesting_children,
    _node_label,
    _node_lsp_range,
)
from dimfort.lsp.tree_nav import (
    _enclosing_scopes as _enclosing_scopes,
)
from dimfort.lsp.tree_nav import (
    _find_expression_root as _find_expression_root,
)
from dimfort.lsp.tree_nav import (
    _identifier_at as _identifier_at,
)
from dimfort.lsp.tree_nav import (
    _normalized_unit as _normalized_unit,
)
from dimfort.lsp.tree_nav import (
    _scope_header as _scope_header,
)
from dimfort.lsp.tree_nav import (
    _smallest_enclosing_scope as _smallest_enclosing_scope,
)

log = logging.getLogger("dimfort.lsp")

server = LanguageServer("dimfort", __version__)


def _notify(ls: LanguageServer | None, message: str, *, toast: bool = False) -> None:
    """Surface a progress message to both the Python logger and the
    LSP client's output channel. Pygls filters ``log.info`` below
    WARNING by default, so user-relevant events would otherwise be
    invisible in VSCode's "DimFort Language Server" output channel.
    Pass ``toast=True`` for unblock signals worth a status-bar popup.
    """
    log.info(message)
    if ls is None:
        return
    try:
        ls.window_log_message(
            lsp.LogMessageParams(type=lsp.MessageType.Info, message=message)
        )
        if toast:
            ls.window_show_message(
                lsp.ShowMessageParams(type=lsp.MessageType.Info, message=message)
            )
    except Exception:
        log.debug("window/logMessage failed", exc_info=True)


# ---------------------------------------------------------------------------
# Feature toggles (set from initializationOptions; off-by-default flags
# would surprise users, so everything defaults on).
# ---------------------------------------------------------------------------


class _FeatureToggles:
    inlay_hints: bool = True
    completion: bool = True
    code_actions: bool = True
    goto_definition: bool = True
    # Hover verbosity, a single tri-state applied to every hover surface
    # (call pairing, expression, variable):
    #   "disabled" — no hover at all (the panel is the unit surface)
    #   "short"    — one-line summary
    #   "detailed" — full pairing / unit-algebra tree
    # The side panel is unaffected: it is always detailed, governed only
    # by its own open/closed state.
    hover: str = "short"   # "disabled" | "short" | "detailed"


_features = _FeatureToggles()


_SEVERITY_TO_LSP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}

# Shared mutable server state (locks, caches, config) lives on the single
# ``state`` object imported above. See ``lsp/state.py`` for the concurrency
# contract — in particular ``state.ts_handler_lock`` and ``state.check_lock``.

def _cap_workset(
    paths: list[Path], active: Path, limit: int,
    *, must_keep: frozenset[Path] = frozenset(),
) -> list[Path]:
    """Trim a workset down to ``limit`` entries while keeping the active
    file and any explicit ``must_keep`` entries.

    Topo order alone isn't enough on real codebases: a direct
    callee that DimFort pulled in via the procedure index can sit
    early in the topo sort (its own deps are shallow) and get
    dropped by a naive ``paths[-limit:]`` cap, even though it's
    semantically central to the active file. ``must_keep`` lets the
    caller pin those entries.

    Algorithm: keep the active file plus every ``must_keep`` entry,
    then fill the remaining budget from the topo-last entries that
    aren't already pinned.
    """
    if len(paths) <= limit:
        return paths
    pinned: set[Path] = {active} | {p for p in must_keep if p in paths}
    if len(pinned) >= limit:
        # Pinned set already exceeds the cap; keep all pins, drop
        # everything else. ``active`` is always present.
        return [p for p in paths if p in pinned]
    budget = limit - len(pinned)
    out_set: set[Path] = set(pinned)
    # Walk paths from the end backwards, picking topo-last entries
    # that aren't already pinned, until the budget is exhausted.
    for p in reversed(paths):
        if budget == 0:
            break
        if p in out_set:
            continue
        out_set.add(p)
        budget -= 1
    # Return in topo order so downstream consumers (multifile)
    # process deps before users.
    return [p for p in paths if p in out_set]


def _remember_uri(uri: str) -> None:
    p = _uri_to_path(uri)
    if p is None:
        return
    try:
        resolved = p.resolve()
    except OSError:
        return
    with state.opened_uris_lock:
        state.opened_uris[resolved] = uri


def _forget_uri(uri: str) -> None:
    p = _uri_to_path(uri)
    if p is None:
        return
    try:
        resolved = p.resolve()
    except OSError:
        return
    with state.opened_uris_lock:
        state.opened_uris.pop(resolved, None)


# ---------------------------------------------------------------------------
# URI / position helpers
# ---------------------------------------------------------------------------


def _to_lsp_diagnostic(d: Diagnostic) -> lsp.Diagnostic:
    start_line = max(d.start.line - 1, 0)
    start_col = max(d.start.column - 1, 0)
    end_line = max(d.end.line - 1, 0)
    end_col = max(d.end.column - 1, 0)
    if (end_line, end_col) <= (start_line, start_col):
        end_col = start_col + 1
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=start_line, character=start_col),
            end=lsp.Position(line=end_line, character=end_col),
        ),
        severity=_SEVERITY_TO_LSP.get(d.severity, lsp.DiagnosticSeverity.Error),
        code=d.code,
        source="DimFort",
        message=d.message,
    )


# ---------------------------------------------------------------------------
# Workspace traversal
# ---------------------------------------------------------------------------


def _discover_fortran_files(roots: list[Path]) -> list[Path]:
    """Walk every workspace folder and collect Fortran sources."""
    out: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in _FORTRAN_EXTS:
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
    return out


def _workset_for(ls: LanguageServer, active_uri: str) -> tuple[list[Path], Path | None]:
    """Return the workset of paths plus the active path (if known).

    Uses the workspace index to follow ``use``-statement dependencies
    from the active file when the index is ready. Falls back to the
    whole-workspace scan otherwise (initial-check window before the
    index finishes building, or no workspace folders configured).

    Always includes the active file even if it lives outside any
    workspace folder (e.g. the user opened a loose ``.f90``).
    """
    active = _uri_to_path(active_uri)
    if active is None or not active.is_file():
        return [], active

    with state.workspace_index_lock:
        idx = state.workspace_index

    resolved_active = active.resolve()

    if idx is not None:
        res = resolve_workset(
            idx, [resolved_active], external_modules=state.external_modules
        )
        paths = list(res.compile_order)
        # Belt-and-braces: ensure the active file is present even if it
        # lives outside the indexed roots (e.g. a loose `.f90` outside
        # any workspace folder).
        if resolved_active not in paths:
            paths.append(resolved_active)
        # Pin the active file's direct dependencies (modules used,
        # procedures called) so the cap doesn't drop them. Without
        # this, an external callee can land mid-topo and get cut.
        must_keep: set[Path] = set()
        for use in idx.uses_by_file.get(resolved_active, ()):
            tgt = idx.modules.get(use.module)
            if tgt is not None:
                must_keep.add(tgt)
        for callee in idx.calls_by_file.get(resolved_active, ()):
            tgt = idx.procedures.get(callee)
            if tgt is not None:
                must_keep.add(tgt)
        # Cap to keep the LSP process alive on deep workspaces.
        capped = _cap_workset(
            paths, resolved_active, state.max_workset_size,
            must_keep=frozenset(must_keep),
        )
        if len(capped) < len(paths):
            _notify(
                ls,
                f"DimFort: workset capped at {state.max_workset_size} "
                f"(full deps: {len(paths)}) for {resolved_active.name}",
            )
        return capped, active

    # Fallback: index not ready yet, or no workspace folders. Just
    # check the active file alone. Cross-file deps will surface as
    # transient U007 errors until the index build finishes; that's
    # strictly better than feeding every file in the workspace to
    # the checker — at large scale (~2400 files) the old behaviour
    # SIGKILLed the LSP via macOS jetsam.
    return [resolved_active], active


# ---------------------------------------------------------------------------
# Diagnostic publication
# ---------------------------------------------------------------------------


def _publish_for_uri(ls: LanguageServer, uri: str, *, override_text: str | None = None) -> None:
    paths, active = _workset_for(ls, uri)
    if active is None:
        return
    overrides: dict[Path, str] = {}
    if override_text is not None:
        overrides[active.resolve()] = override_text

    try:
        result = check_files(
            paths,
            overrides=overrides,
            external_modules=state.external_modules,
            cpp_defines=state.project_config.cpp_defines,
            include_paths=state.project_config.include_paths,
            cache=state.cache,
            cache_mode=state.cache_mode,
            units_file=state.project_config.units_file,
            diagnostic_severities=state.project_config.diagnostic_severities,
            scale_mode=state.scale_mode,
        )
    except Exception:
        log.exception("dimfort pipeline crashed on %s", active)
        return

    with state.last_result_lock:
        state.last_result = result

    # Publish per-file. Files that produced no diagnostics still get an
    # empty publish, so stale squiggles clear immediately.
    for path in paths:
        diags = result.diagnostics.get(path, [])
        try:
            file_uri = _uri_for_path(path)
        except ValueError:
            continue
        ls.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(
                uri=file_uri,
                diagnostics=[_to_lsp_diagnostic(d) for d in diags],
            )
        )
    _refresh_inlay_hints(ls)


def _refresh_inlay_hints(ls: LanguageServer) -> None:
    """Ask the client to re-query inlay hints for every open buffer.

    The client may issue a ``textDocument/inlayHint`` request *before*
    the server's initial workspace check has populated
    ``state.last_result``; that early request returns empty and the
    client caches "no hints". Without this nudge the user has to
    perform a buffer edit to coax the client into re-querying. The
    method is opt-in via the LSP spec (``workspace.inlayHint.refreshSupport``),
    so we fire it unconditionally and let the framework drop it when
    the client didn't advertise support.
    """
    with contextlib.suppress(Exception):
        ls.workspace_inlay_hint_refresh(None)


def _bump_version(uri: str) -> int:
    with state.doc_versions_lock:
        state.doc_versions[uri] = state.doc_versions.get(uri, 0) + 1
        return state.doc_versions[uri]


def _is_current(uri: str, version: int) -> bool:
    with state.doc_versions_lock:
        return state.doc_versions.get(uri) == version


# ---------------------------------------------------------------------------
# Tree access for handlers (workset-cache lookup + ctx builder)
# ---------------------------------------------------------------------------


def _ensure_uri_loaded(ls: LanguageServer, uri: str) -> None:
    """Re-publish for ``uri`` if its tree isn't in ``state.last_result``.

    The LSP keeps a single global ``state.last_result``, updated on every
    didOpen / didSave / didChange. When the user navigates between
    open tabs, VSCode doesn't fire any LSP event — but the last
    publish may have been for a *different* active file whose
    workset doesn't include the now-active one (typical when the
    user jumped from a caller to a callee via goto-def: the
    callee's workset is downward-only and doesn't loop back).

    Detect that by asking ``_trees_for`` and, if it returns None
    for what's actually a known Fortran file, fire a synchronous
    publish for the URI so the next hover / goto-def / inlay
    request sees fresh trees.
    """
    if _trees_for(uri) is not None:
        return
    path = _uri_to_path(uri)
    if path is None or not path.is_file():
        return
    if path.suffix.lower() not in _FORTRAN_EXTS:
        return
    with state.check_lock:
        _publish_for_uri(ls, uri)


# ---------------------------------------------------------------------------
# Hover dispatch (tree-sitter)
# ---------------------------------------------------------------------------


def _resolve_hover(
    uri: str,
    line_1based: int,
    col_1based: int,
    source_text: str | None,  # accepted for caller compatibility; unused
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
        unit = ts_checker._resolve_member_chain(member_hit, ctx, source)
        base, path = _ts_h.member_expr_chain(member_hit, source)
        if base is not None and path:
            display = f"{base}%{'%'.join(path)}"
            return _hover_text(display, _unit_pretty(unit)), _node_lsp_range(member_hit)

    # 3. Call expression / subroutine call.
    call_hit = _ts_h.smallest_enclosing(
        _ts_h.walk_calls(tree), line_1based, col_1based
    )
    if call_hit is not None:
        name = _ts_h.call_name(call_hit, source)
        if name is not None:
            sig = result.signatures.get(name.lower())
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
                    level = _features.hover
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
                            name, call_hit, sig, rctx, source,
                        )
                    else:
                        text = _render_call_pairing_a(
                            name, call_hit, sig, rctx, source,
                        )
                    if text is None:
                        text = _hover_signature(name, sig)
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
            name_lc = name.lower()
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
                unit = ts_checker._resolve(call_hit, ctx, source)
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
    ident_ctx: ts_checker._Ctx | None = None
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
            source = _unit_source_for(
                result, resolved_path, name, ident_ctx.scope_at(ident.start_byte),
            )
            return (
                _hover_text(name, _unit_pretty(unit), unit_source=source),
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
        u = ts_checker._resolve(n, ctx, source)
        u_s = format_unit(u) if u is not None else "1"
        body = f"{_node_label(n, source)} : {u_s}"
        text = f"**🟢 DimFort**\n\n```\n{body}\n```"
        return text, _node_lsp_range(n)
    return None


def _unit_source_for(
    result, resolved_path: Path, name: str, scope_lc: str | None,
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
    sources = getattr(attached, "var_unit_sources", None)
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


# ---------------------------------------------------------------------------
# Lifecycle handlers (initialize, initialized, background index build)
# ---------------------------------------------------------------------------


@server.feature(lsp.INITIALIZE)
def _initialize(ls: LanguageServer, params: lsp.InitializeParams) -> None:
    folders: list[Path] = []
    if params.workspace_folders:
        for folder in params.workspace_folders:
            p = _uri_to_path(folder.uri)
            if p is not None:
                folders.append(p)
    elif params.root_uri:
        p = _uri_to_path(params.root_uri)
        if p is not None:
            folders.append(p)
    state.workspace_folders = folders

    # Load .dimfort.toml from the first workspace folder, if any.
    if folders:
        state.project_config = load_config(folders[0])
    config = state.project_config

    # Project-specific unit table (projects ship a ``*_units.toml`` with
    # ``degree``, ``hPa``, ``day``, etc.). Install before any check
    # fires so var_units parsing doesn't drop those annotations.
    if config.units_file is not None:
        from dimfort.core import unit_config as _unit_config
        _unit_config.install_default(config.units_file)

    # Start from config-provided values; initializationOptions override.
    _external_modules_from_config = DEFAULT_EXTERNAL_MODULES | frozenset(
        config.external_modules
    )
    opts = params.initialization_options or {}
    state.external_modules = _external_modules_from_config
    if config.max_workset_size is not None:
        state.max_workset_size = config.max_workset_size
    # Scale mode: config default, optionally overridden per-client.
    state.scale_mode = config.scale_mode

    # [diagnostics] severity overrides are applied by finalize_diagnostics
    # via a process-wide global. The CLI sets it; the LSP must too, or
    # editor diagnostics silently ignore every [diagnostics] override.
    set_severity_overrides(config.diagnostic_severities)

    if isinstance(opts, dict):
        _features.inlay_hints = bool(opts.get("inlayHintsEnabled", True))
        _features.completion = bool(opts.get("completionEnabled", True))
        _features.code_actions = bool(opts.get("codeActionsEnabled", True))
        _features.goto_definition = bool(opts.get("gotoDefinitionEnabled", True))
        # Single tri-state hover verbosity. Accept the legacy
        # ``traceHoverEnabled`` / per-surface keys defensively in case an
        # older client connects, but the modern key is ``hover``.
        hv = opts.get("hover")
        if hv in ("disabled", "short", "detailed"):
            _features.hover = hv
        else:
            # Back-compat with pre-1.x clients: traceHoverEnabled=true or
            # any per-surface "detailed" means detailed; else short.
            legacy_detailed = bool(opts.get("traceHoverEnabled", False)) or any(
                opts.get(k) == "detailed"
                for k in ("hoverFunctionCalls", "hoverSubroutineCalls",
                          "hoverExpressions")
            )
            _features.hover = "detailed" if legacy_detailed else "short"
        # Init options extend whatever config already contributed.
        extra = opts.get("externalModules")
        if isinstance(extra, list):
            state.external_modules = state.external_modules | frozenset(
                str(m).lower() for m in extra if isinstance(m, str)
            )
        cap = opts.get("maxWorksetSize")
        if isinstance(cap, int) and cap > 0:
            state.max_workset_size = cap

        # Opt-in scale checking: initializationOption overrides config.
        scale_opt = opts.get("scaleMode")
        if isinstance(scale_opt, bool):
            state.scale_mode = scale_opt

        # Content-hash cache: opt-in via initializationOptions. The
        # cache directory defaults to ``.dimfort-cache/`` under the
        # first workspace folder; clients can override with cacheDir.
        requested = opts.get("cacheMode", "off")
        if requested in ("off", "read-only", "read-write") and folders:
            state.cache_mode = requested
            if requested != "off":
                from dimfort.core.cache_store import default_cache_dir
                cache_dir_opt = opts.get("cacheDir")
                cache_root = (
                    Path(cache_dir_opt) if isinstance(cache_dir_opt, str)
                    else default_cache_dir(folders[0])
                )
                state.cache = CacheStore(root=cache_root)

    config_note = (
        f" (config: {config.config_path.name})"
        if config.config_path is not None
        else ""
    )
    _notify(
        ls,
        f"DimFort LSP initialised — {len(folders)} folder(s), "
        f"{len(state.external_modules)} external module(s) on allowlist"
        f"{config_note}",
    )


@server.feature(lsp.INITIALIZED)
def _initialized(ls: LanguageServer, params: lsp.InitializedParams) -> None:
    """Kick off the workspace scan once the client is ready.

    The workspace scan needs to send server-to-client requests
    (``window/workDoneProgress/create``); these are only valid after the
    client has sent the ``initialized`` notification. Spawning earlier —
    e.g. from inside the ``initialize`` handler — races against the
    client's readiness and produces JsonRpcMethodNotFound responses.
    """
    folders = state.workspace_folders
    if not folders:
        return
    # If ``.dimfort.toml`` narrows the source tree via [project].src_paths,
    # scan only those subdirectories. Otherwise scan every workspace
    # folder. Useful on large monorepos where DimFort cares about a
    # handful of subtrees.
    scan_roots = tuple(state.project_config.src_paths) or tuple(folders)
    _notify(
        ls,
        f"DimFort: scanning workspace ({len(scan_roots)} root(s))…",
    )
    threading.Thread(
        target=_build_initial_index,
        args=(ls, scan_roots),
        daemon=True,
        name="dimfort-workspace-scan",
    ).start()


def _build_initial_index(ls: LanguageServer, roots: tuple[Path, ...]) -> None:
    """Background scan; assigns the result to the module-level index.

    Emits ``$/progress`` notifications so VSCode shows a status-bar
    spinner with per-file detail. Reports are throttled to ~10/sec so
    a 2435-file scan doesn't flood the wire.
    """
    token = f"dimfort-scan-{int(time.time() * 1000)}"
    progress = ls.work_done_progress
    progress_started = False
    try:
        progress.create(token).result(timeout=2.0)
        progress.begin(
            token,
            lsp.WorkDoneProgressBegin(
                title="DimFort: scanning workspace",
                cancellable=False,
                percentage=0,
            ),
        )
        progress_started = True
    except Exception:
        log.debug("could not start workDoneProgress", exc_info=True)

    last_report_at = 0.0

    def on_progress(scanned: int, total: int, path: Path) -> None:
        nonlocal last_report_at
        if not progress_started:
            return
        now = time.monotonic()
        # Always report the final tick; throttle the rest to ~10/sec.
        if scanned != total and now - last_report_at < 0.1:
            return
        last_report_at = now
        try:
            progress.report(
                token,
                lsp.WorkDoneProgressReport(
                    message=f"{scanned}/{total} {path.name}",
                    percentage=int(scanned * 100 / total) if total else 100,
                ),
            )
        except Exception:
            log.debug("workDoneProgress report failed", exc_info=True)

    try:
        idx = scan_workspace(roots, progress_cb=on_progress)
    except Exception:
        log.exception("workspace index build failed")
        if progress_started:
            try:
                progress.end(token, lsp.WorkDoneProgressEnd(message="failed"))
            except Exception:
                log.debug("workDoneProgress end failed", exc_info=True)
        return

    with state.workspace_index_lock:
        state.workspace_index = idx

    if progress_started:
        try:
            progress.end(token, lsp.WorkDoneProgressEnd(message="done"))
        except Exception:
            log.debug("workDoneProgress end failed", exc_info=True)

    _notify(
        ls,
        f"DimFort workspace index ready: {len(idx.modules)} modules "
        f"across {len(idx.uses_by_file)} files",
        toast=True,
    )

    # Re-check every currently-open file. Without this, files opened
    # during the scan published diagnostics under the single-file
    # fallback workset (their use-deps surfacing as bogus U007s) and
    # nothing ever re-triggered them. Now that the index is in
    # place, ``_workset_for`` will resolve full topo-sorted
    # closures.
    with state.opened_uris_lock:
        opened = list(state.opened_uris.values())
    if opened:
        _notify(
            ls,
            f"DimFort: refreshing diagnostics for {len(opened)} open file(s)",
        )
        for uri in opened:
            try:
                with state.check_lock:
                    _publish_for_uri(ls, uri)
            except Exception:
                log.exception("post-index refresh failed for %s", uri)


def _update_index_for(path: Path, *, new_text: str | None = None) -> None:
    """Incrementally re-scan one file into the index. No-op when the
    initial build hasn't completed."""
    with state.workspace_index_lock:
        idx = state.workspace_index
    if idx is None:
        return
    try:
        update_index(idx, path, new_text=new_text)
    except Exception:
        log.exception("workspace index update failed for %s", path)


# ---------------------------------------------------------------------------
# Document-sync handlers (did_open / did_save / did_close / did_change)
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def _did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)

    def worker() -> None:
        with state.check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didOpen check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-open").start()


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def _did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)
    saved = _uri_to_path(uri)
    if saved is not None:
        _update_index_for(saved.resolve())

    def worker() -> None:
        with state.check_lock:
            try:
                _publish_for_uri(ls, uri)
            except Exception:
                log.exception("didSave check failed for %s", uri)

    threading.Thread(target=worker, daemon=True, name="dimfort-save").start()


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def _did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    uri = params.text_document.uri
    path = _uri_to_path(uri)
    _forget_uri(uri)

    # DimFort is a workspace-wide checker, so a file's diagnostics
    # remain true after the user closes it. Republish the cached
    # workspace-check diagnostics instead of clearing them; clear only
    # if we have nothing on file (e.g. a single-file workset whose
    # entry no longer applies).
    cached: list[Diagnostic] = []
    if path is not None:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = None
        if resolved is not None:
            with state.last_result_lock:
                result = state.last_result
            if result is not None:
                cached = result.diagnostics.get(resolved, [])

    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(
            uri=uri, diagnostics=[_to_lsp_diagnostic(d) for d in cached]
        )
    )


_DEBOUNCE_SECONDS = 0.4


@server.feature(lsp.TEXT_DOCUMENT_DID_CHANGE)
def _did_change(ls: LanguageServer, params: lsp.DidChangeTextDocumentParams) -> None:
    uri = params.text_document.uri
    _remember_uri(uri)
    version = _bump_version(uri)

    # Pygls keeps a TextDocument with the up-to-date buffer source.
    doc = ls.workspace.get_text_document(uri)
    text = doc.source

    def delayed() -> None:
        time.sleep(_DEBOUNCE_SECONDS)
        if not _is_current(uri, version):
            return  # superseded by a later keystroke
        with state.check_lock:
            # Re-check version inside the lock: a later keystroke may
            # have arrived while we were waiting for our turn.
            if not _is_current(uri, version):
                return
            try:
                # Reflect the in-memory buffer in the index so a freshly
                # added `use M` is picked up on the same keystroke.
                active = _uri_to_path(uri)
                if active is not None:
                    _update_index_for(active.resolve(), new_text=text)
                _publish_for_uri(ls, uri, override_text=text)
            except Exception:
                log.exception("debounced check failed for %s", uri)

    threading.Thread(target=delayed, daemon=True).start()


# ---------------------------------------------------------------------------
# Hover handler (registration; dispatch logic lives in _resolve_hover above)
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_HOVER)
def _hover(ls: LanguageServer, params: lsp.HoverParams) -> Any:
    # Hover disabled entirely (the panel is the unit surface). Bail
    # before doing any work.
    if _features.hover == "disabled":
        return None
    # Tab switches to a different open document don't fire any LSP
    # event, but their workset may not include the now-active file.
    # Trigger a fresh publish before reading trees.
    _ensure_uri_loaded(ls, params.text_document.uri)
    with state.ts_handler_lock:
        uri = params.text_document.uri
        # LSP positions are 0-based; our internal helpers are 1-based.
        line = params.position.line + 1
        col = params.position.character + 1
        source_text: str | None = None
        try:
            source_text = ls.workspace.get_text_document(uri).source
        except Exception:
            log.debug("could not fetch buffer text for %s", uri)
        # Most-specific wins: try the specific hovers (identifier,
        # member access, call callee) first. The expression hover fires
        # only when nothing more specific matched — that's what catches
        # cursor positions on operators, ``=``, or whitespace inside an
        # assignment or condition.
        hit = _resolve_hover(uri, line, col, source_text)
        if hit is None:
            hit = _expression_hover_for(uri, line, col)
        if hit is None:
            return None
        text, range_ = hit
        return lsp.Hover(
            contents=lsp.MarkupContent(kind=lsp.MarkupKind.Markdown, value=text),
            range=range_,
        )


def _expression_hover_for(
    uri: str, line_1based: int, col_1based: int,
) -> tuple[str, lsp.Range] | None:
    """Expression hover. Fires when no more-specific hover matched
    (i.e. cursor isn't on an identifier or callee). Renders Short or
    Detailed depending on ``_features.hover``.

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
        if _features.hover == "short":
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
    if _features.hover == "short":
        return _render_assignment_short(asn, lhs, rhs, ctx, source)
    rows: list[tuple[str, str | None, str, str]] = []
    lhs_unit = ts_checker._resolve(lhs, ctx, source)
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
    tree, source: bytes, resolved_path, result,
    line_1based: int, col_1based: int,
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
    if _features.hover == "short":
        # Relational expressions are a homogeneity check on their
        # operands — the relation itself has no unit.
        if expr.type == "relational_expression":
            return _render_relational_short(expr, rctx, source)
        return _render_subexpr_short(expr, rctx, source)
    rows: list[tuple[str, str, str, str]] = []
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
    units_present = [r[1] for r in rows if r[1] != ""]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit != "" else ""
        if rule:
            lines.append(f"{head}{mid}  {mark}  {rule}")
        else:
            lines.append(f"{head}{mid}  {mark}".rstrip())
    body = "\n".join(lines)
    header_marker = _aggregate_marker(r[2] for r in rows)
    text = f"**{header_marker} DimFort**\n\n```\n" + body + "\n```"
    return text, _node_lsp_range(expr)


_MATH_OP_TYPES = frozenset({"+", "-", "*", "/", "**"})


def _math_op_at_cursor(tree, line: int, col: int):
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


def _render_mathop_short(math_expr, ctx, source: bytes) -> tuple[str, lsp.Range] | None:
    """One-line homogeneity hover for a ``+`` / ``-`` math expression."""
    from dimfort.core.units import format_unit
    operands = [c for c in math_expr.children if c.type not in _SKIP_TOKEN_TYPES]
    if len(operands) < 2:
        return None
    lhs, rhs = operands[0], operands[1]
    marker = _node_marker(math_expr, ctx, source)
    lu = ts_checker._resolve(lhs, ctx, source)
    ru = ts_checker._resolve(rhs, ctx, source)
    lhs_s = format_unit(lu) if lu is not None else "?"
    rhs_s = format_unit(ru) if ru is not None else "?"
    body = (
        f"{_node_label(lhs, source)} : {lhs_s}"
        f"   ◂   {_node_label(rhs, source)} : {rhs_s}"
    )
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(math_expr)


def _expression_hover_render_tree(
    root, ctx, source: bytes, *, range_node,
) -> tuple[str, lsp.Range] | None:
    """Detailed-mode tree render rooted at ``root``. Shared by the
    operator-specific path and the generic expression-context path."""
    rows: list[tuple[str, str, str, str]] = []
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
    units_present = [r[1] for r in rows if r[1] != ""]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit != "" else ""
        if rule:
            lines.append(f"{head}{mid}  {mark}  {rule}")
        else:
            lines.append(f"{head}{mid}  {mark}".rstrip())
    body = "\n".join(lines)
    header_marker = _aggregate_marker(r[2] for r in rows)
    text = f"**{header_marker} DimFort**\n\n```\n" + body + "\n```"
    return text, _node_lsp_range(range_node)


_VERDICT_TO_MARKER = {
    "homogeneous": "🟢",
    "autocast": "🟢",
    "wrapper_untag": "🟡",
    "mismatch": "🔴",
    "unresolved": "🟡",
}


def _render_assignment_short(asn, lhs, rhs, ctx, source: bytes) -> tuple[str, lsp.Range] | None:
    """One-line homogeneity hover for an assignment cursor position.

    Delegates the assignment-specific logic (autocast detection,
    unit comparison) to ``ts_checker._assignment_homogeneity`` — the
    single source of truth shared with the panel and the checker."""
    from dimfort.core.units import format_unit
    verdict, lhs_u, rhs_u = ts_checker._assignment_homogeneity(
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


def _render_relational_short(rel, ctx, source: bytes) -> tuple[str, lsp.Range] | None:
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
    lhs_u = ts_checker._resolve(lhs, ctx, source)
    rhs_u = ts_checker._resolve(rhs, ctx, source)
    lhs_s = format_unit(lhs_u) if lhs_u is not None else "?"
    rhs_s = format_unit(rhs_u) if rhs_u is not None else "?"
    body = (
        f"{_node_label(lhs, source)} : {lhs_s}"
        f"   ◂   {_node_label(rhs, source)} : {rhs_s}"
    )
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(rel)


def _render_subexpr_short(expr, ctx, source: bytes) -> tuple[str, lsp.Range] | None:
    """One-line resolved-unit hover for a computed sub-expression or
    a numeric literal. Marker uses propagated-mark logic so a nested
    homogeneity violation surfaces as 🔴 even though the wrapping
    operator has no unit either."""
    from dimfort.core.units import format_unit
    u = ts_checker._resolve(expr, ctx, source)
    marker = _node_marker(expr, ctx, source)
    u_s = format_unit(u) if u is not None else "?"
    body = f"{_node_label(expr, source)} : {u_s}"
    text = f"**{marker} DimFort**\n\n```\n{body}\n```"
    return text, _node_lsp_range(expr)


def _call_actual_args(call_node) -> list:
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
    callee_name: str, call_node, sig, rctx, source: bytes,
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
            aunit = ts_checker._resolve(an, rctx, source)
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
    callee_name: str, call_node, sig, rctx, source: bytes,
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
            aunit = ts_checker._resolve(an, rctx, source)
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
            sub_rows: list[tuple[str, str, str, str]] = []
            _render_ast_tree(
                an, rctx, source,
                prefix="", is_last=True, is_root=True, rows=sub_rows,
            )
            # Drop the root row (== the actual cell we already render);
            # keep only the descendants.
            if len(sub_rows) > 1:
                max_l = max(len(r[0]) for r in sub_rows[1:])
                max_u = max(len(r[1]) for r in sub_rows[1:])
                for label, unit, mk, rule in sub_rows[1:]:
                    if rule:
                        sub_lines.append(
                            f"      {label.ljust(max_l)}  :  {unit.ljust(max_u)}  {mk}  {rule}"
                        )
                    else:
                        sub_lines.append(
                            f"      {label.ljust(max_l)}  :  {unit.ljust(max_u)}  {mk}".rstrip()
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


def _checker_equal(a, b) -> bool:
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
    rows: list[tuple[str, str, str, str]] = []  # (label, unit, mark, rule)
    _render_ast_tree(rhs, ctx, source, prefix="", is_last=True, is_root=True, rows=rows)
    if not rows:
        return None
    max_label = max(len(r[0]) for r in rows)
    # ``unit`` of ``""`` marks a row that should not display a unit at
    # all (e.g. the assignment_statement row — a statement, not an
    # expression). Compute column width only over rows that DO show
    # a unit; unit-less rows skip the ``: unit`` block entirely.
    units_present = [r[1] for r in rows if r[1] != ""]
    max_unit = max((len(u) for u in units_present), default=0)
    lines: list[str] = []
    for label, unit, mark, rule in rows:
        head = label.ljust(max_label)
        mid = f"  :  {unit.ljust(max_unit)}" if unit != "" else ""
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


def _pick_trace_subexpr(ctx_node, line: int, col: int):
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
    node, ctx, source: bytes,
    *,
    prefix: str, is_last: bool, is_root: bool,
    rows: list[tuple[str, str, str]],
    target_unit_for_literal=None,
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
        unit = ts_checker._resolve(node, ctx, source)
    snap = trace.snapshot()
    rule_id = snap[-1].rule_id if snap else None

    # Initialization autocast: a pure-numeric-constant subtree (literal,
    # unary-minus literal, math of literals) in a propagated target
    # context takes on the target unit and is marked 🟢. Uses the same
    # predicate as the checker's R4.4 — :func:`_is_pure_numeric_constant`
    # — so all three sites (checker, hover, panel) agree on the set of
    # nodes that autocast.
    apply_autocast = (
        target_unit_for_literal is not None
        and ts_checker._is_pure_numeric_constant(node)
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
    # - Assignment: ask ``_assignment_homogeneity`` for the effective
    #   RHS unit; pass it to the last child (the RHS) when the verdict
    #   says we're in autocast mode.
    # - Unary-minus: if THIS node is already being autocast (i.e. it's
    #   a unary-minus wrapping a literal in an autocast context), pass
    #   the target through to the inner literal.
    child_target = None
    if node.type == "assignment_statement" and children:
        verdict, lhs_u, _ = ts_checker._assignment_homogeneity(
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


# ---------------------------------------------------------------------------
# Side-panel info — structured-tree builders for the dimfort/panelInfo
# request. The two functions below mirror _render_ast_tree's resolution
# logic but return data instead of rendered strings, so each editor's
# side panel can lay it out in its own idiom (Nvim split, Emacs window,
# VSCode webview). See docs/design/panel-info.md.
# ---------------------------------------------------------------------------




@server.feature("dimfort/panelInfo")
def _panel_info(ls: LanguageServer, params) -> dict | None:
    """Return the side-panel payload for ``(uri, position)``.

    See docs/design/panel-info.md for the data model. Stateless:
    reads from the last cached WorksetResult, computes the response
    on the fly.
    """
    return panel.resolve(ls, params)


@server.feature("dimfort/interactions")
def _interactions(ls: LanguageServer, params) -> dict | None:
    """Cross-site unit analysis for the symbol under the cursor.

    Resolves the identifier at ``(uri, position)`` (or an explicit
    ``symbol`` param), then runs :func:`collect_interactions` over the
    cached workset and returns the report. See
    docs/design/interaction-points.md.
    """
    return interactions.resolve(ls, params)


# ---------------------------------------------------------------------------
# Inlay hints
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_INLAY_HINT,
    lsp.InlayHintOptions(resolve_provider=False),
)
def _inlay_hint(
    ls: LanguageServer, params: lsp.InlayHintParams
) -> list[lsp.InlayHint] | None:
    """Inlay hints (``[unit]`` ghost text) at variable uses, calls, and member accesses.

    Walks the visible range only — VSCode requests inlays in the
    currently-on-screen range — and pulls each candidate node through
    the ts_checker resolver so the unit-text matches what the
    diagnostic pipeline computes.
    """
    # Tab-switch safety: re-publish if the URI isn't currently loaded.
    _ensure_uri_loaded(ls, params.text_document.uri)
    if not _features.inlay_hints:
        return None
    # See state.ts_handler_lock declaration: tree-sitter's C library isn't
    # thread-safe for concurrent traversal; serialise alongside hover
    # and definition.
    with state.ts_handler_lock:
        return inlay.resolve(params)


# ---------------------------------------------------------------------------
# Completion inside `@unit{…}`
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_COMPLETION,
    lsp.CompletionOptions(trigger_characters=["{", " ", "/", "*", "^"]),
)
def _completion(
    ls: LanguageServer, params: lsp.CompletionParams
) -> lsp.CompletionList | None:
    if not _features.completion:
        return None
    return completion.complete(ls, params)


# ---------------------------------------------------------------------------
# Go to definition
# ---------------------------------------------------------------------------


@server.feature(lsp.TEXT_DOCUMENT_DEFINITION)
def _definition(
    ls: LanguageServer, params: lsp.DefinitionParams
) -> list[lsp.Location] | None:
    """Go-to-definition.

    Resolves identifiers and call-callees to their declaration site,
    searching every loaded file's tree-sitter tree. Returns the first
    match — F90's case-insensitive name resolution is implemented by
    a lower-cased compare on both ends.
    """
    # Tab-switch safety: re-publish if the URI isn't currently loaded.
    _ensure_uri_loaded(ls, params.text_document.uri)
    if not _features.goto_definition:
        return None
    # See state.ts_handler_lock declaration: tree-sitter's C library isn't
    # thread-safe for concurrent traversal; Cmd-hover fires hover +
    # definition simultaneously and was triggering native-level
    # crashes. Serialise.
    with state.ts_handler_lock:
        return definition.resolve(params)


# ---------------------------------------------------------------------------
# Code action: insert a `!< @unit{}` skeleton on annotation-less decls
# ---------------------------------------------------------------------------


@server.feature(
    lsp.TEXT_DOCUMENT_CODE_ACTION,
    lsp.CodeActionOptions(code_action_kinds=[lsp.CodeActionKind.QuickFix]),
)
def _code_action(
    ls: LanguageServer, params: lsp.CodeActionParams
) -> list[lsp.CodeAction] | None:
    if not _features.code_actions:
        return None
    return code_action.resolve(ls, params)


@server.command("dimfort.checkWorkspace")
def _cmd_check_workspace(ls: LanguageServer, *_args) -> None:
    """Run the active checker backend over every file in the workspace
    index, publishing diagnostics for each. Triggered from the client
    via ``workspace/executeCommand`` (palette command "DimFort: Check
    Whole Workspace").

    The work runs on a daemon thread so the LSP stays responsive. The
    server-wide ``state.check_lock`` is held for the duration to avoid
    racing with per-file didOpen/didSave/didChange checks.
    """
    threading.Thread(
        target=_check_whole_workspace,
        args=(ls,),
        daemon=True,
        name="dimfort-check-workspace",
    ).start()


def _check_whole_workspace(ls: LanguageServer) -> None:
    with state.workspace_index_lock:
        idx = state.workspace_index
    if idx is None:
        _notify(
            ls,
            "DimFort: workspace index not ready yet — wait for the scan "
            "to finish, then try again.",
        )
        return

    files = sorted(idx.uses_by_file.keys())
    if not files:
        _notify(ls, "DimFort: no Fortran files in workspace")
        return

    token = f"dimfort-workspace-check-{int(time.time() * 1000)}"
    progress = ls.work_done_progress
    progress_started = False
    try:
        progress.create(token).result(timeout=2.0)
        progress.begin(
            token,
            lsp.WorkDoneProgressBegin(
                title=f"DimFort: checking workspace ({len(files)} files)",
                cancellable=False,
                percentage=0,
            ),
        )
        progress_started = True
    except Exception:
        log.debug("could not start workDoneProgress for checkWorkspace", exc_info=True)

    _notify(ls, f"DimFort: checking workspace ({len(files)} files)…")

    last_report_at = [0.0]   # mutable via closure
    # Each AST pipeline phase walks every file once. Show the user
    # which phase we're in plus per-file detail so the spinner doesn't
    # look stuck during the post-load passes (which take comparable
    # time on a 2400-file workspace).
    _phase_labels = {
        "load": "loading",
        "index": "indexing modules",
        "check": "checking",
    }

    def on_load_progress(phase: str, scanned: int, total: int, path: Path) -> None:
        if not progress_started:
            return
        now = time.monotonic()
        # Always emit the final tick of each phase; throttle the rest
        # to ~10/sec so we don't flood the wire on a 2400-file workspace.
        if scanned != total and now - last_report_at[0] < 0.1:
            return
        last_report_at[0] = now
        label = _phase_labels.get(phase, phase)
        with contextlib.suppress(Exception):
            progress.report(
                token,
                lsp.WorkDoneProgressReport(
                    message=f"{label} {scanned}/{total} {path.name}",
                    percentage=int(scanned * 100 / total) if total else 100,
                ),
            )

    with state.check_lock:
        try:
            result = check_files(
                files,
                external_modules=state.external_modules,
                cpp_defines=state.project_config.cpp_defines,
                include_paths=state.project_config.include_paths,
                progress_cb=on_load_progress,
                cache=state.cache,
                cache_mode=state.cache_mode,
                units_file=state.project_config.units_file,
                diagnostic_severities=state.project_config.diagnostic_severities,
                scale_mode=state.scale_mode,
            )
        except Exception:
            log.exception("workspace check failed")
            if progress_started:
                with contextlib.suppress(Exception):
                    progress.end(
                        token, lsp.WorkDoneProgressEnd(message="failed")
                    )
            return

        with state.last_result_lock:
            state.last_result = result

        published = 0
        for path in files:
            diags = result.diagnostics.get(path, [])
            try:
                file_uri = _uri_for_path(path)
            except ValueError:
                continue
            ls.text_document_publish_diagnostics(
                lsp.PublishDiagnosticsParams(
                    uri=file_uri,
                    diagnostics=[_to_lsp_diagnostic(d) for d in diags],
                )
            )
            published += 1
            # Throttle progress reports the same way the scan does.
            if progress_started and (published % 100 == 0 or published == len(files)):
                with contextlib.suppress(Exception):
                    progress.report(
                        token,
                        lsp.WorkDoneProgressReport(
                            message=f"published {published}/{len(files)}",
                            percentage=int(published * 100 / len(files)),
                        ),
                    )

    if progress_started:
        with contextlib.suppress(Exception):
            progress.end(token, lsp.WorkDoneProgressEnd(message="done"))

    _refresh_inlay_hints(ls)

    h_count = sum(
        1 for diags in result.diagnostics.values() for d in diags
        if d.code.startswith("H")
    )
    u_count = sum(
        1 for diags in result.diagnostics.values() for d in diags
        if d.code.startswith("U")
    )
    total_seconds = result.phase_timings.get("total")
    timing = f" in {total_seconds:.1f} s" if total_seconds is not None else ""
    cache_note = ""
    if state.cache is not None and (
        result.cache_hits or result.cache_misses or result.cache_dirty
    ):
        cache_note = (
            f" [cache: {result.cache_hits} hit / "
            f"{result.cache_misses} miss / {result.cache_dirty} dirty]"
        )
    _notify(
        ls,
        f"DimFort workspace check complete: {len(files)} files, "
        f"{h_count} H-diags, {u_count} U-diags{timing}{cache_note}",
        toast=True,
    )


def run_stdio() -> None:
    # Raise DimFort's own log level so progress messages emitted via
    # ``log.info`` reach handlers. Pygls's root threshold is WARNING;
    # without this, namespace-scoped INFO logs would be silently
    # dropped before reaching the client's output channel.
    logging.getLogger("dimfort").setLevel(logging.INFO)
    _install_crash_trace_hook()
    server.start_io()


def _install_crash_trace_hook() -> None:
    """Wire ``sys.excepthook`` + ``threading.excepthook`` to a crash log.

    LSP stdio mode can lose stderr (the client doesn't forward it,
    pygls may not flush before process death, etc.). Writing
    tracebacks to a known file makes the next silent crash
    actionable. Default log path is ``/tmp/dimfort-lsp.crash``;
    override via ``DIMFORT_CRASH_LOG`` env var. Disable entirely
    with ``DIMFORT_CRASH_LOG=`` (empty value).

    Deliberately does NOT touch asyncio's loop policy — pygls owns
    that, and meddling with it broke server startup on Python 3.14.
    Most pygls feature handlers run on the asyncio loop, so an
    unhandled exception there typically dies via the loop's default
    handler which prints to stderr. If a future crash isn't caught
    by sys.excepthook + threading.excepthook, the next step is to
    wrap individual feature handlers in try/except locally rather
    than instrument the loop globally.
    """
    import os
    import sys
    import traceback

    env = os.environ.get("DIMFORT_CRASH_LOG")
    if env is None:
        path = "/tmp/dimfort-lsp.crash"
    elif env == "":
        return
    else:
        path = env

    def _write(header: str, body: str) -> None:
        try:
            with open(path, "a") as f:
                f.write(f"\n=== {header} ===\n{body}\n")
                f.flush()
        except Exception:  # noqa: BLE001 — diagnostic path; can't help if write fails
            pass

    def _hook(exc_type, exc_value, exc_tb) -> None:
        _write(
            "sys.excepthook",
            "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )

    sys.excepthook = _hook
    # Threads (pygls runs synchronous features in a thread pool).
    # Without this, exceptions from a worker thread don't reach
    # excepthook on older Pythons.
    if hasattr(threading, "excepthook"):
        def _thread_hook(args):
            _write(
                f"thread {args.thread.name}",
                "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback,
                )),
            )
        threading.excepthook = _thread_hook

    # Pygls wraps feature handler bodies in its own try/except and
    # converts exceptions into JSON-RPC error responses. Those
    # exceptions are logged through the ``pygls`` logger but never
    # reach sys.excepthook / threading.excepthook. Attach a stream
    # handler that mirrors ERROR-level logs into our crash file so
    # we capture them too.
    class _CrashFileHandler(logging.Handler):
        def emit(self, record):  # type: ignore[override]
            try:
                msg = self.format(record)
            except Exception:  # noqa: BLE001
                msg = record.getMessage()
            _write(f"pygls logger {record.name}/{record.levelname}", msg)

    crash_handler = _CrashFileHandler(level=logging.ERROR)
    crash_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s\n"
        )
    )
    # ``pygls`` covers handler-wrap errors; ``asyncio`` covers the
    # loop's default exception handler (which logs unhandled
    # task exceptions). Attach to both.
    logging.getLogger("pygls").addHandler(crash_handler)
    logging.getLogger("asyncio").addHandler(crash_handler)

    _write("startup", "crash hook installed; logging to this file")
