"""Workset-wide name → declaration index for goto-def (audit #12).

Built once per ``check_files`` completion by the LSP layer; the
goto-definition handler consults it instead of walking every cached
tree per request. Brings worst-case from "hundreds of ms on a 2435-file
workset" to ~O(log N) dict lookup + a small filter pass.

The builder lives in the LSP layer because it depends on
:mod:`dimfort.lsp.ts_helpers` (tree-sitter walkers). The data shape
itself — :class:`~dimfort.core.multifile.SymbolEntry` — lives in core
next to :class:`~dimfort.core.multifile.WorksetResult`, so CLI callers
that never construct an index still pay zero.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from dimfort.core.multifile import SymbolEntry
from dimfort.lsp import ts_helpers as _ts_h

if TYPE_CHECKING:
    from pathlib import Path

    from tree_sitter import Tree


def build_symbols_index(
    trees: dict[Path, tuple[Tree, bytes]],
) -> dict[str, tuple[SymbolEntry, ...]]:
    """Walk every tree once and return a lower-cased name → sites map.

    Args:
        trees: ``WorksetResult.trees`` — per-file ``(tree, source)``
            pairs after a successful ``check_files`` pass.

    Returns:
        Dict from lower-cased symbol name to a tuple of
        :class:`SymbolEntry` records. Three kinds are surfaced:

        * ``"module"`` — top-level ``module`` definitions.
        * ``"callable"`` — both ``function`` and ``subroutine``
          definitions (the goto-def classification collapses them
          because ``a(1)`` is syntactically ambiguous in Fortran).
        * ``"var"`` — every ``variable_declaration``.

        Multiple files declaring the same name (Fortran is
        case-insensitive, so casings collapse) all appear in the same
        tuple. Callers decide how to filter / disambiguate.
    """
    bucket: dict[str, list[SymbolEntry]] = defaultdict(list)
    for path, (tree, source) in trees.items():
        for mod in _ts_h.walk_module_definitions(tree):
            nm = _ts_h.module_definition_name(mod, source)
            if nm is None:
                continue
            name, name_node = nm
            sr, sc = name_node.start_point
            er, ec = name_node.end_point
            bucket[name.lower()].append(SymbolEntry(
                file=path, kind="module",
                start_row=sr, start_col=sc, end_row=er, end_col=ec,
            ))
        for func in _ts_h.walk_function_definitions(tree):
            nm = _ts_h.function_definition_name(func, source)
            if nm is None:
                continue
            name, name_node = nm
            sr, sc = name_node.start_point
            er, ec = name_node.end_point
            bucket[name.lower()].append(SymbolEntry(
                file=path, kind="callable",
                start_row=sr, start_col=sc, end_row=er, end_col=ec,
            ))
        for _decl, name_node in _ts_h.walk_decl_identifiers(tree):
            text = source[name_node.start_byte:name_node.end_byte].decode(
                "utf-8", errors="replace",
            )
            sr, sc = name_node.start_point
            er, ec = name_node.end_point
            bucket[text.lower()].append(SymbolEntry(
                file=path, kind="var",
                start_row=sr, start_col=sc, end_row=er, end_col=ec,
            ))
    return {name: tuple(entries) for name, entries in bucket.items()}
