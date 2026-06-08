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

import re
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

# Codes whose presence on a line paints it red (hard fire). Covers the
# full ERROR-severity consistency family — dimension homogeneity
# (H001-H004), polymorphism unification failures (H020-H023), affine-
# conversion-directive validation (S003), and unparseable annotation
# (U002 — the user's @unit{...} text itself failed to parse).
_RED_CODES: frozenset[str] = frozenset({
    "H001", "H002", "H003", "H004",
    "H020", "H021", "H022", "H023",
    "S003",
    "U002",
})

# Codes whose presence on a line paints it yellow (needs attention).
# Covers WARNING-severity quality and scale diagnostics: U005
# (unannotated-but-used), H010 (hint-level fires — implicit literal
# cast etc.), S001 / S002 (scale and offset mismatches).
_YELLOW_CODES: frozenset[str] = frozenset({
    "U005", "H010", "S001", "S002",
})

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


# U005 message format from ts_checker.py:
#   f"{name_text!r} is used in a unit-checked expression but has no @unit{{}}"
# Python's repr() on a string wraps it in single quotes; the regex
# captures whatever sits between them. Stable across server versions
# because the message shape is part of the documented diagnostic
# surface (changing it would already be a breaking change).
_U005_NAME_RE = re.compile(r"^'([^']+)'")


def _unannotated_names_for_file(
    result: WorksetResult, path: Path
) -> frozenset[str]:
    """Extract the lower-cased names of all unannotated-but-used variables.

    The checker fires one ``U005`` diagnostic per unannotated declaration
    that participates in a unit-checked expression. The variable name
    appears as a single-quoted token at the start of the message; this
    function lifts those names out so the projection can propagate the
    yellow signal from declaration lines to every use site.

    Args:
        result: Workset check result the diagnostics live on.
        path: Resolved absolute file path; key into
            ``result.diagnostics``.

    Returns:
        Lower-cased set of names that fired ``U005`` for this file.
        Empty when the file has no ``U005`` diagnostics or no entry in
        ``result.diagnostics``.
    """
    names: set[str] = set()
    for d in result.diagnostics.get(path, []):
        if d.code != "U005":
            continue
        m = _U005_NAME_RE.match(d.message)
        if m is not None:
            names.add(m.group(1).lower())
    return frozenset(names)


# Substring marker for an annotation comment. Every canonical directive
# (``@unit{...}``, ``@unit_assume{...}``, ``@unit_affine_conversion{...}``)
# starts with this token. User-configured comment delimiters (e.g.
# ``[m/s]``) would not match — those rely on the ``var_units_span``
# first-seen-wins fallback, which is itself a known limitation called
# out in the §10.2 projection notes.
_ANNOTATION_MARKER: bytes = b"@unit"

# Intrinsic-type texts that carry a meaningful unit. A declaration of
# one of these types without an ``@unit`` annotation paints yellow as
# an "unannotated, could carry a unit" signal — matching the panel /
# hover resolution-axis 🟡. Integer / character / logical types are
# not unit-bearing and do not paint at all.
_UNIT_BEARING_TYPES: frozenset[str] = frozenset({
    "real",
    "double precision",
    "double",
})


