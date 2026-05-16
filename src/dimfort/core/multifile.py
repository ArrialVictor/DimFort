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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tree_sitter import Tree

from dimfort.core import ts_checker
from dimfort.core import ts_parser as _ts
from dimfort.core import units as _units_mod
from dimfort.core import workspace_index as _wsi
from dimfort.core.annotations import scan_text
from dimfort.core.attach import attach, AttachmentResult
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
    # Function / subroutine signatures resolved across the whole workset.
    signatures: dict[str, FuncSig] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal load state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Loaded:
    """Per-file intermediate state for one workset pass."""

    path: Path
    text: str
    source: bytes              # raw UTF-8 bytes fed to tree-sitter
    scan: object
    attachment: AttachmentResult
    tree: Tree | None          # None only if the file couldn't be read
    load_error: str | None


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

    use_cpp = (
        path.suffix == ".F90"
        and path not in overrides
        and (cpp_defines or include_paths)
    )
    try:
        if use_cpp:
            try:
                pre = _ts.parse_with_cpp(
                    path, defines=cpp_defines, include_paths=include_paths,
                )
                # Replace source with the preprocessed bytes so node
                # positions / text extracts line up with what the tree
                # actually contains. Diagnostics will point at expanded
                # line numbers; line-map remapping back to source lives
                # in PreprocessedSource for future use.
                return _Loaded(
                    path, text, pre.expanded_text, scan, attachment, pre.tree, None,
                )
            except _ts.CppFailedError as exc:
                # cpp couldn't preprocess this file (missing include,
                # syntax error in a directive). Surface as a load
                # failure; tree-sitter's raw parse would garble
                # continuations anyway.
                return _Loaded(path, text, source, scan, attachment, None, exc.stderr)
        tree = _ts.parse_text(source)
    except Exception as exc:
        # tree-sitter shouldn't fail on valid bytes, but we mirror the
        # error path so callers can rely on a uniform _Loaded shape.
        return _Loaded(path, text, source, scan, attachment, None, str(exc))
    return _Loaded(path, text, source, scan, attachment, tree, None)


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

    # Phase A — load + parse in parallel.
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

    # Phase B — aggregate annotation tables across the workset.
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
    # hover/inlay/goto.
    for entry in loaded:
        if entry.tree is not None:
            result.trees[entry.path] = (entry.tree, entry.source)

    # Phase C — index modules + signatures across the workset.
    module_exports: dict[str, ModuleExports] = {}
    global_signatures: dict[str, FuncSig] = {}
    for i, entry in enumerate(loaded, start=1):
        if entry.tree is not None:
            for mname, exp in ts_checker.collect_module_exports(
                entry.tree, merged_var_units, entry.source
            ).items():
                module_exports.setdefault(mname, exp)
            for fname, sig in ts_checker.collect_function_signatures(
                entry.tree, merged_var_units, entry.source
            ).items():
                global_signatures.setdefault(fname, sig)
        if progress_cb is not None:
            progress_cb("index", i, total, entry.path)
    result.signatures = global_signatures

    # Phase D — check each file with its imports merged in.
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

        diags.extend(
            ts_checker.check(
                entry.tree,
                per_file_var_units,
                source=entry.source,
                file=str(entry.path),
                table=active_table,
                signatures=per_file_sigs,
                field_units=merged_field_units_text,
            )
        )
        result.diagnostics[entry.path] = diags
        if progress_cb is not None:
            progress_cb("check", di, total, entry.path)

    return result
