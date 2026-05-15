"""AST-only multi-file orchestration — Phase 2.

Parallel to :mod:`dimfort.core.multifile`, but never invokes ``lfortran
-c`` and never asks for ASR. Every cross-file symbol that the ASR
pipeline gets "for free" via inlined ``use``-imports must be
synthesised here from each module's AST: see
:func:`ast_checker.collect_module_exports` and
:func:`ast_checker.apply_use_clauses`.

Phase 2 v1 deliberately keeps the public-by-default policy from F90's
implicit ``public`` rule — no ``private`` honouring yet. Refinements
(``[checker] backend = "ast"`` config wiring, in-place buffer overrides
through the LSP, ``.mods`` cache removal) land in later phases.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dimfort.core import ast_checker
from dimfort.core import lfortran as lf
from dimfort.core import units as _units_mod
from dimfort.core import workspace_index as _wsi
from dimfort.core.annotations import scan_file, scan_text
from dimfort.core.attach import attach, AttachmentResult
from dimfort.core.ast_checker import (
    ModuleExports,
    apply_use_clauses,
    collect_function_signatures,
    collect_module_exports,
)
from dimfort.core.checker import FuncSig
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.multifile import (
    WorksetResult,
    FileLoadFailure,
    _attachment_diags,
    _clean_stderr,
)
from dimfort.core.units import Unit, UnitError, UnitTable


@dataclass(frozen=True)
class _Loaded:
    """Per-file intermediate state for a workset pass."""

    path: Path
    text: str
    scan: object
    attachment: AttachmentResult
    ast: dict | None
    load_error: str | None


def _load_one(
    path: Path,
    *,
    lfortran: str | os.PathLike[str] | None,
    overrides: dict[Path, str],
    include_paths: tuple[Path, ...] = (),
) -> _Loaded:
    """Scan + attach + dump AST for one file.

    ``overrides`` lets the LSP feed unsaved buffer contents. On any
    LFortran error the ``ast`` field is ``None`` and ``load_error``
    carries the stderr — the caller surfaces U007.
    """
    from dimfort.core._source_io import read_text
    text = overrides.get(path) if path in overrides else read_text(path)
    scan = scan_text(text)
    attachment = attach(scan)
    try:
        ast = lf.dump_tree(
            path, "ast", lfortran=lfortran, include_paths=include_paths,
        )
    except lf.LFortranError as exc:
        return _Loaded(path, text, scan, attachment, None, exc.stderr)
    return _Loaded(path, text, scan, attachment, ast, None)


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
    progress_cb: Callable[[str, int, int, Path], None] | None = None,
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

    # Phase A: load every file. ``progress_cb`` (if supplied) fires
    # AFTER each file is loaded so the LSP / CLI can show per-file
    # detail during the slow part of the pipeline.
    loaded: list[_Loaded] = []
    total = len(abs_sources)
    for i, src in enumerate(abs_sources, start=1):
        try:
            loaded.append(
                _load_one(
                    src,
                    lfortran=lfortran,
                    overrides=overrides_map,
                    include_paths=include_paths,
                )
            )
        except OSError as exc:
            result.load_failures[src] = FileLoadFailure(stderr=str(exc))
            loaded.append(_Loaded(src, "", scan_text(""), attach(scan_text("")), None, str(exc)))
        if progress_cb is not None:
            progress_cb("load", i, total, src)

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

    # Phase C: aggregate module exports and global signatures across
    # every successfully-loaded file.
    module_exports: dict[str, ModuleExports] = {}
    global_signatures: dict[str, FuncSig] = {}
    for i, entry in enumerate(loaded, start=1):
        if entry.ast is not None:
            for mname, exp in collect_module_exports(entry.ast, merged_var_units).items():
                module_exports.setdefault(mname, exp)
            for fname, sig in collect_function_signatures(entry.ast, merged_var_units).items():
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

        if entry.ast is None:
            head = _clean_stderr(entry.load_error or "").splitlines()
            diags.append(
                _u007(
                    entry.path,
                    f"LFortran could not load this file: "
                    f"{head[0] if head else '(no message)'}",
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

        # Convert the merged Unit table back to text-keyed form (which
        # ast_checker.check re-parses). Cheap because parses are cached
        # implicitly by Python's small-object reuse — and at this size
        # the cost is invisible. Keeps the check() public interface
        # uniform with the single-file path.
        var_units_text = {
            n: _units_mod.format_unit(u) for n, u in per_file_var_units.items()
        }
        # field_units flows straight through: it's keyed by
        # ``(type, field)`` and there's no cross-file rename concept
        # for derived-type fields the way there is for ``use``-imported
        # variable names. Merge across all files so a consumer that
        # uses a type declared elsewhere still sees its fields.
        diags.extend(
            ast_checker.check(
                entry.ast,
                var_units_text,
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