def _walk_all_channels(
    tree: Tree,
    annotated_lc: frozenset[str],
    unannotated_lc: frozenset[str],
) -> tuple[set[int], set[int], set[int], set[int]]:
    """Audit #1b: walk the tree ONCE, emit all four coverage channels.

    Replaces three separate full-tree walks (annotation comments,
    unannotated unit-bearing declarations, expression-statement
    classification) with one pass. On a real-world workset (~2435
    files) the original three-walk pattern accounted for ~8.5 s of
    ``build_workspace_payload`` cost; collapsing brings the
    per-file cost down by ~3×.

    Args:
        tree: Parse tree for the file. Caller must hold any required
            traversal lock.
        annotated_lc: Lower-cased annotated names (for green
            classification of expression statements). Pass an empty
            frozenset to skip classification — comments + decls
            still emit.
        unannotated_lc: Lower-cased unannotated names (for yellow
            classification). Empty frozenset = skip classification.

    Returns:
        Quadruple of 1-based line sets:
        ``(annotation_comment_lines, unannotated_decl_lines,
        green_lines, yellow_lines)``. Equivalent to running the three
        original walkers and unpacking their results.
    """
    annotation_lines: set[int] = set()
    unannotated_decl_lines: set[int] = set()
    green_lines: set[int] = set()
    yellow_lines: set[int] = set()
    classify = bool(annotated_lc or unannotated_lc)

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        # Channel 1: annotation comments. Skip descent — comments
        # don't carry meaningful children.
        if node.type == "comment":
            text = node.text
            if text is not None and _ANNOTATION_MARKER in text:
                for row in range(node.start_point[0], node.end_point[0] + 1):
                    annotation_lines.add(row + 1)
            continue

        # Channel 2: unannotated unit-bearing declarations. Inspect
        # each variable_declaration CHILD against its sibling
        # comments (the annotation lives as a sibling, not a child,
        # in tree-sitter Fortran). The walk continues to descend into
        # both variable_declaration and non-declaration children
        # because the channel-3 expression walk needs full coverage
        # and the channel-1 comment walk needs to find inline
        # ``!< @unit{m}`` siblings deeper in the tree.
        children = node.children
        for child in children:
            if child.type != "variable_declaration":
                continue
            intrinsic = next(
                (c for c in child.children if c.type == "intrinsic_type"),
                None,
            )
            if intrinsic is None or intrinsic.text is None:
                continue
            type_text = (
                intrinsic.text.decode("utf-8", errors="replace")
                .lower()
                .strip()
            )
            if type_text not in _UNIT_BEARING_TYPES:
                continue
            decl_end_row = child.end_point[0]
            is_annotated = False
            for sibling in children:
                if sibling.type != "comment":
                    continue
                if sibling.start_point[0] != decl_end_row:
                    continue
                comment_text = sibling.text
                if (
                    comment_text is not None
                    and _ANNOTATION_MARKER in comment_text
                ):
                    is_annotated = True
                    break
            if is_annotated:
                continue
            for row in range(child.start_point[0], child.end_point[0] + 1):
                unannotated_decl_lines.add(row + 1)

        # Channel 3: expression-statement classification. Inspect
        # the descendant identifier subtree once; bucket the line
        # span by tier. Don't descend into the matched statement —
        # comments inside expression statements (the rare ``x = 1 !
        # @unit{m}`` shape) are handled by Channel 1 from the
        # PARENT's child iteration since tree-sitter Fortran emits
        # such trailing comments as siblings of the statement, not
        # children.
        if classify and node.type in _EXPRESSION_STATEMENT_TYPES:
            has_unann, has_ann = _classify_identifiers(
                node, annotated_lc, unannotated_lc,
            )
            if has_unann or has_ann:
                for row in range(node.start_point[0], node.end_point[0] + 1):
                    line = row + 1
                    if has_unann:
                        yellow_lines.add(line)
                    elif has_ann:
                        green_lines.add(line)
                continue

        stack.extend(children)
    return annotation_lines, unannotated_decl_lines, green_lines, yellow_lines


def _classify_identifiers(
    node: object,
    annotated_lc: frozenset[str],
    unannotated_lc: frozenset[str],
) -> tuple[bool, bool]:
    """Depth-first scan of ``node``'s identifier subtree.

    Returns ``(has_unannotated, has_annotated)`` — both booleans
    independent so a statement can contribute to yellow and green
    simultaneously. Extracted from the original ``_walk_expression_lines``
    inner ``_classify`` so the merged walker can call it directly.
    """
    has_unann = False
    has_ann = False
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == "identifier":  # type: ignore[attr-defined]
            text = n.text  # type: ignore[attr-defined]
            if text is not None:
                name_lc = text.decode("utf-8", errors="replace").lower()
                if name_lc in unannotated_lc:
                    has_unann = True
                elif name_lc in annotated_lc:
                    has_ann = True
        stack.extend(n.children)  # type: ignore[attr-defined]
    return has_unann, has_ann


def _walk_annotation_comment_lines(tree: Tree) -> set[int]:
    """Collect 1-based line numbers carrying an ``@unit`` annotation comment.

    Tree-sitter Fortran parses an inline annotation
    (``real :: x  !< @unit{m}``) as a ``comment`` node sibling of the
    ``variable_declaration``, not as a child of it. Walking for the
    declaration node alone therefore misses the comment that carries
    the annotation marker. This helper walks every ``comment`` node
    and records its line iff its text contains :data:`_ANNOTATION_MARKER`.

    The line of an annotation comment is the line of the declaration
    it documents (Fortran's ``!<`` trails the declaration on the same
    line) so painting that line green is equivalent to painting the
    declaration line — without depending on
    :attr:`AttachmentResult.var_units_span`, which is keyed
    first-seen-wins on the variable name and therefore misses
    same-name declarations across scopes (e.g. a polymorphic ``x``
    declared in every routine of a module).

    Args:
        tree: Parse tree for the file. Caller must hold any required
            traversal lock.

    Returns:
        Set of 1-based line numbers carrying an annotation marker.
    """
    lines: set[int] = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "comment":
            text = node.text
            if text is not None and _ANNOTATION_MARKER in text:
                for row in range(node.start_point[0], node.end_point[0] + 1):
                    lines.add(row + 1)
            # Comments don't have meaningful children.
            continue
        stack.extend(node.children)
    return lines


