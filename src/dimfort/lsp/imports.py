"""Imported-symbol resolution for the side panel (``imports`` field).

Builds the panel's **Imports / Used modules** section: the variables a
``use`` clause brings into the cursor's scope — names that are usable
where the cursor sits but are *not* lexically declared in any enclosing
scope, so the scope-variable tables don't cover them.

Scoping mirrors Fortran visibility: a ``use`` at module level is visible
to every procedure in the module; a routine-level ``use`` is visible only
in that routine. A clause is in scope for the cursor when the innermost
scope containing the clause also contains the cursor (reusing the scope
spans from ``expr_tree.recover_scopes``).

Units + declaration sites come from the workspace ``module_exports`` and
``trees`` already on the cached ``WorksetResult`` — so a row can navigate
cross-file to where the imported variable (and its ``@unit{}``) is
declared, the same way ``definition.py`` resolves a symbol.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from dimfort.core import ts_parser as _ts
from dimfort.core.units import format_unit
from dimfort.core.workspace_index import extract_uses
from dimfort.lsp import ts_helpers as _ts_h
from dimfort.lsp.expr_tree import _innermost_scope_idx, recover_scopes
from dimfort.lsp.tree_nav import _normalized_unit

if TYPE_CHECKING:
    from tree_sitter import Tree

    from dimfort.core.multifile import WorksetResult


def _resolve_decl_location(
    result: WorksetResult,
    module_lc: str,
    remote_name: str,
    *,
    want_procedure: bool = False,
) -> dict[str, Any] | None:
    """Locate where ``remote_name`` is declared in module ``module_lc``.

    Searches the workset's loaded trees for the module's defining file,
    then for the declaration site inside it — a variable's declaration
    identifier, or (when ``want_procedure`` is set) the function or
    subroutine definition's name. Reuses the same walks
    ``definition.py`` uses for go-to-definition, so a row in the panel
    navigates exactly where ``F12`` on the symbol would.

    Args:
        result: Cached :class:`WorksetResult` carrying the loaded
            tree-sitter trees and source bytes keyed by file path.
        module_lc: Lower-cased module name to search for.
        remote_name: Symbol name to locate inside the module — looked
            up case-insensitively against the declaration site.
        want_procedure: When ``True``, look for a function /
            subroutine definition whose name matches; when ``False``,
            look for a variable declaration identifier.

    Returns:
        A 1-based ``{"file", "line", "column"}`` dict pointing at the
        declaration, or ``None`` when the module file isn't loaded or
        the declaration can't be located. The caller falls back to the
        ``use`` site when ``None`` is returned.
    """
    remote_lc = remote_name.lower()
    for tree_path, (other_tree, other_source) in result.trees.items():
        if not any(
            (nm := _ts_h.module_definition_name(mod, other_source))
            and nm[0].lower() == module_lc
            for mod in _ts_h.walk_module_definitions(other_tree)
        ):
            continue
        # The module's file — find the declaration of ``remote_name``.
        if want_procedure:
            for fn in _ts_h.walk_function_definitions(other_tree):
                fnm = _ts_h.function_definition_name(fn, other_source)
                if fnm is not None and fnm[0].lower() == remote_lc:
                    sr, sc = fnm[1].start_point  # 0-based
                    return {"file": str(tree_path), "line": sr + 1, "column": sc + 1}
        else:
            for _decl, name_node in _ts_h.walk_decl_identifiers(other_tree):
                if _ts.node_text(name_node, other_source).lower() == remote_lc:
                    sr, sc = name_node.start_point  # 0-based
                    return {"file": str(tree_path), "line": sr + 1, "column": sc + 1}
        return None  # module found, declaration not located
    return None


def build_imports(
    tree: Tree,
    source: bytes,
    cursor_line: int,
    result: WorksetResult,
    local_names_lc: frozenset[str],
    *,
    scale_mode: bool = False,
    recovered: tuple[tuple[str, str, int, int], ...] | None = None,
) -> list[dict[str, Any]]:
    """Build the in-scope imported-symbol rows for the side panel.

    Mirrors Fortran ``use``-clause visibility: a clause at module level
    is visible to every procedure in the module, while a routine-level
    ``use`` is visible only in that routine. A clause is considered in
    scope for the cursor when the innermost scope containing the clause
    also contains the cursor (scope spans come from
    :func:`recover_scopes`).

    For each in-scope ``use`` the function walks the module's
    transitive-export closure (variables and procedure signatures),
    skips any name shadowed by a local declaration, and emits one row
    per surviving symbol. Each row's location points at the *original*
    declaration site (resolved via :func:`_resolve_decl_location`) when
    available, so a click on a re-exported name lands on the module
    that actually declared it; the ``use`` line is used as the
    fallback.

    Args:
        tree: Tree-sitter ``program``-rooted :class:`Tree` for the
            current buffer, used for scope recovery and ``use``
            statement extraction.
        source: UTF-8 source bytes for the same buffer; passed to the
            tree-sitter helpers for text extraction.
        cursor_line: 1-based cursor line. Visibility is computed
            against this line.
        result: Cached :class:`WorksetResult` carrying module exports,
            transitive closures, and loaded trees for cross-file
            declaration lookup.
        local_names_lc: Lower-cased names declared locally in any
            scope enclosing the cursor. These shadow imports of the
            same name, so they are dropped from the Imports list (they
            already appear under Scope).
        scale_mode: Forwarded to the unit-normalisation helper so the
            ``unitNormalized`` field reflects the active scale-mode
            toggle.
        recovered: Optional pre-computed output of
            :func:`recover_scopes` for the same ``(tree, source)``.
            Passed by ``panel.resolve`` when its no-scopes fallback
            already ran ``recover_scopes`` so the walk doesn't run
            twice per request. ``None`` (default) → compute locally.

    Returns:
        One ``dict`` per imported symbol in source order. Variable
        rows carry ``{name, unit, unitNormalized, module, kind,
        callable=False, file?, line, column}``. Procedure rows add
        ``callable=True`` and a ``signature`` string of comma-joined
        argument units. Both add ``viaModule`` when the symbol is
        transitively re-exported (``module`` is the origin, ``viaModule``
        is the directly-used module).

    Note:
        Only the ``WorksetResult`` trees are read here; the live buffer
        ``tree`` is used solely for cursor-relative scope detection. No
        tree-sitter handler lock is acquired because the live tree is
        freshly parsed by the caller (``panel.resolve``) and the
        workset trees are accessed read-only by name lookup.
    """
    # Audit #15: accept a pre-computed ``recovered`` from the
    # caller so panel.resolve's fallback branch (which also runs
    # ``recover_scopes`` for the same tree/source) doesn't pay
    # the walk twice. Falls back to computing locally when the
    # caller didn't pre-compute.
    if recovered is None:
        recovered = recover_scopes(tree, source)
    enclosing = {
        i for i, (_k, _n, s, e) in enumerate(recovered) if s <= cursor_line <= e
    }

    # Parse the file's use clauses once (module / only-list / renames),
    # bucketed by module so we can pair each in-scope ``use`` node with
    # its clause in source order (robust to a module used more than once).
    refs_by_mod: dict[str, list[Any]] = defaultdict(list)
    for use_ref in extract_uses(source.decode("utf-8", "replace")):
        refs_by_mod[use_ref.module.lower()].append(use_ref)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()  # local import names already emitted
    for use_node in _ts_h.walk_use_statements(tree):
        nm = _ts_h.use_statement_module_name(use_node, source)
        if nm is None:
            continue
        module_lc = nm[0].lower()
        use_line = _ts.position_for(use_node).line
        # Consume this module's next clause in source order *before* the
        # scope check, so the positional pairing between tree ``use`` nodes
        # and ``extract_uses`` clauses stays aligned even when we skip an
        # out-of-scope use of the same module (e.g. a sibling module's
        # whole-module ``use`` of a module another scope only-imports).
        refs = refs_by_mod.get(module_lc)
        ref = refs.pop(0) if refs else None
        # Visibility: file-level uses (inner is None) and uses whose
        # innermost enclosing scope also encloses the cursor are in scope;
        # a sibling routine's use is not.
        inner = _innermost_scope_idx(use_line, recovered)
        if inner is not None and inner not in enclosing:
            continue
        exports = result.module_exports.get(module_lc)
        if exports is None:
            continue  # external / unresolved module — nothing to list

        # Transitive re-export closure for the used module — the panel's
        # source of truth. ``trans_vars[name_lc] = (unit_or_None,
        # origin_module_lc)`` includes both locally-declared and
        # transitively re-exported names; ``origin_module_lc`` is the
        # module that *originally* declared the symbol (so a row for a
        # name re-exported from ``phys_base`` through ``phys_constants``
        # navigates to ``phys_base``'s declaration site). Falls back to
        # the direct ``exports`` view when the closure is missing
        # (transitive-disabled test stubs).
        trans_vars = result.module_transitive_vars.get(module_lc)
        trans_sigs = result.module_transitive_sigs.get(module_lc)
        if trans_vars is None:
            trans_vars = {
                n.lower(): (
                    exports.var_units.get(n)
                    or {k.lower(): v for k, v in exports.var_units.items()}.get(n.lower()),
                    module_lc,
                )
                for n in (exports.all_var_names or tuple(exports.var_units))
            }
        if trans_sigs is None:
            trans_sigs = {
                k.lower(): (v, module_lc) for k, v in exports.signatures.items()
            }

        # (local_lc, remote_lc) pairs brought into scope. A whole-module
        # import lists every transitively-visible variable AND every
        # procedure; an ``only:`` list names a subset.
        if ref is None or ref.only is None:
            pairs = [(n, n) for n in trans_vars]
            pairs += [(n, n) for n in trans_sigs]
        else:
            rename_map = {local: remote for local, remote in ref.renames}
            pairs = [
                (local.lower(), rename_map.get(local, local).lower())
                for local in ref.only
            ]

        for local_lc, remote_lc in pairs:
            if local_lc in local_names_lc or local_lc in seen:
                continue  # local declaration shadows it / already listed
            var_entry = trans_vars.get(remote_lc)
            sig_entry = trans_sigs.get(remote_lc)
            if var_entry is None and sig_entry is None:
                continue  # not an exported var or procedure (type, …) — skip
            seen.add(local_lc)
            # Re-derive a display name that preserves the user's casing
            # for direct-imported names; transitive names fall back to
            # the lower-cased form.
            local = local_lc
            if sig_entry is not None and var_entry is None:
                # Imported procedure: a function shows its return unit; a
                # subroutine has none (and isn't "missing" one). ``callable``
                # + ``signature`` (the parenthesised argument units, ``?``
                # for an un-annotated arg) let renderers show the contract,
                # e.g. ``force(kg, m)``.
                sig, origin_lc = sig_entry
                ret = format_unit(sig.return_unit) if sig.return_unit else None
                arg_units = ", ".join(
                    format_unit(u) if u is not None else "?" for u in sig.arg_units
                )
                row: dict[str, Any] = {
                    "name": local,
                    "unit": ret,
                    "unitNormalized": (
                        _normalized_unit(ret, scale_mode=scale_mode)
                        if ret else None
                    ),
                    "module": origin_lc,
                    "kind": ("annotated"
                             if (ret or sig.is_subroutine) else "unannotated"),
                    "callable": True,
                    "signature": "(" + arg_units + ")",
                }
                if origin_lc != module_lc:
                    row["viaModule"] = module_lc
                loc = _resolve_decl_location(
                    result, origin_lc, remote_lc, want_procedure=True,
                )
            else:
                assert var_entry is not None
                unit, origin_lc = var_entry
                unit_text = format_unit(unit) if unit is not None else None
                row = {
                    "name": local,
                    "unit": unit_text,
                    "unitNormalized": (
                        _normalized_unit(unit_text, scale_mode=scale_mode)
                        if unit_text else None
                    ),
                    "module": origin_lc,
                    "kind": "annotated" if unit_text else "unannotated",
                    "callable": False,
                }
                if origin_lc != module_lc:
                    row["viaModule"] = module_lc
                loc = _resolve_decl_location(result, origin_lc, remote_lc)
            if loc is None:
                loc = {"line": use_line, "column": 1}  # fall back to use site
            row.update(loc)
            out.append(row)
    return out
