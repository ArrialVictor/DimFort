"""Multi-file orchestration (tree-sitter pipeline).

Runs scan → attach → check across a workset of Fortran files,
producing a :class:`WorksetResult` consumable by the CLI and LSP.

Pipeline:

1. **Phase A — load.** Per file, read source, scan annotations, attach
   them, and parse with tree-sitter. Runs in parallel via a thread pool;
   the tree-sitter C grammar releases the GIL during parsing.
2. **Phase B — aggregate annotations.** Merge per-file ``var_units`` /
   ``field_units`` tables; parse the unit strings to :class:`Unit`
   objects once.
3. **Phase C — index.** Walk every loaded tree to collect module
   exports and function/subroutine signatures, plus per-file
   scope-keyed unit tables, then compute the transitive re-export
   closure across module ``use`` chains so cross-file lookups and
   H004 work.
4. **Phase D — check.** Per file, re-parse its own ``var_units``
   locally (so per-file scoping doesn't leak across files), splice
   the closure-computed imports into a local-scope copy of
   ``(var_units, signatures)``, then run :func:`ts_checker.check`.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tree_sitter import Tree

from dimfort.core import multifile_cache as _mfc
from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core import units as _units_mod
from dimfort.core import workspace_index as _wsi
from dimfort.core.annotations import scan_text
from dimfort.core.attach import AttachmentResult, attach
from dimfort.core.cache_key import IncludeHasher, compute_file_key
from dimfort.core.cache_serde import (
    dump_diagnostic,
    dump_module_exports,
    load_diagnostic,
)
from dimfort.core.cache_store import CacheStore
from dimfort.core.diagnostics import AutocastEvent, Diagnostic, Position, Severity
from dimfort.core.multifile_cache import (
    CachedProjection,
    ExportsKey,
    ModuleExportsCache,
    ProjectionCache,
    ProjectionKey,
    TreeCache,
    TreeKey,
    patterns_fingerprint,
)
from dimfort.core.rewrite import suggest_rewrite as _suggest_rewrite
from dimfort.core.symbols import (
    FuncSig,
    ModuleExports,
    apply_use_clauses,
    compute_transitive_exports,
    deps_consumed_from_uses,
)
from dimfort.core.unit_patterns import (
    DEFAULT_AFFINE_PATTERNS,
    DEFAULT_ASSUME_PATTERNS,
    DEFAULT_UNIT_PATTERNS,
    StructuredPattern,
    UnitPattern,
)
from dimfort.core.units import UnitError, UnitExpr, UnitTable

# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileLoadFailure:
    """The file couldn't be read (or, vanishingly rare, tree-sitter raised).

    Attributes:
        stderr: Human-readable error text (the underlying exception
            string, or ``cpp``'s stderr on a CPP failure).
    """

    stderr: str


@dataclass(frozen=True)
class SymbolEntry:
    """One declaration site, indexed for workset-wide name lookup.

    Used by :class:`WorksetResult.symbols_by_name_lc` so the LSP
    goto-definition handler can resolve a name in O(log N) instead of
    walking every cached tree per request.

    Attributes:
        file: Absolute path of the source file containing the
            declaration.
        kind: One of ``"module"`` | ``"callable"`` | ``"var"``.
            ``"callable"`` collapses ``function`` + ``subroutine`` to
            match the goto-def classification, where ``a(1)`` is
            syntactically ambiguous between a call and an array index.
        start_row: Zero-based row of the declaration's name node.
        start_col: Zero-based column of the declaration's name node.
        end_row: Zero-based end row of the declaration's name node.
        end_col: Zero-based end column of the declaration's name node.
    """

    file: Path
    kind: str
    start_row: int
    start_col: int
    end_row: int
    end_col: int


@dataclass
class WorksetResult:
    """Aggregated output of one workset pass.

    All ``dict`` / ``list`` fields default to empty containers and are
    populated incrementally by :func:`check_files`. Per-field semantics
    are documented inline beside each field.

    Attributes:
        diagnostics: Per-file list of emitted diagnostics, sorted as
            produced by the checker.
        attachments: Per-file annotation-attachment record.
        load_failures: Per-file read / parse failures, keyed by path.
        compile_failures: Reserved for future per-file compile-stage
            failures (currently unused; kept for API stability).
        trees: Per-file ``(tree, source_bytes)`` consumed by the LSP.
        merged_var_units: Workset-wide flat ``var_units`` table (first
            occurrence wins).
        merged_field_units: Workset-wide ``(type, field) → unit`` map.
        var_units_by_scope: Per-file scope-aware unit table.
        signatures: Function / subroutine signatures across the
            workset.
        module_exports: Lower-cased module name → exports record.
        module_transitive_vars: Per-module transitive re-export
            closure for variables.
        module_transitive_sigs: Per-module transitive re-export
            closure for signatures.
        phase_timings: Wall-clock seconds per pipeline phase.
        deps_consumed: Per-file set of workspace modules whose exports
            the file's check output depends on.
        autocast_events: Per-file R4.4 autocast events.
        unparseable_units: Per-file lower-cased names whose
            ``@unit{...}`` annotation failed to parse (the U002 set).
        cache_hits: Number of cache reads that validated.
        cache_misses: Number of cache reads that found no entry.
        cache_dirty: Number of cache reads invalidated by dep drift.
        cache_writes: Number of cache writes attempted (best-effort).
    """

    diagnostics: dict[Path, list[Diagnostic]] = field(default_factory=dict)
    attachments: dict[Path, AttachmentResult] = field(default_factory=dict)
    load_failures: dict[Path, FileLoadFailure] = field(default_factory=dict)
    compile_failures: dict[Path, str] = field(default_factory=dict)
    # Per-file ``(tree, source_bytes)`` pair populated below, used by
    # the LSP for hover/inlay/goto-time symbol lookup.
    trees: dict[Path, tuple[Tree, bytes]] = field(default_factory=dict)
    # Per-file parsed unit table (for hover formatting); same key set
    # as ``trees``.
    merged_var_units: dict[str, UnitExpr] = field(default_factory=dict)
    merged_field_units: dict[tuple[str, str], UnitExpr] = field(default_factory=dict)
    # Per-file scope-aware unit table. Key: file path → (scope_lc|None,
    # name) → Unit. Consumed by LSP hover/inlay so identifier lookups
    # honour the enclosing subroutine instead of falling back to the
    # flat merged_var_units (which is first-seen-wins across the
    # workset).
    var_units_by_scope: dict[Path, dict[tuple[str | None, str], UnitExpr]] = field(
        default_factory=dict
    )
    # Function / subroutine signatures resolved across the whole workset.
    signatures: dict[str, FuncSig] = field(default_factory=dict)
    # Module-name → exports map, populated during Phase C. Reused by
    # the LSP for module hover (summary of exported vars/signatures)
    # and module-name goto-definition. Keyed by lower-cased module
    # name to match ``apply_use_clauses`` lookups.
    module_exports: dict[str, ModuleExports] = field(default_factory=dict)
    # Transitive re-export closure, indexed by module-name (lower-cased).
    # ``module_transitive_vars[mod_lc][name_lc] = (unit_or_None,
    # origin_module_lc)`` and similarly for signatures. Computed once at
    # the end of Phase C (see :func:`compute_transitive_exports`) so the
    # LSP imports panel can surface chains like ``solver use phys_constants``
    # → ``phys_constants use phys_base`` without re-walking the graph
    # per cursor call.
    module_transitive_vars: dict[
        str, dict[str, tuple[UnitExpr | None, str]]
    ] = field(default_factory=dict)
    module_transitive_sigs: dict[
        str, dict[str, tuple[FuncSig, str]]
    ] = field(default_factory=dict)
    # Wall-clock seconds spent in each pipeline phase. Populated by
    # ``check_files``; consulted by the CLI's ``--timings`` flag and by
    # ad-hoc profiling scripts. Keys: "load", "aggregate", "index",
    # "check", "total".
    phase_timings: dict[str, float] = field(default_factory=dict)
    # Per-file ``deps_consumed``: the set of workspace modules whose
    # exports this file's checked output depends on. Populated by the
    # check phase; consumed by the content-hash cache writer to decide
    # which other files' caches must invalidate when this file changes.
    deps_consumed: dict[Path, frozenset[str]] = field(default_factory=dict)
    # Per-file autocast events (R4.4). Populated by the check phase
    # whenever a pure-numeric-constant RHS takes on its assignment's
    # LHS unit. Consumed by audit tooling and by the LSP renderers to
    # decide marker / unit rendering for assignments. Empty for files
    # without any autocast fires.
    autocast_events: dict[Path, list[AutocastEvent]] = field(default_factory=dict)
    # Per-file set of variable names whose ``@unit{...}`` annotation
    # failed to parse (the U002 set), lower-cased. Single source of
    # truth shared by the U002 diagnostic and the LSP panel's 🔴
    # "unparseable" marker, so both stay in lock-step.
    unparseable_units: dict[Path, frozenset[str]] = field(default_factory=dict)
    # Workset-wide name → declaration sites index, lower-cased keys.
    # Populated by the LSP layer (``dimfort.lsp.symbols_index``) after
    # ``check_files`` returns so the goto-definition handler can avoid
    # walking every cached tree per request. CLI callers leave this
    # empty (it's an LSP performance feature, not a checker semantics
    # change). See ``docs/0_2_6_PLAN.md`` audit #12 for rationale.
    symbols_by_name_lc: dict[str, tuple[SymbolEntry, ...]] = field(
        default_factory=dict
    )
    # Cache hit/miss/dirty/write counters. Populated only when the
    # workspace check ran with a CacheStore. Surfaced by --timings.
    cache_hits: int = 0
    cache_misses: int = 0
    cache_dirty: int = 0
    cache_writes: int = 0


# ---------------------------------------------------------------------------
# Internal load state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    """Per-file intermediate state for one workset pass.

    On ``.F90`` files where CPP preprocessing is needed (to make
    modules buried inside ``#ifdef`` blocks visible), two trees are
    kept: a primary cpp-expanded tree the checker walks, and a raw
    tree the LSP enrichment handlers walk because it carries
    on-disk-source coordinates. Diagnostics emitted on the primary
    tree are remapped via ``line_map`` before publish.

    Attributes:
        path: Resolved absolute path of the file.
        text: Original source text (pre-cpp on ``.F90``).
        source: Bytes ``tree`` was actually built from (cpp-expanded
            when cpp ran, otherwise identical to ``text.encode()``).
        scan: Annotation-scan result for the file.
        attachment: Annotation-attachment result.
        tree: Primary parse tree; ``None`` only if the file couldn't
            be read.
        load_error: Error string when loading failed, ``None``
            otherwise.
        line_map: Per-line expanded-to-source mapping when cpp ran,
            ``None`` for raw-parsed files.
        raw_tree: Raw-source tree, populated only when cpp ran.
        raw_source: Bytes for ``raw_tree``.
        cpp_closure: Set of include files cpp pulled in (used to feed
            include-hashing for the cache key).
        source_hash: SHA-256 hex of :attr:`source` (the bytes the
            primary :attr:`tree` was built from — cpp-expanded for
            cpp files, raw otherwise). Computed in ``_load_one`` and
            reused by the index loop's ``ExportsKey`` so the workset
            doesn't hash each file twice. Empty string on load
            failure.
    """

    path: Path
    text: str
    source: bytes              # bytes ``tree`` was actually built from
    scan: object
    attachment: AttachmentResult
    tree: Tree | None          # None only if the file couldn't be read
    load_error: str | None
    line_map: tuple[int | None, ...] | None = None
    raw_tree: Tree | None = None
    raw_source: bytes | None = None
    cpp_closure: frozenset[str] = frozenset()
    source_hash: str = ""


def _load_one(
    path: Path,
    *,
    overrides: dict[Path, str],
    cpp_defines: tuple[str, ...] = (),
    include_paths: tuple[Path, ...] = (),
    unit_patterns: tuple[UnitPattern, ...] = DEFAULT_UNIT_PATTERNS,
    assume_patterns: tuple[StructuredPattern, ...] = DEFAULT_ASSUME_PATTERNS,
    affine_patterns: tuple[StructuredPattern, ...] = DEFAULT_AFFINE_PATTERNS,
    tree_cache: TreeCache | None = None,
    projection_cache: ProjectionCache | None = None,
    patterns_fp: str = "",
) -> _Loaded:
    """Read source, scan + attach annotations, parse with tree-sitter.

    For ``.F90`` files the system ``cpp`` is pre-run using the
    project's ``cpp_defines`` / ``include_paths`` (from
    ``.dimfort.toml``). Without this, modules whose ``module NAME``
    statement sits inside an ``#ifdef X`` block surface as an ERROR
    span and downstream consumers fire U007 even though the module
    exists. The cost is ~10 ms per ``.F90`` file (system cpp); on a
    workset where the directives aren't used (no defines configured)
    cpp is skipped entirely.

    Buffer overrides bypass cpp regardless — editor edits hit the
    in-memory text, which we want parsed verbatim.

    Args:
        path: File to load.
        overrides: Optional in-memory source overrides keyed by
            resolved path; lets the LSP feed unsaved buffer contents.
        cpp_defines: ``-D`` flags forwarded to ``cpp``.
        include_paths: ``-I`` paths forwarded to ``cpp``.
        unit_patterns: Configured ``@unit{...}``-family delimiters.
        assume_patterns: Configured ``@unit_assume{...}`` delimiters.
        affine_patterns: Configured ``@unit_affine_conversion{...}``
            delimiters.
        tree_cache: Optional session-scoped tree cache; on a hit the
            parse step is skipped entirely and the cached
            tree-sitter outputs replay into ``_Loaded``.
        projection_cache: Optional session-scoped scan+attach cache;
            on a hit, both walks are skipped and the cached outputs
            replay into ``_Loaded``.
        patterns_fp: Pre-computed annotation-patterns fingerprint
            (from :func:`patterns_fingerprint`). When empty and
            ``projection_cache`` is set, ``_load_one`` recomputes the
            fingerprint per call — the caller is expected to compute
            it once per ``check_files`` call and thread it through.

    Returns:
        A ``_Loaded`` record. On any pre-parse failure (read,
        scan/attach, cpp, tree-sitter), the record carries
        ``tree=None`` and a populated ``load_error`` so the workset
        pass can continue without aborting.
    """
    from dimfort.core._source_io import read_text
    text = overrides[path] if path in overrides else read_text(path)
    source = text.encode("utf-8")

    # cpp short-circuit: even when defines/includes are configured, a
    # ``.F90`` file with no ``#`` directives produces output identical
    # to its input, so spawning a subprocess for ~10 ms each is pure
    # overhead. Cheap text scan first; bypass cpp when no directive
    # line is present. (Real-world: ~half the .F90 files have no directives.)
    has_directives = text.startswith("#") or "\n#" in text
    use_cpp = (
        path.suffix == ".F90"
        and path not in overrides
        and (cpp_defines or include_paths)
        and has_directives
    )
    # Hash once and reuse for both TreeKey here and ExportsKey in the
    # index loop (via _Loaded.source_hash). Halves SHA-256 work per file.
    src_hash = _mfc.content_hash(source)
    cache_key: TreeKey | None = None
    hit: _mfc.CachedParse | None = None
    if tree_cache is not None:
        mode = (
            f"cpp:{_mfc.cpp_fingerprint(cpp_defines, include_paths)}"
            if use_cpp
            else "raw"
        )
        cache_key = TreeKey(src_hash, mode)
        hit = tree_cache.get(cache_key)

    # Source-coordinate tree, shared between scan_text and the result.
    # On a cache hit, take it from the cached entry (raw_tree for cpp,
    # tree otherwise); on a miss, parse once now. tree-sitter rarely
    # fails on valid bytes; on the rare failure, scan_text falls back
    # to its internal parse and the surrounding error path catches it
    # below.
    source_tree: Tree | None = None
    parse_error: str | None = None
    if hit is not None:
        source_tree = hit.raw_tree if use_cpp else hit.tree
    else:
        try:
            source_tree = _ts.parse_text(source)
        except Exception as exc:
            parse_error = str(exc)

    # Projection cache (M1): when the file's content + patterns
    # haven't changed, the scan + attach outputs are identical to last
    # call. Skip both walks on hit. Patterns fingerprint is computed
    # once per check_files call by the caller; falls back to a fresh
    # fingerprint here when ``_load_one`` is called outside that path.
    proj_key: ProjectionKey | None = None
    if projection_cache is not None:
        fp = patterns_fp or patterns_fingerprint(
            unit_patterns, assume_patterns, affine_patterns,
        )
        proj_key = ProjectionKey(src_hash, fp)
        proj_hit = projection_cache.get(proj_key)
        if proj_hit is not None:
            scan = proj_hit.scan
            attachment = proj_hit.attachment
        else:
            scan = scan_text(
                text,
                unit_patterns=unit_patterns,
                assume_patterns=assume_patterns,
                affine_patterns=affine_patterns,
                tree=source_tree,
            )
            attachment = attach(scan)
            projection_cache.put(
                proj_key, CachedProjection(scan=scan, attachment=attachment),
            )
    else:
        scan = scan_text(
            text,
            unit_patterns=unit_patterns,
            assume_patterns=assume_patterns,
            affine_patterns=affine_patterns,
            tree=source_tree,
        )
        attachment = attach(scan)

    if hit is not None:
        # Non-cpp: source_hash == src_hash. Cpp: source_hash is the
        # post-cpp digest stored at cache-write time.
        if use_cpp:
            return _Loaded(
                path, text, hit.source, scan, attachment,
                hit.tree, None, line_map=hit.line_map,
                raw_tree=hit.raw_tree, raw_source=source,
                cpp_closure=hit.cpp_closure,
                source_hash=hit.source_hash or _mfc.content_hash(hit.source),
            )
        return _Loaded(
            path, text, hit.source, scan, attachment, hit.tree, None,
            source_hash=src_hash,
        )

    if parse_error is not None and not use_cpp:
        # tree-sitter shouldn't fail on valid bytes, but the workset
        # contract is uniform-shape entries.
        return _Loaded(
            path, text, source, scan, attachment, None, parse_error,
            source_hash=src_hash,
        )

    try:
        if use_cpp:
            try:
                pre = _ts.parse_with_cpp(
                    path, defines=cpp_defines, include_paths=include_paths,
                )
            except _ts.CppFailedError as exc:
                # cpp couldn't preprocess this file (missing include,
                # syntax error in a directive). Surface as a load
                # failure; tree-sitter's raw parse would garble
                # continuations anyway.
                return _Loaded(
                    path, text, source, scan, attachment, None, exc.stderr,
                    source_hash=src_hash,
                )
            # Two-tree mode: cpp'd tree for the checker (correct
            # semantics), raw tree for the LSP (source-coordinate
            # positions). The raw tree was already parsed above (or
            # parse failed there; re-attempt for the cpp branch).
            raw_tree = source_tree if source_tree is not None else _ts.parse_text(source)
            # Cpp-expanded bytes differ from raw, so we have to hash
            # twice on cpp files. Non-cpp files (the common case) only
            # pay one hash because src_hash matches the post-cpp hash.
            post_cpp_hash = _mfc.content_hash(pre.expanded_text)
            if cache_key is not None and tree_cache is not None:
                tree_cache.put(cache_key, _mfc.CachedParse(
                    tree=pre.tree, source=pre.expanded_text,
                    expanded_text=pre.expanded_text, line_map=pre.line_map,
                    raw_tree=raw_tree, cpp_closure=pre.cpp_closure,
                    source_hash=post_cpp_hash,
                ))
            return _Loaded(
                path, text, pre.expanded_text, scan, attachment,
                pre.tree, None, line_map=pre.line_map,
                raw_tree=raw_tree, raw_source=source,
                cpp_closure=pre.cpp_closure, source_hash=post_cpp_hash,
            )
        # By this point the no-cpp path has either returned with a
        # parse_error _Loaded (above) or source_tree is non-None.
        # Assert keeps mypy happy without changing runtime behavior.
        assert source_tree is not None
        if cache_key is not None and tree_cache is not None:
            tree_cache.put(
                cache_key, _mfc.CachedParse(
                    tree=source_tree, source=source, source_hash=src_hash,
                ),
            )
    except Exception as exc:
        return _Loaded(
            path, text, source, scan, attachment, None, str(exc),
            source_hash=src_hash,
        )
    return _Loaded(
        path, text, source, scan, attachment, source_tree, None,
        source_hash=src_hash,
    )


def _remap_diagnostic(
    d: Diagnostic, line_map: tuple[int | None, ...] | None,
) -> Diagnostic:
    """Remap a diagnostic's positions from expanded → source coordinates.

    No-op for files parsed raw (``line_map is None``). Lines that came
    from an ``#include`` (``line_map[idx] is None``) are clamped to
    the nearest known source line so the diagnostic still publishes
    somewhere useful rather than being silently dropped.

    Args:
        d: Diagnostic emitted on cpp-expanded coordinates.
        line_map: Per-line expanded-to-source mapping from
            ``_Loaded.line_map``.

    Returns:
        A new :class:`Diagnostic` with start/end lines rewritten to
        source coordinates (or the original when no remap is needed).
    """
    if line_map is None:
        return d

    def _src(line_1based: int) -> int:
        """Map a 1-based expanded line to its source line (clamping)."""
        idx = line_1based - 1
        if 0 <= idx < len(line_map):
            mapped = line_map[idx]
            if mapped is not None:
                return mapped
            # Walk backward to the previous source-line entry.
            for j in range(idx - 1, -1, -1):
                if line_map[j] is not None:
                    return line_map[j]  # type: ignore[return-value]
        return line_1based

    return Diagnostic(
        file=d.file,
        start=Position(_src(d.start.line), d.start.column),
        end=Position(_src(d.end.line), d.end.column),
        severity=d.severity,
        code=d.code,
        message=d.message,
    )


# ---------------------------------------------------------------------------
# Diagnostic helpers
# ---------------------------------------------------------------------------


def _u007(path: Path, message: str) -> Diagnostic:
    """Build a file-level U007 (missing-module / unloadable-file) diagnostic.

    Anchored at line/column 0 because U007 fires at file scope rather
    than on a particular statement.
    """
    return Diagnostic(
        file=str(path),
        start=Position(0, 0),
        end=Position(0, 0),
        severity=Severity.ERROR,
        code="U007",
        message=message,
    )


def _attachment_diags(
    file: str,
    att: AttachmentResult,
    assignment_line_ranges: tuple[tuple[int, int], ...] = (),
) -> list[Diagnostic]:
    """Surface attach-time issues (orphan annotations, conflicts, U010).

    When an ``@unit{}`` orphan lands on an assignment statement, that
    is a wrong-statement-kind situation (spec §8.3 → U023) rather than
    a plain orphan (U006).

    Args:
        file: Stringified path of the file owning ``att``.
        att: Attachment result produced by :func:`attach`.
        assignment_line_ranges: Line ranges of assignment statements
            in the file; used to upgrade an orphan that lands on one
            of those ranges from U006 to U023.

    Returns:
        Diagnostics for every orphan, attach-time conflict, and
        intermediate-continuation error (U010) found.
    """
    out: list[Diagnostic] = []
    for orph in att.orphans:
        msg = orph.reason
        if msg and not msg[:1].isupper():
            msg = msg[:1].upper() + msg[1:]
        check_line = orph.target_line or orph.line
        on_assignment = any(
            lo <= check_line <= hi for lo, hi in assignment_line_ranges
        )
        if on_assignment:
            end_col = orph.end_column or (orph.column + 1)
            out.append(
                Diagnostic(
                    file=file,
                    start=Position(orph.line, orph.column),
                    end=Position(orph.line, end_col),
                    severity=Severity.WARNING,
                    code="U023",
                    message=(
                        "@unit landed on an assignment statement; "
                        "@unit attaches to declarations. Did you mean "
                        "@unit_assume or @unit_affine_conversion?"
                    ),
                )
            )
            continue
        out.append(
            Diagnostic(
                file=file,
                start=Position(orph.line, orph.column),
                end=Position(orph.line, orph.column),
                severity=Severity.WARNING,
                code="U006",
                message=msg,
            )
        )
    for confl in att.conflicts:
        out.append(
            Diagnostic(
                file=file,
                start=Position(confl.second_line, 0),
                end=Position(confl.second_line, 0),
                severity=Severity.ERROR,
                code="U-conflict",
                message=(
                    f"Conflicting unit for {confl.variable!r}: "
                    f"{confl.first_unit} vs {confl.second_unit}"
                ),
            )
        )
    for inter in att.intermediate_continuations:
        out.append(
            Diagnostic(
                file=file,
                start=Position(inter.line, inter.column),
                end=Position(inter.line, inter.column),
                severity=Severity.ERROR,
                code="U010",
                message=inter.reason,
            )
        )
    return out


def _digest_text_dict(text: Mapping[Any, str], /) -> str:
    """Stable short hash of a ``str → str`` (or tuple-keyed) text dict.

    Used as a key into the parsed-unit-table memo; sorts keys so two
    dicts with the same contents but different insertion order
    digest the same.
    """
    h = hashlib.sha256()
    for k in sorted(text, key=repr):
        h.update(repr(k).encode("utf-8"))
        h.update(b"=")
        h.update(text[k].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


def _parse_var_units(
    text: dict[str, str],
    table: UnitTable,
    *,
    memo: dict[tuple[str, int], object] | None = None,
) -> dict[str, UnitExpr]:
    """Parse every ``name → unit-text`` entry against ``table``.

    Entries that fail to parse are silently dropped; the U002
    diagnostic is emitted from a separate code path so the parsing
    failure is surfaced exactly once.

    Args:
        text: Map of variable names to unit-text strings.
        table: Unit table the strings parse against.
        memo: Optional ``(text_digest, id(table)) → parsed`` dict.
            When supplied, identical inputs return the cached parsed
            table instead of re-parsing every string.
    """
    key: tuple[str, int] | None = None
    if memo is not None:
        key = (_digest_text_dict(text), id(table))
        cached = memo.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
    out: dict[str, UnitExpr] = {}
    for name, raw in text.items():
        try:
            out[name] = _units_mod.parse(raw, table)
        except UnitError:
            continue
    if memo is not None and key is not None:
        memo[key] = out
    return out


def _parse_var_units_by_scope(
    text: dict[tuple[str | None, str], str],
    table: UnitTable,
    *,
    memo: dict[tuple[str, int], object] | None = None,
) -> dict[tuple[str | None, str], UnitExpr]:
    """Scope-keyed variant of :func:`_parse_var_units`.

    Keys are ``(scope_lc_or_None, name)`` pairs. Unparseable entries
    are dropped (U002 is emitted elsewhere).

    Args:
        text: Map of ``(scope, name) → unit-text`` entries.
        table: Unit table the strings parse against.
        memo: Optional memo (same shape as :func:`_parse_var_units`).
    """
    key: tuple[str, int] | None = None
    if memo is not None:
        key = (_digest_text_dict(text), id(table))
        cached = memo.get(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
    out: dict[tuple[str | None, str], UnitExpr] = {}
    for k, raw in text.items():
        try:
            out[k] = _units_mod.parse(raw, table)
        except UnitError:
            continue
    if memo is not None and key is not None:
        memo[key] = out
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _digest_module_exports(
    exports: ModuleExports | None,
    *,
    memo: dict[int, str] | None = None,
) -> str:
    """Stable hex digest of a module's exports, used for dep-validation.

    A cached file's entry is dirty when any module it consumed has a
    different digest now vs. when the entry was written.

    Args:
        exports: Module exports record, or ``None`` when the module is
            no longer in the workspace.
        memo: Optional ``id(exports) → digest`` dict; when supplied,
            the digest is computed at most once per ``exports``
            object lifetime. Owned by
            :class:`~dimfort.core.multifile_cache.ModuleExportsCache`
            in LSP runs; ``None`` in CLI / test paths is functionally
            identical, just slower.

    Returns:
        A SHA-256 hex digest of the serialised exports, or the literal
        ``"absent"`` sentinel when ``exports`` is ``None`` (so
        disappearance is treated as "changed").
    """
    if exports is None:
        return "absent"
    if memo is not None:
        cached = memo.get(id(exports))
        if cached is not None:
            return cached
    blob = json.dumps(
        dump_module_exports(exports), sort_keys=True, separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(blob).hexdigest()
    if memo is not None:
        memo[id(exports)] = digest
    return digest


def _hash_file(path: Path) -> str:
    """Hex SHA-256 of a file's contents, ``""`` if missing.

    Used to feed ``units_file_hash`` into the per-file cache key so a
    project-units-table edit invalidates cached diagnostics.

    Args:
        path: File to hash.

    Returns:
        Lowercase hex digest, or the empty string on ``OSError``.
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _build_cache_config_view(
    *,
    external_modules: frozenset[str],
    cpp_defines: tuple[str, ...],
    include_paths: tuple[Path, ...],
    units_file: Path | None,
    diagnostic_severities: dict[str, str] | None,
    scale_mode: bool,
    unit_patterns: tuple[UnitPattern, ...],
    assume_patterns: tuple[StructuredPattern, ...],
    affine_patterns: tuple[StructuredPattern, ...],
) -> dict[str, object]:
    """Assemble the per-file-affecting config dict for the cache key.

    Every dimension that can change a file's diagnostics for the same
    source bytes must contribute here; see
    :data:`dimfort.core.cache_key.PER_FILE_CONFIG_KEYS`.

    Returns:
        A plain dict carrying every cache-key-affecting setting in a
        JSON-serialisable shape.
    """
    return {
        "external_modules": external_modules,
        "extra_defines": list(cpp_defines),
        "extra_include_paths": [str(p) for p in include_paths],
        "units_file_hash": _hash_file(units_file) if units_file else "",
        "diagnostic_severities": dict(diagnostic_severities or {}),
        "scale_mode": scale_mode,
        "unit_comment_delimiters": [
            [p.open, p.close] for p in unit_patterns
        ],
        "unit_assume_comment_delimiters": [
            [p.open, p.close, p.sep] for p in assume_patterns
        ],
        "unit_affine_comment_delimiters": [
            [p.open, p.close, p.sep] for p in affine_patterns
        ],
    }


def _try_replay_from_cache(
    *,
    cache: CacheStore,
    include_hasher: IncludeHasher,
    entry: _Loaded,
    cache_config_view: dict[str, object],
    module_exports: dict[str, ModuleExports],
    result: WorksetResult,
    digest_memo: dict[int, str] | None = None,
) -> tuple[str | None, list[Diagnostic] | None]:
    """Attempt to serve an entry from cache.

    Args:
        cache: The opened cache store.
        include_hasher: Memoised include-file hasher used to fold the
            cpp closure into the cache key.
        entry: Loaded per-file state.
        cache_config_view: Settings dict from
            :func:`_build_cache_config_view`.
        module_exports: Current workspace module-exports map (used to
            validate dep digests).
        result: Workset result whose cache counters are mutated.
        digest_memo: Optional ``id(exports) → digest`` dict shared
            across calls so each module's dep digest is computed at
            most once per session.

    Returns:
        A ``(key, diags)`` pair:

        * ``(key, [..])`` — cache hit, validated; replay these
          diagnostics and skip the fresh check pass.
        * ``(key, None)`` — cache miss or dep-dirty; caller runs check
          and may write back under ``key``.
        * ``(None, None)`` — key could not be computed (e.g. include
          hashing raised ``OSError``); caller proceeds without
          caching.
    """
    try:
        closure_hashes = include_hasher.hash_closure(entry.cpp_closure)
    except OSError:
        return None, None

    key = compute_file_key(
        source_bytes=entry.source,
        cpp_closure_hashes=closure_hashes,
        config=cache_config_view,
    )
    payload = cache.read(key)
    if payload is None:
        result.cache_misses += 1
        return key, None

    # Validate deps: every consumed module's current digest must
    # match what we stored when the entry was written.
    deps_snapshot = payload.get("deps_signature", {})
    for mod_lc, stored_digest in deps_snapshot.items():
        current = _digest_module_exports(
            module_exports.get(mod_lc), memo=digest_memo,
        )
        if current != stored_digest:
            result.cache_dirty += 1
            return key, None

    result.cache_hits += 1
    return key, [load_diagnostic(d) for d in payload.get("diagnostics", [])]


def _write_cache_entry(
    *,
    cache: CacheStore,
    key: str,
    deps_consumed: frozenset[str],
    module_exports: dict[str, ModuleExports],
    remapped_diags: list[Diagnostic],
    result: WorksetResult,
    digest_memo: dict[int, str] | None = None,
) -> None:
    """Persist a fresh check's output to the cache.

    Best-effort: ``OSError`` from the underlying write is swallowed so
    a transient cache failure never aborts a workset pass.

    Args:
        cache: The opened cache store.
        key: Cache key computed earlier by
            :func:`_try_replay_from_cache`.
        deps_consumed: Modules this file's check output depends on.
        module_exports: Current workspace module-exports map (used to
            stamp dep digests into the payload).
        remapped_diags: Already-remapped diagnostics; cached as-is so
            replay restores source-coordinate positions without a
            second remap.
        result: Workset result whose ``cache_writes`` counter is
            incremented on success.
        digest_memo: Optional ``id(exports) → digest`` dict shared
            across calls so each module's dep digest is computed at
            most once per session.
    """
    deps_signature = {
        mod_lc: _digest_module_exports(
            module_exports.get(mod_lc), memo=digest_memo,
        )
        for mod_lc in deps_consumed
    }
    try:
        cache.write(
            key,
            {
                # Payload-shape sharding lives in CHECKER_OUTPUT_VERSION;
                # the ``schema`` key that used to ride along here was
                # never read by any consumer, so it's dropped.
                "deps_signature": deps_signature,
                "diagnostics": [dump_diagnostic(d) for d in remapped_diags],
            },
        )
        result.cache_writes += 1
    except OSError:
        pass


def check_files(
    sources: list[Path],
    *,
    table: UnitTable | None = None,
    overrides: dict[Path, str] | None = None,
    external_modules: frozenset[str] = frozenset(),
    cpp_defines: tuple[str, ...] = (),
    include_paths: tuple[Path, ...] = (),
    progress_cb: Callable[[str, int, int, Path], None] | None = None,
    max_load_workers: int | None = None,
    cache: CacheStore | None = None,
    cache_mode: str = "off",
    units_file: Path | None = None,
    diagnostic_severities: dict[str, str] | None = None,
    scale_mode: bool = False,
    unit_patterns: tuple[UnitPattern, ...] = DEFAULT_UNIT_PATTERNS,
    assume_patterns: tuple[StructuredPattern, ...] = DEFAULT_ASSUME_PATTERNS,
    affine_patterns: tuple[StructuredPattern, ...] = DEFAULT_AFFINE_PATTERNS,
    tree_cache: TreeCache | None = None,
    exports_cache: ModuleExportsCache | None = None,
    projection_cache: ProjectionCache | None = None,
    outer_lock: threading.Lock | None = None,
    lock_yield_every: int = 50,
) -> WorksetResult:
    """Scan, attach, and check every file in ``sources`` together.

    The public entry point used by both the CLI and the LSP. Runs the
    four pipeline phases documented at module level and returns a
    populated :class:`WorksetResult`.

    Args:
        sources: Files to load and check.
        table: Optional unit table; defaults to
            :data:`dimfort.core.units.DEFAULT_TABLE`.
        overrides: Unsaved buffer contents keyed by absolute path
            (typically passed by the LSP).
        external_modules: Allowlist of module names treated as
            known-out-of-workset so their unresolved ``use`` clauses
            don't fire U007.
        cpp_defines: ``-D`` flags forwarded to ``cpp`` for ``.F90``
            files that need preprocessing.
        include_paths: ``-I`` paths forwarded to ``cpp``.
        progress_cb: Optional callback receiving
            ``(phase, current, total, path)`` ticks during load /
            index / check phases so the LSP status bar stays
            informative on large worksets.
        max_load_workers: Override for the load-phase thread pool
            size; default is one less than the CPU count.
        cache: Optional opened :class:`CacheStore` for per-file
            content-hash caching.
        cache_mode: One of ``"off"`` / ``"read-only"`` /
            ``"read-write"``; ``"off"`` keeps the cache untouched.
        units_file: Project units extension file; its content hash
            feeds the per-file cache key.
        diagnostic_severities: Per-rule severity overrides.
        scale_mode: Enables S001 / S002 scale checking.
        unit_patterns: Configured ``@unit{...}``-family delimiters.
        assume_patterns: Configured ``@unit_assume{...}`` delimiters.
        affine_patterns: Configured ``@unit_affine_conversion{...}``
            delimiters.
        tree_cache: Optional session-scoped
            :class:`~dimfort.core.multifile_cache.TreeCache`; when set,
            unchanged files skip tree-sitter parsing entirely. ``None``
            (the default) keeps behaviour byte-identical to the
            un-cached path.
        exports_cache: Optional session-scoped
            :class:`~dimfort.core.multifile_cache.ModuleExportsCache`;
            when set, unchanged files skip the per-file
            ``collect_function_signatures_and_module_exports`` walk in
            the index phase.
        projection_cache: Optional session-scoped
            :class:`~dimfort.core.multifile_cache.ProjectionCache`;
            when set, unchanged files skip the ``scan_text`` +
            ``attach`` passes during the load phase.
        outer_lock: When the caller is holding a lock around this whole
            ``check_files`` call (typically the LSP's ``check_lock``,
            held by the workspace coverage refresh) and wants the lock
            yielded periodically so other handlers can slot in, pass
            it here. Phase D's per-file loop releases it every
            ``lock_yield_every`` files and re-acquires before continuing.
            ``None`` (default) means no yielding — the lock, if any,
            stays held for the whole call.
        lock_yield_every: Lock-yield cadence in files. Default 50 →
            ~0.5 s of work per yield window on a typical real-world
            workset, small enough that a yielded-in ``didChange``
            check (~30-file closure, ~0.5 s) completes in one window
            without re-triggering the outer-lock contention.

    Returns:
        A :class:`WorksetResult` carrying per-file diagnostics,
        per-file ``(tree, source)`` for downstream LSP use, the
        resolved signatures / module exports, and timing / cache
        counters.

    Raises:
        RuntimeError: If no unit table is available (the caller must
            have imported :mod:`dimfort.core.unit_config` to install
            ``DEFAULT_TABLE``).
    """
    abs_sources = [Path(p).resolve() for p in sources]
    overrides_map = {Path(p).resolve(): t for p, t in (overrides or {}).items()}
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )

    result = WorksetResult()
    t_total_start = time.perf_counter()

    # Compute the patterns fingerprint once per call so every file's
    # ProjectionKey reuses the same string (cheap; the patterns rarely
    # change between calls but we don't want per-file recomputation).
    patterns_fp = (
        patterns_fingerprint(unit_patterns, assume_patterns, affine_patterns)
        if projection_cache is not None
        else ""
    )

    # Session-scoped memo (when an exports_cache is wired in) so the
    # 120k-call ``units.parse`` workload during Phase B + index +
    # check collapses on repeat calls. Per-call dict when no cache —
    # still helps because the same text dicts often recur across
    # phases of one call.
    parsed_units_memo: dict[tuple[str, int], object] = (
        exports_cache.parsed_units_memo if exports_cache is not None else {}
    )
    extract_uses_memo: dict[str, tuple[object, ...]] = (
        exports_cache.extract_uses_memo if exports_cache is not None else {}
    )

    # Phase A — load + parse in parallel.
    t_phase_start = time.perf_counter()
    total = len(abs_sources)
    workers = (
        max_load_workers
        if max_load_workers is not None
        else max(1, (multiprocessing.cpu_count() or 4) - 1)
    )
    load_slots: list[_Loaded | None] = [None] * total
    progress_lock = threading.Lock()
    progress_counter = [0]

    def _do_load(
        idx: int, src: Path
    ) -> tuple[int, Path, _Loaded | None, Exception | None]:
        """Thread-pool worker that loads one file.

        Returns a 4-tuple ``(idx, src, entry_or_None, exc_or_None)``
        so the outer loop can place the result at the correct slot
        and surface failures as :class:`FileLoadFailure` records
        without aborting the workset pass.
        """
        try:
            return idx, src, _load_one(
                src,
                overrides=overrides_map,
                cpp_defines=cpp_defines,
                include_paths=include_paths,
                unit_patterns=unit_patterns,
                assume_patterns=assume_patterns,
                affine_patterns=affine_patterns,
                tree_cache=tree_cache,
                projection_cache=projection_cache,
                patterns_fp=patterns_fp,
            ), None
        except Exception as exc:
            # Widened from ``OSError`` only — the three pre-parse
            # operations inside ``_load_one`` (``read_text``,
            # ``scan_text``, ``attach``) can raise ``UnicodeDecodeError``
            # on a binary file accidentally added to sources, or any
            # regression in the annotation pipeline can throw an
            # ``AttributeError`` etc.; without the wider catch a single
            # bad file would re-raise from ``fut.result()`` and abort
            # the entire workset check. Per-file failure is recorded
            # as a ``FileLoadFailure`` and the rest of the workset
            # proceeds.
            return idx, src, None, exc

    # Release the outer lock for the entire Phase A (load). The main
    # thread sits in ``as_completed()`` waiting on worker threads —
    # the lock provides no protection during that wait. Releasing it
    # lets active-file ``didChange`` slot in continuously instead of
    # waiting for the 6+ second load phase to finish. The tree +
    # exports caches mutated by ``_load_one`` workers are
    # individually thread-safe (their own Lock).
    if outer_lock is not None:
        outer_lock.release()
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(_do_load, i, src)
                for i, src in enumerate(abs_sources)
            ]
            for fut in as_completed(futures):
                idx, src, entry, err = fut.result()
                if err is not None:
                    result.load_failures[src] = FileLoadFailure(stderr=str(err))
                    empty_scan = scan_text("")
                    load_slots[idx] =_Loaded(
                        src, "", b"", empty_scan, attach(empty_scan), None, str(err),
                    )
                else:
                    load_slots[idx] =entry
                if progress_cb is not None:
                    with progress_lock:
                        progress_counter[0] += 1
                        n = progress_counter[0]
                    progress_cb("load", n, total, src)
    finally:
        if outer_lock is not None:
            outer_lock.acquire()
    loaded: list[_Loaded] = [e for e in load_slots if e is not None]
    if len(loaded) != total:
        raise RuntimeError("internal: parallel load left None entries")
    result.phase_timings["load"] = time.perf_counter() - t_phase_start

    # Phase B — aggregate annotation tables across the workset.
    t_phase_start = time.perf_counter()
    merged_var_units_text: dict[str, str] = {}
    merged_field_units_text: dict[tuple[str, str], str] = {}
    for entry in loaded:
        for vn, u in entry.attachment.var_units.items():
            merged_var_units_text.setdefault(vn, u)
        for k, u in entry.attachment.field_units.items():
            merged_field_units_text.setdefault(k, u)
    result.attachments = {entry.path: entry.attachment for entry in loaded}
    merged_var_units = _parse_var_units(
        merged_var_units_text, active_table, memo=parsed_units_memo,
    )
    result.merged_var_units = merged_var_units
    for (tn, fn), t in merged_field_units_text.items():
        try:
            result.merged_field_units[(tn, fn)] = _units_mod.parse(t, active_table)
        except UnitError:
            continue

    # Expose per-file (tree, source_bytes) for the LSP to reuse on
    # hover/inlay/goto. When cpp ran, we publish the raw-source tree
    # so positions match the on-disk file. Otherwise the primary tree
    # is already in source coordinates.
    for entry in loaded:
        if entry.tree is None:
            continue
        if entry.raw_tree is not None and entry.raw_source is not None:
            result.trees[entry.path] = (entry.raw_tree, entry.raw_source)
        else:
            result.trees[entry.path] = (entry.tree, entry.source)

    result.phase_timings["aggregate"] = time.perf_counter() - t_phase_start

    # Phase C — index modules + signatures across the workset.
    # Single combined walk per file via
    # ``collect_function_signatures_and_module_exports``: profiling
    # a reference workspace showed two separate walks here cost ~6-7s; one walk halves it.
    t_phase_start = time.perf_counter()
    module_exports: dict[str, ModuleExports] = {}
    global_signatures: dict[str, FuncSig] = {}
    # Per-file parsed scoped table — kept around for Phase D so we don't
    # re-parse the same annotations in the checker. Empty entries when a
    # file has no scoped annotations or failed to load.
    per_file_var_units_by_scope: dict[
        Path, dict[tuple[str | None, str], UnitExpr]
    ] = {}
    # ExportsCache reads "merged_var_units" as a fallback context; digest
    # it once per call so each file's lookup is O(1) rather than
    # re-hashing for every file.
    merged_units_digest = (
        _mfc.digest_merged_var_units(merged_var_units)
        if exports_cache is not None
        else ""
    )
    for i, entry in enumerate(loaded, start=1):
        if entry.tree is not None:
            file_scoped = _parse_var_units_by_scope(
                entry.attachment.var_units_by_scope, active_table,
                memo=parsed_units_memo,
            )
            per_file_var_units_by_scope[entry.path] = file_scoped
            # Pass the scoped table even when empty — switching to the
            # ``None`` branch would re-enable flat-var_units fallback,
            # which causes unannotated params (e.g. a NetCDF wrapper's
            # ``v``) to absorb annotations from same-named variables
            # elsewhere in the workset (e.g. a wind ``v: m/s``). The
            # by-scope path returns ``None`` for unannotated names,
            # which is the correct semantic.
            exports_key: ExportsKey | None = None
            cached_exports: (
                tuple[dict[str, FuncSig], dict[str, ModuleExports]] | None
            ) = None
            if exports_cache is not None:
                # ``entry.source_hash`` was computed once in ``_load_one``
                # over ``text.encode()`` (the raw on-disk bytes). Fall
                # back to hashing if a load path didn't populate it
                # (e.g. parallel error slot).
                src_hash = entry.source_hash or _mfc.content_hash(entry.source)
                exports_key = ExportsKey(src_hash, merged_units_digest)
                cached_exports = exports_cache.get(exports_key)
            if cached_exports is not None:
                sigs, modules = cached_exports
            else:
                sigs, modules = (
                    ts_checker.collect_function_signatures_and_module_exports(
                        entry.tree, merged_var_units, entry.source,
                        var_units_by_scope=file_scoped,
                    )
                )
                if exports_cache is not None and exports_key is not None:
                    exports_cache.put(exports_key, (sigs, modules))
            for mname, exp in modules.items():
                module_exports.setdefault(mname, exp)
            for fname, sig in sigs.items():
                global_signatures.setdefault(fname, sig)
        if progress_cb is not None:
            progress_cb("index", i, total, entry.path)
    result.signatures = global_signatures
    result.module_exports = module_exports
    # Transitive closure: memoised, computed once. The LSP imports panel
    # consults this; the per-file checker still uses direct ``use``
    # semantics via ``apply_use_clauses`` below.
    t_vars, t_sigs = compute_transitive_exports(module_exports)
    result.module_transitive_vars = t_vars
    result.module_transitive_sigs = t_sigs
    result.var_units_by_scope = per_file_var_units_by_scope
    result.phase_timings["index"] = time.perf_counter() - t_phase_start

    # Phase D — check each file with its imports merged in.
    t_phase_start = time.perf_counter()

    # Cache setup. ``cache_active`` gates every cache touch so the
    # default (no cache) path is untouched.
    cache_active = cache is not None and cache_mode in ("read-only", "read-write")
    cache_writes_enabled = cache_active and cache_mode == "read-write"
    include_hasher = IncludeHasher() if cache_active else None
    cache_config_view = _build_cache_config_view(
        external_modules=external_modules,
        cpp_defines=cpp_defines,
        include_paths=include_paths,
        units_file=units_file,
        diagnostic_severities=diagnostic_severities,
        scale_mode=scale_mode,
        unit_patterns=unit_patterns,
        assume_patterns=assume_patterns,
        affine_patterns=affine_patterns,
    )
    # Session-scoped memo when an exports_cache is wired in (LSP path);
    # per-call dict otherwise. Both forms still help within one call —
    # a workspace's ~1900 files typically share ~10-20 module-export
    # dependencies, each digested over and over inside the cache-replay
    # loop without this memo.
    digest_memo: dict[int, str] = (
        exports_cache.digest_memo if exports_cache is not None else {}
    )

    for di, entry in enumerate(loaded, start=1):
        # Periodic outer-lock yield. Gives other handlers (active-file
        # didChange / didSave, hover, panelInfo) a chance to slot in
        # during long workspace checks instead of waiting tens of
        # seconds for the lock. Doesn't affect the refresh's total
        # duration measurably; addresses the typing-freeze UX.
        if (
            outer_lock is not None
            and di > 1
            and di % lock_yield_every == 0
        ):
            outer_lock.release()
            time.sleep(0)  # surrender the timeslice to any waiter
            outer_lock.acquire()

        diags: list[Diagnostic] = []

        diags.extend(_attachment_diags(
            str(entry.path),
            entry.attachment,
            tuple(getattr(entry.scan, "assignment_line_ranges", ())),
        ))
        for wsk in getattr(entry.scan, "wrong_statement_kinds", ()):
            diags.append(
                Diagnostic(
                    file=str(entry.path),
                    start=Position(wsk.line, wsk.column),
                    end=Position(wsk.line, wsk.end_column or wsk.column + 1),
                    severity=Severity.WARNING,
                    code="U023",
                    message=(
                        f"{wsk.directive_found} landed on a "
                        f"{wsk.landed_on}; {wsk.directive_found} "
                        f"attaches to a different statement kind. "
                        f"Did you mean {wsk.expected_directive}?"
                    ),
                )
            )
        for err in getattr(entry.scan, "errors", ()):
            end_col = getattr(err, "end_column", 0) or (err.column + 1)
            msg = err.reason
            if msg and not msg[:1].isupper():
                msg = msg[:1].upper() + msg[1:]
            diags.append(
                Diagnostic(
                    file=str(entry.path),
                    start=Position(err.line, err.column),
                    end=Position(err.line, end_col),
                    severity=Severity.ERROR,
                    code="U001",
                    message=msg,
                )
            )
        for conf in getattr(entry.scan, "pattern_conflicts", ()):
            diags.append(
                Diagnostic(
                    file=str(entry.path),
                    start=Position(conf.line, conf.column),
                    end=Position(conf.line, conf.end_column or conf.column + 1),
                    severity=Severity.WARNING,
                    code="U021",
                    message=(
                        f"Conflicting {conf.directive} comment patterns: "
                        f"first-listed capture {conf.first_unit_text!r} "
                        f"applied; another configured pattern matched "
                        f"with {conf.second_unit_text!r}. Clarify by "
                        f"keeping only one form."
                    ),
                )
            )
        # Map each annotated name to its declaration line so a U002
        # (unparseable annotation) lands on the offending declaration
        # rather than at the top of the file. First-declared wins when
        # a name appears in several scopes — good enough to put the
        # squiggle on the right region.
        decl_line_for: dict[str, int] = {}
        for decl in getattr(entry.scan, "declarations", ()):
            for vn in decl.names:
                decl_line_for.setdefault(vn.lower(), decl.line_start)
        unparseable: set[str] = set()
        for name, text in entry.attachment.var_units.items():
            try:
                _units_mod.parse(text, active_table)
            except UnitError as exc:
                unparseable.add(name.lower())
                # Prefer the exact ``@unit{...}`` token span so the
                # squiggle lands on the annotation itself. Span columns
                # are 1-based in the checker's convention. Fall back to
                # the declaration line (col 0) when the span is missing
                # (e.g. intrinsic-default entries, which always parse).
                span = entry.attachment.var_units_span.get(name)
                if span is not None:
                    sline, scol, ecol = span
                    start = Position(sline, scol)
                    end = Position(sline, ecol)
                else:
                    line1 = decl_line_for.get(name.lower(), 0)
                    start = Position(line1, 0)
                    end = Position(line1, 0)
                suggestion = _suggest_rewrite(text, active_table)
                msg = f"Unit annotation for {name!r}: {exc}"
                if suggestion is not None:
                    msg += f"; did you mean {suggestion!r}?"
                diags.append(
                    Diagnostic(
                        file=str(entry.path),
                        start=start,
                        end=end,
                        severity=Severity.ERROR,
                        code="U002",
                        message=msg,
                        suggested_rewrite=suggestion,
                    )
                )
        if unparseable:
            result.unparseable_units[entry.path] = frozenset(unparseable)

        if entry.tree is None:
            diags.append(
                _u007(
                    entry.path,
                    f"Could not load this file: {entry.load_error or '(read error)'}",
                )
            )
            result.load_failures[entry.path] = FileLoadFailure(
                stderr=entry.load_error or ""
            )
            result.diagnostics[entry.path] = diags
            if progress_cb is not None:
                progress_cb("check", di, total, entry.path)
            continue

        # Scope each file to its OWN declared variables to avoid leaking
        # workset-wide name collisions. Cross-file imports arrive only
        # through explicit ``use`` clauses below.
        uses = extract_uses_memo.get(entry.text)
        if uses is None:
            uses = _wsi.extract_uses(entry.text)
            extract_uses_memo[entry.text] = uses
        file_var_units = _parse_var_units(
            entry.attachment.var_units, active_table, memo=parsed_units_memo,
        )
        per_file_var_units, per_file_sigs, unresolved = apply_use_clauses(
            uses, module_exports, file_var_units, global_signatures,
            external_modules=external_modules,
        )
        result.deps_consumed[entry.path] = deps_consumed_from_uses(
            uses, unresolved, external_modules,
        )
        for missing in unresolved:
            diags.append(
                _u007(entry.path, f"Module '{missing}' not found in workset")
            )

        # Make ``use``-imports resolvable in scope-aware mode without the
        # flat fallback (finding #018): merge the import delta — names not
        # declared in THIS file — into the by-scope table under the
        # ``(None, name)`` module layer. The file's own per-scope
        # annotations are untouched (setdefault), so a sibling routine's
        # unannotated param can no longer absorb a same-named unit from
        # elsewhere. Stored back on the result so the LSP's scope-aware
        # ``_Ctx`` resolves imports the same way (no second source of truth).
        own_scoped = per_file_var_units_by_scope.get(entry.path) or {}
        own_names_lc = {n.lower() for n in file_var_units}
        scoped_with_imports: dict[tuple[str | None, str], UnitExpr] = dict(own_scoped)
        for nm, uu in per_file_var_units.items():
            if nm.lower() not in own_names_lc:
                scoped_with_imports.setdefault((None, nm), uu)
        result.var_units_by_scope[entry.path] = scoped_with_imports

        # ---- cache lookup ------------------------------------------------
        cache_key: str | None = None
        replayed: list[Diagnostic] | None = None
        if cache_active and cache is not None and include_hasher is not None:
            cache_key, replayed = _try_replay_from_cache(
                cache=cache,
                include_hasher=include_hasher,
                entry=entry,
                cache_config_view=cache_config_view,
                module_exports=module_exports,
                result=result,
                digest_memo=digest_memo,
            )

        if replayed is not None:
            diags.extend(replayed)
        else:
            file_autocasts: list[AutocastEvent] = []
            check_diags = ts_checker.check(
                entry.tree,
                per_file_var_units,
                source=entry.source,
                file=str(entry.path),
                table=active_table,
                signatures=per_file_sigs,
                field_units=merged_field_units_text,
                var_units_by_scope=scoped_with_imports,
                routine_scopes=entry.attachment.routine_scopes,
                out_autocast_events=file_autocasts,
                assumes={
                    a.line: (a.unit_text, a.reason, a.column, a.end_column)
                    for a in getattr(entry.scan, "assumes", ())
                },
                affine_conversions={
                    a.line: (a.src, a.tgt, a.column)
                    for a in getattr(entry.scan, "affine_conversions", ())
                },
                scale_mode=scale_mode,
            )
            if file_autocasts:
                result.autocast_events[entry.path] = file_autocasts
            # Remap to source coordinates when the file went through cpp.
            # No-op when ``line_map`` is None (file parsed raw).
            # Cache the *remapped* diagnostics so replay restores
            # source-coordinate positions without a second remap.
            remapped = [_remap_diagnostic(d, entry.line_map) for d in check_diags]
            diags.extend(remapped)

            if cache_writes_enabled and cache_key is not None and cache is not None:
                _write_cache_entry(
                    cache=cache,
                    key=cache_key,
                    deps_consumed=result.deps_consumed[entry.path],
                    module_exports=module_exports,
                    remapped_diags=remapped,
                    result=result,
                    digest_memo=digest_memo,
                )

        result.diagnostics[entry.path] = diags
        if progress_cb is not None:
            progress_cb("check", di, total, entry.path)
    result.phase_timings["check"] = time.perf_counter() - t_phase_start
    result.phase_timings["total"] = time.perf_counter() - t_total_start

    return result