def _walk_unannotated_unit_bearing_declaration_lines(tree: Tree) -> set[int]:
    """Collect lines spanned by unit-bearing declarations without an annotation.

    A declaration of a unit-bearing intrinsic type (real, double
    precision) that lacks an ``@unit`` annotation comment paints yellow
    in the coverage view. This matches the panel / hover resolution
    axis: 🟡 means "could carry a unit, doesn't yet" — the same signal
    surfaces independent of whether ``U005`` happened to fire (``U005``
    only fires on declarations whose variables are also *used* in a
    unit-checked expression; a declared-but-never-used real variable
    has no diagnostic but is still unannotated).

    "Annotated" is judged by looking at every sibling ``comment`` node
    on the declaration's last line: if any such comment carries the
    ``@unit`` marker, the declaration is annotated and this walker
    skips it (the annotated case is painted green by
    :func:`_walk_annotation_comment_lines`).

    Non-unit-bearing types (``integer``, ``character``, ``logical``,
    derived types) are not painted by this walker — they carry no
    coverage signal at all.

    Args:
        tree: Parse tree for the file. Caller must hold any required
            traversal lock.

    Returns:
        Set of 1-based line numbers spanned by unit-bearing
        declarations that lack an inline ``@unit`` annotation.
    """
    lines: set[int] = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        # Look at this node's children so we can match a
        # variable_declaration against its sibling comments.
        children = list(node.children)
        for child in children:
            if child.type != "variable_declaration":
                stack.append(child)
                continue
            # Detect the declaration's intrinsic type. Skip non-unit-
            # bearing types entirely; they carry no coverage signal.
            intrinsic = next(
                (c for c in child.children if c.type == "intrinsic_type"),
                None,
            )
            if intrinsic is None or intrinsic.text is None:
                continue
            type_text = (
                intrinsic.text.decode("utf-8", errors="replace")
                .lower()
                .strip()
            )
            if type_text not in _UNIT_BEARING_TYPES:
                continue
            # Is there an annotation comment on the declaration's end
            # line? Scan sibling comments — declarations and their
            # ``!<`` annotations live as siblings in the tree, not in
            # a parent/child relationship.
            decl_end_row = child.end_point[0]
            is_annotated = False
            for sibling in children:
                if sibling.type != "comment":
                    continue
                if sibling.start_point[0] != decl_end_row:
                    continue
                comment_text = sibling.text
                if (
                    comment_text is not None
                    and _ANNOTATION_MARKER in comment_text
                ):
                    is_annotated = True
                    break
            if is_annotated:
                continue
            for row in range(child.start_point[0], child.end_point[0] + 1):
                lines.add(row + 1)
    return lines


