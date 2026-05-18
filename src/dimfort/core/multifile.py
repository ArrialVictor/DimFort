"""Multi-file orchestration (tree-sitter pipeline).

Runs scan → attach → check across a workset of Fortran files,
producing a :class:`WorksetResult` consumable by the CLI and LSP.

Pipeline:

1. **Phase A — load.** Per file, read source, scan annotations, attach
   them, and parse with tree-sitter. Runs in parallel via a thread pool;
   the tree-sitter C grammar releases the GIL during parsing.
2. **Phase B — aggregate annotations.** Merge per-file ``var_units`` /
   ``field_units`` tables; parse the strings to :class:`Unit` objects
   once.
3. **Phase C — index.** Walk every loaded tree to collect module
   exports and function/subroutine signatures so cross-file ``use`` and
   H004 work.
4. **Phase D — check.** Per file, apply its ``use`` clauses to splice
   imports into a local-scope copy of ``(var_units, signatures)``, then
   run :func:`ts_checker.check`.
"""
from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Tree

from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core import units as _units_mod
from dimfort.core import workspace_index as _wsi
from dimfort.core.annotations import scan_text
from dimfort.core.attach import AttachmentResult, attach
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.symbols import (
    FuncSig,
    ModuleExports,
    apply_use_clauses,
)
from dimfort.core.units import Unit, UnitError, UnitTable

# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileLoadFailure:
    """The file couldn't be read (or, vanishingly rare, tree-sitter raised)."""

    stderr: str


