"""Per-line coverage projection over a workset check result.

See ``docs/design/future/coverage-visualization.md`` for the design
spec. Reuses the existing :class:`~dimfort.core.multifile.WorksetResult`
without re-running the checker; produces a per-line status map that
the LSP layer surfaces via ``dimfort/lineStatus`` and the CLI surfaces
via the ``dimfort coverage`` subcommand.

The four status tiers (``green`` / ``yellow`` / ``red`` / ``blue``)
reuse the existing marker colours documented in
``docs/design/shipped/markers.md``. Lines not present in the projection
are out-of-scope (no decoration). The taxonomy is defined in §3 of the
design spec.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tree_sitter import Tree

    from dimfort.core.diagnostics import Diagnostic
    from dimfort.core.multifile import WorksetResult


# ---------------------------------------------------------------------------
# Diagnostic code → coverage tier
# ---------------------------------------------------------------------------

# Codes whose presence on a line paints it red (hard fire).
_RED_CODES: frozenset[str] = frozenset({"H001", "H002", "H003", "H004"})

# Codes whose presence on a line paints it yellow (needs attention).
# Includes both U005 (unannotated-but-used) and H010 (hint-level fires).
_YELLOW_CODES: frozenset[str] = frozenset({"U005", "H010"})

# Codes whose presence on a line paints it blue (unparsed region).
_BLUE_CODES: frozenset[str] = frozenset({"P001"})

# Worst-wins ordering when multiple diagnostics cover the same line. Higher
# value = more severe; the projection records the maximum.
_TIER_ORDER: dict[str, int] = {"green": 0, "blue": 1, "yellow": 2, "red": 3}


# ---------------------------------------------------------------------------
# Per-line projection
# ---------------------------------------------------------------------------


def _diagnostic_tier(d: Diagnostic) -> str | None:
    """Return the coverage tier for a diagnostic.

    Args:
        d: Diagnostic from the checker.

    Returns:
        ``"red"`` / ``"yellow"`` / ``"blue"`` if the code is in the
        coverage-bearing set; ``None`` for diagnostics that do not
        drive coverage (e.g. ``U002`` annotation-parse, ``U020``
        info, ``S001`` / ``S002`` scale — these have their own
        marker logic and are not part of the visualisation
        taxonomy in v1).
    """
    if d.code in _RED_CODES:
        return "red"
    if d.code in _YELLOW_CODES:
        return "yellow"
    if d.code in _BLUE_CODES:
        return "blue"
    return None


# Statement node types that carry unit-bearing computation. An
# identifier appearing inside one of these counts toward the line's
# green status; an identifier appearing under a declaration / signature
# / use-statement does not (those contexts are not "checking" a unit).
_EXPRESSION_STATEMENT_TYPES: frozenset[str] = frozenset({
    "assignment_statement",
    "subroutine_call",
    "if_statement",
    "elseif_clause",
    "do_loop_statement",
    "where_statement",
    "select_case_statement",
    "case_statement",
    "return_statement",
    "print_statement",
    "write_statement",
    "read_statement",
})


def _walk_expression_lines(tree: Tree, names_lc: frozenset[str]) -> set[int]:
    """Collect line numbers of unit-bearing statements referencing an annotated identifier.

    Walks the tree-sitter tree, identifies expression-bearing statement
    nodes (per :data:`_EXPRESSION_STATEMENT_TYPES`), and records every
    line spanned by such a node iff a descendant identifier matches
    one of ``names_lc``.

    Lines occupied purely by declarations, subroutine / function
    signatures, ``use`` statements, ``end`` markers, or comments do
    NOT contribute — those carry annotated identifiers in their token
    streams but are not unit-checking the way an assignment or call
    site is.

    Args:
        tree: Parse tree for the file. Caller must hold any required
            traversal lock (see ``state.ts_handler_lock`` in
            ``lsp/state.py``).
        names_lc: Lower-cased annotated names. A statement counts iff
            at least one descendant ``identifier`` matches.

    Returns:
        Set of 1-based line numbers. Empty if ``names_lc`` is empty
        or no expression-bearing statement matches.
    """
    if not names_lc:
        return set()

    def _has_annotated_identifier(node: object) -> bool:
        """Depth-first scan for an ``identifier`` token in ``names_lc``."""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":  # type: ignore[attr-defined]
                text = n.text  # type: ignore[attr-defined]
                if text is not None:
                    name_lc = text.decode("utf-8", errors="replace").lower()
                    if name_lc in names_lc:
                        return True
            stack.extend(n.children)  # type: ignore[attr-defined]
        return False

    lines: set[int] = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in _EXPRESSION_STATEMENT_TYPES:
            if _has_annotated_identifier(node):
                # tree-sitter rows are 0-based; convert. Span every
                # line the statement touches so multi-line continued
                # statements paint completely.
                for row in range(node.start_point[0], node.end_point[0] + 1):
                    lines.add(row + 1)
            # Don't descend into a matched expression statement (its
            # whole span is already painted) — but still descend into
            # an unmatched one, since a nested if/case may contain a
            # matching deeper statement.
            if _has_annotated_identifier(node):
                continue
        stack.extend(node.children)
    return lines


def project_file(
    path: Path,
    result: WorksetResult,
) -> dict[int, str]:
    """Compute the per-line coverage status for one file.

    Implements §10.2 of the design spec. Returns a dict mapping 1-based
    line numbers to one of ``"green"`` / ``"yellow"`` / ``"red"`` /
    ``"blue"``. Lines not present in the dict are out-of-scope.

    Args:
        path: Resolved absolute path of the file. Must match the key
            shape used by ``result.diagnostics`` and
            ``result.attachments``.
        result: Workset check result the projection reads from. The
            function does NOT mutate it.

    Returns:
        Mapping ``{line: status}``. Empty when the file is not in the
        workset (no annotations, no diagnostics, no parsed tree).

    Note:
        For green detection this function walks the cached tree at
        ``result.trees[path]``. Callers that share that tree with
        other tree-sitter traversal handlers must serialise the call
        with the shared traversal lock (see the LSP handler in
        ``lsp/coverage.py`` for the locking pattern).
    """
    statuses: dict[int, str] = {}

    # Step 1: paint red / yellow / blue from diagnostics, worst-wins.
    for d in result.diagnostics.get(path, []):
        tier = _diagnostic_tier(d)
        if tier is None:
            continue
        for line in range(d.start.line, d.end.line + 1):
            current = statuses.get(line)
            if current is None or _TIER_ORDER[tier] > _TIER_ORDER[current]:
                statuses[line] = tier

    # Step 2: paint green where annotated identifiers appear and no
    # higher-severity tier already owns the line. Lines that hold an
    # annotation token itself (the declaration) are also included via
    # the var_units_span table.
    attached = result.attachments.get(path)
    if attached is None:
        return statuses

    # Declaration lines: the line carrying the @unit{} token.
    for line, _start_col, _end_col in attached.var_units_span.values():
        if line not in statuses:
            statuses[line] = "green"

    # Use-sites: walk the tree and find identifier tokens matching any
    # annotated name. Skip when no tree is cached (e.g. a load failure).
    tree_entry = result.trees.get(path)
    if tree_entry is None:
        return statuses
    tree, _source = tree_entry

    annotated_names_lc = frozenset(name.lower() for name in attached.var_units)
    use_lines = _walk_expression_lines(tree, annotated_names_lc)
    for line in use_lines:
        if line not in statuses:
            statuses[line] = "green"

    return statuses


# ---------------------------------------------------------------------------
# Aggregate statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileCoverage:
    """Per-file aggregate counts for one workset file.

    Attributes:
        path: Resolved absolute path of the file.
        ok: Lines painted green (verified-OK).
        warn: Lines painted yellow (needs attention).
        fire: Lines painted red (hard fire).
        unparsed: Lines painted blue (P001 unparsed region).
        out: Lines with no coverage decoration (out of scope).
    """

    path: Path
    ok: int
    warn: int
    fire: int
    unparsed: int
    out: int

    @property
    def coverage_pct(self) -> float:
        """Percentage of in-scope lines painted green.

        Returns:
            ``ok / (ok + warn + fire + unparsed) * 100`` rounded to one
            decimal place. ``0.0`` when the denominator is zero
            (entire file out of scope — typical for a file with no
            annotations and no diagnostics).
        """
        in_scope = self.ok + self.warn + self.fire + self.unparsed
        if in_scope == 0:
            return 0.0
        return round((self.ok / in_scope) * 100.0, 1)


def aggregate_file(
    path: Path,
    statuses: dict[int, str],
    *,
    total_lines: int,
) -> FileCoverage:
    """Tally a per-line status map into per-tier counts.

    Args:
        path: Resolved absolute path of the file.
        statuses: Per-line projection from :func:`project_file`.
        total_lines: Total source-file line count. The
            ``out`` field is computed as
            ``total_lines - (ok + warn + fire + unparsed)``.

    Returns:
        :class:`FileCoverage` with the four tier counts plus the
        out-of-scope count.
    """
    ok = sum(1 for s in statuses.values() if s == "green")
    warn = sum(1 for s in statuses.values() if s == "yellow")
    fire = sum(1 for s in statuses.values() if s == "red")
    unparsed = sum(1 for s in statuses.values() if s == "blue")
    in_scope = ok + warn + fire + unparsed
    out = max(0, total_lines - in_scope)
    return FileCoverage(
        path=path,
        ok=ok,
        warn=warn,
        fire=fire,
        unparsed=unparsed,
        out=out,
    )


@dataclass(frozen=True)
class WorksetCoverage:
    """Aggregate coverage stats across a set of files.

    Attributes:
        files: Per-file breakdowns in the order they were aggregated.
        ok: Total green lines across the workset.
        warn: Total yellow lines.
        fire: Total red lines.
        unparsed: Total blue lines.
        out: Total out-of-scope lines.
    """

    files: tuple[FileCoverage, ...]
    ok: int
    warn: int
    fire: int
    unparsed: int
    out: int

    @property
    def coverage_pct(self) -> float:
        """Workset-wide coverage percentage, computed like :attr:`FileCoverage.coverage_pct`.

        Returns:
            ``ok / (ok + warn + fire + unparsed) * 100`` rounded to one
            decimal place. ``0.0`` when no file in the workset has any
            in-scope lines.
        """
        in_scope = self.ok + self.warn + self.fire + self.unparsed
        if in_scope == 0:
            return 0.0
        return round((self.ok / in_scope) * 100.0, 1)


def aggregate_workset(files: Iterable[FileCoverage]) -> WorksetCoverage:
    """Sum per-file coverage records into a workset total.

    Args:
        files: Iterable of :class:`FileCoverage` records, typically the
            output of one :func:`aggregate_file` per workset file.

    Returns:
        :class:`WorksetCoverage` with per-file breakdowns preserved
        and tier totals summed.
    """
    rows = tuple(files)
    return WorksetCoverage(
        files=rows,
        ok=sum(f.ok for f in rows),
        warn=sum(f.warn for f in rows),
        fire=sum(f.fire for f in rows),
        unparsed=sum(f.unparsed for f in rows),
        out=sum(f.out for f in rows),
    )