def _walk_expression_lines(
    tree: Tree,
    annotated_lc: frozenset[str],
    unannotated_lc: frozenset[str],
) -> tuple[set[int], set[int]]:
    """Walk expression-bearing statements and bucket their lines by tier.

    For each expression-bearing statement node (per
    :data:`_EXPRESSION_STATEMENT_TYPES`), inspects descendant
    ``identifier`` tokens and decides which tier the statement
    contributes to its spanned lines:

    - **Yellow candidate**: the statement references at least one
      identifier in ``unannotated_lc`` — the use site participates in
      an unannotated declaration, so the line is "needs attention"
      even if no direct diagnostic owns it.
    - **Green candidate**: no unannotated reference, but the statement
      references at least one identifier in ``annotated_lc`` — the
      line is verified-OK in the literal "diagnostic owns the line"
      sense.

    Yellow wins over green on the same line (worst-of-children).

    Args:
        tree: Parse tree for the file. Caller must hold any required
            traversal lock.
        annotated_lc: Lower-cased annotated names from
            ``attached.var_units``.
        unannotated_lc: Lower-cased unannotated names extracted from
            the file's ``U005`` diagnostics (see
            :func:`_unannotated_names_for_file`).

    Returns:
        Pair ``(green_lines, yellow_lines)`` of 1-based line numbers.
        Lines may appear in both sets — :func:`project_file` resolves
        with worst-wins (yellow above green).
    """
    if not annotated_lc and not unannotated_lc:
        return set(), set()

    def _classify(node: object) -> tuple[bool, bool]:
        """Depth-first scan for matching identifiers.

        Returns:
            ``(has_unannotated, has_annotated)`` — both booleans
            independent so a statement can contribute to yellow and
            green simultaneously.
        """
        has_unann = False
        has_ann = False
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type == "identifier":  # type: ignore[attr-defined]
                text = n.text  # type: ignore[attr-defined]
                if text is not None:
                    name_lc = text.decode("utf-8", errors="replace").lower()
                    if name_lc in unannotated_lc:
                        has_unann = True
                    elif name_lc in annotated_lc:
                        has_ann = True
            stack.extend(n.children)  # type: ignore[attr-defined]
        return has_unann, has_ann

    green_lines: set[int] = set()
    yellow_lines: set[int] = set()
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in _EXPRESSION_STATEMENT_TYPES:
            has_unann, has_ann = _classify(node)
            if has_unann or has_ann:
                # tree-sitter rows are 0-based; convert. Span every
                # line the statement touches so multi-line continued
                # statements paint completely.
                for row in range(node.start_point[0], node.end_point[0] + 1):
                    line = row + 1
                    if has_unann:
                        yellow_lines.add(line)
                    elif has_ann:
                        green_lines.add(line)
                # Don't descend into a matched statement — its whole
                # span is already classified.
                continue
        stack.extend(node.children)
    return green_lines, yellow_lines


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

    # Step 2: paint green / yellow at expression sites where annotated
    # or unannotated identifiers appear. The unannotated set is lifted
    # from the file's U005 diagnostics so a use of an unannotated
    # variable carries the yellow signal at every use site, not just
    # the declaration line. Diagnostic-painted tiers from step 1
    # already win on lines they cover.
    attached = result.attachments.get(path)
    if attached is None:
        return statuses

    # Declaration lines: every ``variable_declaration`` node carrying
    # an ``@unit`` annotation in its source span paints green.
    #
    # The earlier implementation read ``attached.var_units_span``, but
    # that table is first-seen-wins on the variable NAME — in a file
    # where the same name appears in multiple scopes (e.g. a
    # polymorphic ``x`` declared in every routine of a module), only
    # the first scope's declaration line is recorded and the others
    # show uncoloured. Walking the tree once recovers every annotated
    # declaration regardless of scope.
    tree_entry = result.trees.get(path)
    if tree_entry is None:
        # No tree → fall back to the (incomplete) span-only painting
        # so single-scope files still work when the tree isn't cached.
        for line, _start_col, _end_col in attached.var_units_span.values():
            if line not in statuses:
                statuses[line] = "green"
        return statuses
    tree, _source = tree_entry

    # Audit #1b: one tree walk emits all four coverage channels
    # (annotation comments, unannotated unit-bearing declarations,
    # green/yellow expression use-sites). The original three-walk
    # pattern accounted for ~8.5 s of ``build_workspace_payload``
    # cost on a 2435-file workset; the merged walker brings
    # per-file cost down by ~3×. Functions ``_walk_*`` retained
    # below for direct callers / tests; they delegate to the
    # merged walker.
    annotated_lc = frozenset(name.lower() for name in attached.var_units)
    unannotated_lc = _unannotated_names_for_file(result, path)
    (
        annotation_lines,
        unannotated_decl_lines,
        green_lines,
        yellow_lines,
    ) = _walk_all_channels(tree, annotated_lc, unannotated_lc)

    for decl_line in annotation_lines:
        if decl_line not in statuses:
            statuses[decl_line] = "green"

    # Unannotated unit-bearing declarations: paint yellow. Matches the
    # panel / hover resolution-axis 🟡 — a real / double-precision
    # variable without an @unit{} annotation is "unannotated, could
    # carry a unit." Fires even when U005 doesn't (a declared-but-
    # never-used variable has no diagnostic but is still unannotated).
    for decl_line in unannotated_decl_lines:
        current = statuses.get(decl_line)
        if current is None or _TIER_ORDER["yellow"] > _TIER_ORDER[current]:
            statuses[decl_line] = "yellow"
    # Yellow first — worst-wins means a line that fell into both buckets
    # paints yellow. Lines already painted red / yellow / blue from
    # step 1 stay at their (possibly higher) tier.
    for line in yellow_lines:
        current = statuses.get(line)
        if current is None or _TIER_ORDER["yellow"] > _TIER_ORDER[current]:
            statuses[line] = "yellow"
    for line in green_lines:
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
        """Percentage of checkable, parseable lines painted green.

        Returns:
            ``ok / (ok + warn + fire) * 100`` rounded to one decimal
            place. ``0.0`` when the denominator is zero (no
            annotatable lines — typical for a file with no
            annotations and no diagnostics, or one entirely covered
            by P001 unparsed regions).

        Note:
            ``unparsed`` is excluded from the denominator because P001
            regions are a tool limitation rather than a missing
            annotation — counting them against the user would conflate
            annotation effort with parser coverage. A fully annotated
            workset reaches 100% even when P001 regions exist.
            ``out`` is excluded for the same reason it always was: not
            a checkable line.
        """
        annotatable = self.ok + self.warn + self.fire
        if annotatable == 0:
            return 0.0
        return round((self.ok / annotatable) * 100.0, 1)


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
            ``ok / (ok + warn + fire) * 100`` rounded to one decimal
            place. ``0.0`` when no file in the workset has any
            annotatable lines.
        """
        annotatable = self.ok + self.warn + self.fire
        if annotatable == 0:
            return 0.0
        return round((self.ok / annotatable) * 100.0, 1)


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