@dataclass
class WorksetResult:
    diagnostics: dict[Path, list[Diagnostic]] = field(default_factory=dict)
    attachments: dict[Path, AttachmentResult] = field(default_factory=dict)
    load_failures: dict[Path, FileLoadFailure] = field(default_factory=dict)
    compile_failures: dict[Path, str] = field(default_factory=dict)
    # Per-file ``(tree, source_bytes)`` pair populated below, used by
    # the LSP for hover/inlay/goto-time symbol lookup.
    trees: dict[Path, tuple[Tree, bytes]] = field(default_factory=dict)
    # Per-file parsed unit table (for hover formatting); same key set
    # as ``trees``.
    merged_var_units: dict[str, Unit] = field(default_factory=dict)
    merged_field_units: dict[tuple[str, str], Unit] = field(default_factory=dict)
    # Per-file scope-aware unit table. Key: file path → (scope_lc|None,
    # name) → Unit. Consumed by LSP hover/inlay so identifier lookups
    # honour the enclosing subroutine instead of falling back to the
    # flat merged_var_units (which is first-seen-wins across the
    # workset).
    var_units_by_scope: dict[Path, dict[tuple[str | None, str], Unit]] = field(
        default_factory=dict
    )
    # Function / subroutine signatures resolved across the whole workset.
    signatures: dict[str, FuncSig] = field(default_factory=dict)
    # Module-name → exports map, populated during Phase C. Reused by
    # the LSP for module hover (summary of exported vars/signatures)
    # and module-name goto-definition. Keyed by lower-cased module
    # name to match ``apply_use_clauses`` lookups.
    module_exports: dict[str, ModuleExports] = field(default_factory=dict)
    # Wall-clock seconds spent in each pipeline phase. Populated by
    # ``check_files``; consulted by the CLI's ``--timings`` flag and by
    # ad-hoc profiling scripts. Keys: "load", "aggregate", "index",
    # "check", "total".
    phase_timings: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal load state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    """Per-file intermediate state for one workset pass.

    On ``.F90`` files where CPP preprocessing is needed (to make
    modules buried inside ``#ifdef`` blocks visible), we keep two
    trees:

    - ``tree`` is the *primary* parse — the cpp-expanded one when cpp
      ran, the raw one otherwise. The checker walks this tree because
      it's where module / use semantics are visible.
    - ``raw_tree`` is the raw parse, populated only when ``tree``
      came from cpp. Its node positions match the on-disk file. The
      LSP enrichment handlers walk this tree because they receive
      cursor positions in source coordinates.

    Diagnostics emitted by the checker are remapped from expanded →
    source line numbers via ``line_map`` before publish.
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


def _load_one(
    path: Path,
    *,
    overrides: dict[Path, str],
    cpp_defines: tuple[str, ...] = (),
    include_paths: tuple[Path, ...] = (),
) -> _Loaded:
    """Read source, scan + attach annotations, parse with tree-sitter.

    ``overrides`` lets the LSP feed unsaved buffer contents instead of
    reading from disk.

    For ``.F90`` files we pre-run the system ``cpp`` using the
    project's ``cpp_defines`` / ``include_paths`` (from
    ``.dimfort.toml``). Without this, modules whose ``module NAME``
    statement sits inside an ``#ifdef X`` block surface as an ERROR
    span and downstream consumers fire U007 even though the module
    exists. The cost is ~10 ms per ``.F90`` file (system cpp); on a
    workset where the directives aren't used (no defines configured)
    we skip cpp entirely.

    Buffer overrides bypass cpp regardless — VSCode edits hit the
    in-memory text, which we want parsed verbatim.
    """
    from dimfort.core._source_io import read_text
    text = overrides.get(path) if path in overrides else read_text(path)
    source = text.encode("utf-8")
    scan = scan_text(text)
    attachment = attach(scan)

    # cpp short-circuit: even when defines/includes are configured, a
    # ``.F90`` file with no ``#`` directives produces output identical
    # to its input, so spawning a subprocess for ~10 ms each is pure
    # overhead. Cheap text scan first; bypass cpp when no directive
    # line is present. (LMDZ: ~half the .F90 files have no directives.)
    has_directives = text.startswith("#") or "\n#" in text
    use_cpp = (
        path.suffix == ".F90"
        and path not in overrides
        and (cpp_defines or include_paths)
        and has_directives
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
                return _Loaded(path, text, source, scan, attachment, None, exc.stderr)
            # Two-tree mode: cpp'd tree for the checker (correct
            # semantics), raw tree for the LSP (source-coordinate
            # positions). Cost is ~3 ms extra per .F90 file; tree-
            # sitter parses in single-digit ms.
            raw_tree = _ts.parse_text(source)
            return _Loaded(
                path, text, pre.expanded_text, scan, attachment,
                pre.tree, None, line_map=pre.line_map,
                raw_tree=raw_tree, raw_source=source,
            )
        tree = _ts.parse_text(source)
    except Exception as exc:
        # tree-sitter shouldn't fail on valid bytes, but we mirror the
        # error path so callers can rely on a uniform _Loaded shape.
        return _Loaded(path, text, source, scan, attachment, None, str(exc))
    return _Loaded(path, text, source, scan, attachment, tree, None)


def _remap_diagnostic(
    d: Diagnostic, line_map: tuple[int | None, ...] | None,
) -> Diagnostic:
    """Remap a diagnostic's positions from expanded → source coordinates.

    No-op for files parsed raw (``line_map is None``). Lines that came
    from an ``#include`` (``line_map[idx] is None``) are clamped to the
    nearest known source line so the diagnostic still publishes
    somewhere useful rather than being silently dropped.
    """
    if line_map is None:
        return d

    def _src(line_1based: int) -> int:
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
    return Diagnostic(
        file=str(path),
        start=Position(0, 0),
        end=Position(0, 0),
        severity=Severity.ERROR,
        code="U007",
        message=message,
    )


def _attachment_diags(file: str, att: AttachmentResult) -> list[Diagnostic]:
    """Surface attach-time issues (orphan annotations, conflicts, U010)."""
    out: list[Diagnostic] = []
    for orph in att.orphans:
        msg = orph.reason
        if msg and not msg[:1].isupper():
            msg = msg[:1].upper() + msg[1:]
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


def _parse_var_units(
    text: dict[str, str], table: UnitTable
) -> dict[str, Unit]:
    out: dict[str, Unit] = {}
    for name, raw in text.items():
        try:
            out[name] = _units_mod.parse(raw, table)
        except UnitError:
            continue
    return out


def _parse_var_units_by_scope(
    text: dict[tuple[str | None, str], str], table: UnitTable
) -> dict[tuple[str | None, str], Unit]:
    out: dict[tuple[str | None, str], Unit] = {}
    for key, raw in text.items():
        try:
            out[key] = _units_mod.parse(raw, table)
        except UnitError:
            continue
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


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
) -> WorksetResult:
    """Scan, attach, and check every file in ``sources`` together.

    ``overrides`` lets callers (typically the LSP) pass unsaved buffer
    contents keyed by absolute path. ``external_modules`` is the
    allowlist of module names treated as known-out-of-workset so their
    unresolved ``use`` clauses don't fire U007.

    ``progress_cb`` receives ``(phase, current, total, path)`` ticks
    during load / index / check phases so the LSP status bar stays
    informative on large worksets.
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

    # Phase A — load + parse in parallel.
    t_phase_start = time.perf_counter()
    total = len(abs_sources)
    workers = (
        max_load_workers
        if max_load_workers is not None
        else max(1, (multiprocessing.cpu_count() or 4) - 1)
    )
    loaded: list[_Loaded | None] = [None] * total
    progress_lock = threading.Lock()
    progress_counter = [0]

    def _do_load(idx: int, src: Path):
        try:
            return idx, src, _load_one(
                src,
                overrides=overrides_map,
                cpp_defines=cpp_defines,
                include_paths=include_paths,
            ), None
        except OSError as exc:
            return idx, src, None, exc

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_do_load, i, src) for i, src in enumerate(abs_sources)]
        for fut in as_completed(futures):
            idx, src, entry, err = fut.result()
            if err is not None:
                result.load_failures[src] = FileLoadFailure(stderr=str(err))
                empty_scan = scan_text("")
                loaded[idx] = _Loaded(
                    src, "", b"", empty_scan, attach(empty_scan), None, str(err),
                )
            else:
                loaded[idx] = entry
            if progress_cb is not None:
                with progress_lock:
                    progress_counter[0] += 1
                    n = progress_counter[0]
                progress_cb("load", n, total, src)
    loaded_files: list[_Loaded] = [e for e in loaded if e is not None]
    if len(loaded_files) != total:
        raise RuntimeError("internal: parallel load left None entries")
    loaded = loaded_files  # type: ignore[assignment]
    result.phase_timings["load"] = time.perf_counter() - t_phase_start

    # Phase B — aggregate annotation tables across the workset.
    t_phase_start = time.perf_counter()
    merged_var_units_text: dict[str, str] = {}
    merged_field_units_text: dict[tuple[str, str], str] = {}
    for entry in loaded:
        for n, u in entry.attachment.var_units.items():
            merged_var_units_text.setdefault(n, u)
        for k, u in entry.attachment.field_units.items():
            merged_field_units_text.setdefault(k, u)
    result.attachments = {entry.path: entry.attachment for entry in loaded}
    merged_var_units = _parse_var_units(merged_var_units_text, active_table)
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
    # LMDZ showed two separate walks here cost ~6-7s; one walk halves it.
    t_phase_start = time.perf_counter()
    module_exports: dict[str, ModuleExports] = {}
    global_signatures: dict[str, FuncSig] = {}
    # Per-file parsed scoped table — kept around for Phase D so we don't
    # re-parse the same annotations in the checker. Empty entries when a
    # file has no scoped annotations or failed to load.
    per_file_var_units_by_scope: dict[
        Path, dict[tuple[str | None, str], Unit]
    ] = {}
    for i, entry in enumerate(loaded, start=1):
        if entry.tree is not None:
            file_scoped = _parse_var_units_by_scope(
                entry.attachment.var_units_by_scope, active_table
            )
            per_file_var_units_by_scope[entry.path] = file_scoped
            # Pass the scoped table even when empty — switching to the
            # ``None`` branch would re-enable flat-var_units fallback,
            # which causes unannotated params (e.g. a NetCDF wrapper's
            # ``v``) to absorb annotations from same-named variables
            # elsewhere in the workset (e.g. a wind ``v: m/s``). The
            # by-scope path returns ``None`` for unannotated names,
            # which is the correct semantic.
            sigs, modules = ts_checker.collect_function_signatures_and_module_exports(
                entry.tree, merged_var_units, entry.source,
                var_units_by_scope=file_scoped,
            )
            for mname, exp in modules.items():
                module_exports.setdefault(mname, exp)
            for fname, sig in sigs.items():
                global_signatures.setdefault(fname, sig)
        if progress_cb is not None:
            progress_cb("index", i, total, entry.path)
    result.signatures = global_signatures
    result.module_exports = module_exports
    result.var_units_by_scope = per_file_var_units_by_scope
    result.phase_timings["index"] = time.perf_counter() - t_phase_start

    # Phase D — check each file with its imports merged in.
    t_phase_start = time.perf_counter()
    for di, entry in enumerate(loaded, start=1):
        diags: list[Diagnostic] = []

        diags.extend(_attachment_diags(str(entry.path), entry.attachment))
        for err in getattr(entry.scan, "errors", ()):
            diags.append(
                Diagnostic(
                    file=str(entry.path),
                    start=Position(err.line, err.column),
                    end=Position(err.line, err.column),
                    severity=Severity.ERROR,
                    code="U001",
                    message=err.reason,
                )
            )
        for name, text in entry.attachment.var_units.items():
            try:
                _units_mod.parse(text, active_table)
            except UnitError as exc:
                diags.append(
                    Diagnostic(
                        file=str(entry.path),
                        start=Position(0, 0),
                        end=Position(0, 0),
                        severity=Severity.ERROR,
                        code="U002",
                        message=f"Unit annotation for {name!r}: {exc}",
                    )
                )

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
        uses = _wsi.extract_uses(entry.text)
        file_var_units = _parse_var_units(
            entry.attachment.var_units, active_table
        )
        per_file_var_units, per_file_sigs, unresolved = apply_use_clauses(
            uses, module_exports, file_var_units, global_signatures,
            external_modules=external_modules,
        )
        for missing in unresolved:
            diags.append(
                _u007(entry.path, f"Module '{missing}' not found in workset")
            )

        check_diags = ts_checker.check(
            entry.tree,
            per_file_var_units,
            source=entry.source,
            file=str(entry.path),
            table=active_table,
            signatures=per_file_sigs,
            field_units=merged_field_units_text,
            var_units_by_scope=per_file_var_units_by_scope.get(entry.path),
            routine_scopes=entry.attachment.routine_scopes,
        )
        # Remap to source coordinates when the file went through cpp.
        # No-op when ``line_map`` is None (file parsed raw).
        diags.extend(_remap_diagnostic(d, entry.line_map) for d in check_diags)
        result.diagnostics[entry.path] = diags
        if progress_cb is not None:
            progress_cb("check", di, total, entry.path)
    result.phase_timings["check"] = time.perf_counter() - t_phase_start
    result.phase_timings["total"] = time.perf_counter() - t_total_start

    return result
