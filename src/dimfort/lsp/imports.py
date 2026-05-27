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
    result: WorksetResult, module_lc: str, remote_name: str
) -> dict[str, Any] | None:
    """Locate where ``remote_name`` is declared in module ``module_lc``.

    Searches the workset's loaded trees for the module's defining file,
    then for the variable's declaration identifier inside it — the same
    two-step walk go-to-definition uses. Returns a 1-based ``{file, line,
    column}`` (for the panel wire format) or ``None`` when the module or
    the declaration can't be located (the caller then falls back to the
    ``use`` site)."""
    remote_lc = remote_name.lower()
    for tree_path, (other_tree, other_source) in result.trees.items():
        if not any(
            (nm := _ts_h.module_definition_name(mod, other_source))
            and nm[0].lower() == module_lc
            for mod in _ts_h.walk_module_definitions(other_tree)
        ):
            continue
        # The module's file — find the declaration of ``remote_name``.
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
) -> list[dict[str, Any]]:
    """Build the in-scope imported-variable rows for the panel.

    ``cursor_line`` is 1-based. ``local_names_lc`` is the set of
    lower-cased names declared locally in the file (a local declaration
    shadows an import, so those are dropped from the Imports list — they
    already appear under Scope). One row per imported variable visible at
    the cursor, each ``{name, unit, unitNormalized, module, kind, file?,
    line?, column?}``."""
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

        # (local, remote) name pairs brought into scope.
        if ref is None or ref.only is None:
            # Whole-module import: every declared variable (annotated or
            # not). ``all_var_names`` is the full set; fall back to the
            # annotated keys for older export records.
            names = exports.all_var_names or tuple(exports.var_units)
            pairs = [(n, n) for n in names]
        else:
            rename_map = {local: remote for local, remote in ref.renames}
            pairs = [(local, rename_map.get(local, local)) for local in ref.only]

        for local, remote in pairs:
            local_lc = local.lower()
            if local_lc in local_names_lc or local_lc in seen:
                continue  # local declaration shadows it / already listed
            seen.add(local_lc)
            unit = exports.var_units.get(remote)
            if unit is None:  # case-insensitive fallback (scanner verbatim)
                for k, v in exports.var_units.items():
                    if k.lower() == remote.lower():
                        unit = v
                        break
            unit_text = format_unit(unit) if unit is not None else None
            row: dict[str, Any] = {
                "name": local,
                "unit": unit_text,
                "unitNormalized": (
                    _normalized_unit(unit_text) if unit_text else None
                ),
                "module": module_lc,
                "kind": "annotated" if unit_text else "unannotated",
            }
            loc = _resolve_decl_location(result, module_lc, remote)
            if loc is None:
                # Fall back to the ``use`` site in this file.
                loc = {"line": use_line, "column": 1}
            row.update(loc)
            out.append(row)
    return out
