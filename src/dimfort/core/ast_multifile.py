"""Tree-sitter multi-file orchestration.

Wires the per-file tree-sitter checker (:mod:`dimfort.core.ts_checker`)
into a workset-wide pipeline: parse every file once, aggregate module
exports and signatures across the corpus, and check each file with its
imports merged in.

Previously parallel to :mod:`dimfort.core.multifile` (the LFortran ASR
pipeline) but now the sole orchestrator on ``main``. The LFortran-AST
backend it replaces was a transitional step — see ``docs/ast-only-
design.md`` for the history.

Phase 2 v1 deliberately keeps the public-by-default policy from F90's
implicit ``public`` rule — no ``private`` honouring yet.
"""
from __future__ import annotations

import multiprocessing
import os
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
from dimfort.core.annotations import scan_file, scan_text
from dimfort.core.attach import attach, AttachmentResult
from dimfort.core.checker import FuncSig
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.multifile import (
    WorksetResult,
    FileLoadFailure,
    _attachment_diags,
)
from dimfort.core.ts_checker import (
    ModuleExports,
    apply_use_clauses,
    collect_function_signatures,
    collect_module_exports,
)
from dimfort.core.units import Unit, UnitError, UnitTable


@dataclass(frozen=True)
class _Loaded:
    """Per-file intermediate state for a workset pass."""

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
    lfortran: str | os.PathLike[str] | None = None,  # accepted for caller compat; unused
    overrides: dict[Path, str],
    include_paths: tuple[Path, ...] = (),            # ditto
    cpp_defines: tuple[str, ...] = (),                # ditto
    cache_dir: Path | None = None,                   # ditto
) -> _Loaded:
    """Read source, scan + attach annotations, parse with tree-sitter.

    Tree-sitter parses in single-digit milliseconds for typical files
    and tens of milliseconds for the largest LMDZ modules, so the
    on-disk AST cache the LFortran backend needed is no longer
    necessary. The ``cache_dir`` and ``lfortran`` parameters are
    accepted for caller compatibility (CLI / LSP still pass them) but
    have no effect on this path.

    ``overrides`` lets the LSP feed unsaved buffer contents instead of
    reading from disk. CPP preprocessing for ``.F90`` files is **not**
    applied here — tree-sitter is error-tolerant around raw ``#ifdef``
    directives, which covers ~99% of LMDZ. The remaining files with
    continuations interleaved across ``#ifdef`` will get a tiny
    structural gap that we localise rather than fail on.
    """
    from dimfort.core._source_io import read_text
    text = overrides.get(path) if path in overrides else read_text(path)
    source = text.encode("utf-8")
    scan = scan_text(text)
    attachment = attach(scan)
    try:
        tree = _ts.parse_text(source)
    except Exception as exc:
        # tree-sitter shouldn't fail on valid bytes, but we mirror the
        # old error path so callers can rely on a uniform _Loaded shape.
        return _Loaded(path, text, source, scan, attachment, None, str(exc))
    return _Loaded(path, text, source, scan, attachment, tree, None)


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


def _u007(path: Path, message: str) -> Diagnostic:
    return Diagnostic(
        file=str(path),
        start=Position(0, 0),
        end=Position(0, 0),
        severity=Severity.ERROR,
        code="U007",
        message=message,
    )


def check_files_ast(
    sources: list[Path],
    *,
    lfortran: str | os.PathLike[str] | None = None,
    table: UnitTable | None = None,
    overrides: dict[Path, str] | None = None,
    external_modules: frozenset[str] = frozenset(),
    include_paths: tuple[Path, ...] = (),
    cpp_defines: tuple[str, ...] = (),
    cache_dir: Path | None = None,
    progress_cb: Callable[[str, int, int, Path], None] | None = None,
    max_load_workers: int | None = None,
) -> WorksetResult:
    """Scan, attach, and AST-check every file in ``sources`` together.

    Pipeline:
      1. Per file: read source, scan annotations, attach (var_units +
         field_units), dump AST. No subprocess beyond ``lfortran
         --show-ast`` once per file.
      2. Build a workset-wide ``module_exports`` table by walking
         every loaded AST.
      3. Per file: parse its ``use`` clauses, splice the imported
         symbols into a local-scope copy of ``(var_units,
         signatures)``, run :func:`ast_checker.check`.
      4. Collect diagnostics into the same :class:`WorksetResult`
         dataclass the ASR pipeline returns, so downstream consumers
         (CLI/LSP) work unchanged.
    """
    abs_sources = [Path(p).resolve() for p in sources]
    overrides_map = {Path(p).resolve(): t for p, t in (overrides or {}).items()}
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )

    result = WorksetResult()

    # Phase A: load every file in parallel. Each ``_load_one`` is
    # dominated by an ``lfortran --show-ast`` subprocess that releases
    # the GIL while running, so threads give us real parallelism
    # without the pickling overhead of a process pool.
    total = len(abs_sources)
    workers = (
        max_load_workers
        if max_load_workers is not None
        else max(1, (multiprocessing.cpu_count() or 4) - 1)
    )
    # Pre-size so we can drop results in by index — preserves source
    # order for downstream Phase B/C/D iteration.
    loaded: list[_Loaded | None] = [None] * total
    progress_lock = threading.Lock()
    progress_counter = [0]  # mutable via closure

    def _do_load(idx: int, src: Path) -> tuple[int, Path, _Loaded | None, OSError | None]:
        try:
            return idx, src, _load_one(
                src,
                lfortran=lfortran,
                overrides=overrides_map,
                include_paths=include_paths,
                cpp_defines=cpp_defines,
                cache_dir=cache_dir,
            ), None
        except OSError as exc:
            return idx, src, None, exc

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
    # All slots filled now; the type system can stop worrying.
    loaded_files: list[_Loaded] = [e for e in loaded if e is not None]
    if len(loaded_files) != total:
        raise RuntimeError("internal: parallel load left None entries")
    loaded = loaded_files  # type: ignore[assignment]

    # Phase B: aggregate annotation tables across the workset, parse to
    # Unit objects once. Local declarations still win when the same
    # name appears in multiple files (first-write-wins by iteration
    # order).
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
    # hover/inlay/goto — without re-parsing.
    for entry in loaded:
        if entry.tree is not None:
            result.trees[entry.path] = (entry.tree, entry.source)

    # Phase C: aggregate module exports and global signatures across
    # every successfully-loaded file.
    module_exports: dict[str, ModuleExports] = {}
    global_signatures: dict[str, FuncSig] = {}
    for i, entry in enumerate(loaded, start=1):
        if entry.tree is not None:
            for mname, exp in collect_module_exports(
                entry.tree, merged_var_units, entry.source
            ).items():
                module_exports.setdefault(mname, exp)
            for fname, sig in collect_function_signatures(
                entry.tree, merged_var_units, entry.source
            ).items():
                global_signatures.setdefault(fname, sig)
        if progress_cb is not None:
            progress_cb("index", i, total, entry.path)
    result.signatures = global_signatures

    # Phase D: check each file with its imports merged in.
    for di, entry in enumerate(loaded, start=1):
        diags: list[Diagnostic] = []

        # Attachment-time issues (orphans, conflicts, U010) — emitted
        # whether or not the AST loaded. Same source-side coverage as
        # the ASR pipeline.
        diags.extend(_attachment_diags(str(entry.path), entry.attachment))
        # Stage-1 malformed annotations (U001).
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
        # Per-file U002 for any unit annotation that didn't parse —
        # emit from the merged text table so the report matches the
        # ASR pipeline file-by-file.
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
            # The only way to reach this branch today is a read error
            # (OSError surfaced from _load_one). Tree-sitter itself
            # doesn't fail on raw bytes.
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

        uses = _wsi.extract_uses(entry.text)
        # Scope each file to its OWN declared variables. Workset-wide
        # name collisions (the same identifier annotated differently
        # in different files) used to leak through ``merged_var_units``
        # and cause false-positive H001s on files whose ``w`` came from
        # a sibling file. Cross-file imports still arrive via
        # ``apply_use_clauses`` — by name, through explicit ``use``.
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
