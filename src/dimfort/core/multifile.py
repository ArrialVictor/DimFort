"""Multi-file orchestration.

Runs the full DimFort pipeline (scan → attach → check) on a workset
of Fortran source files. Three pieces of multi-file glue beyond the
single-file path:

1. Module files are compiled first with ``lfortran -c`` in a private
   temp directory, in dependency order via a retry-loop, so that
   ``use`` statements in later files resolve.
2. Annotation tables (``var_units`` and ``field_units``) are merged
   across all files before checks run.
3. Function and subroutine signatures are collected from every file's
   ASR and merged into a single global table, so a call site in one
   file can be checked against a definition in another.

The orchestrator returns ``{Path: list[Diagnostic]}``. The CLI in
``dimfort.cli`` consumes this and formats the output.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dimfort.core import lfortran as lf
from dimfort.core import units as _units_mod
from dimfort.core.annotations import scan_file, scan_text
from dimfort.core.attach import (
    AttachmentResult,
    attach,
)
from dimfort.core.checker import (
    CODES,
    FuncSig,
    check,
    collect_function_signatures,
)
from dimfort.core.diagnostics import Diagnostic, Position, Severity
from dimfort.core.units import Unit, UnitError, UnitTable


@dataclass(frozen=True)
class FileLoadFailure:
    """The lfortran AST/ASR dump failed for this file."""

    stderr: str


@dataclass
class WorksetResult:
    diagnostics: dict[Path, list[Diagnostic]] = field(default_factory=dict)
    attachments: dict[Path, AttachmentResult] = field(default_factory=dict)
    load_failures: dict[Path, FileLoadFailure] = field(default_factory=dict)
    compile_failures: dict[Path, str] = field(default_factory=dict)
    # Per-file ``(ast, asr)`` pair, populated only for files that loaded
    # successfully. Used by the LSP server for hover-time symbol lookup.
    trees: dict[Path, tuple[dict, dict]] = field(default_factory=dict)
    # Per-file parsed unit table (for hover formatting); same key set as
    # ``trees``.
    merged_var_units: dict[str, Unit] = field(default_factory=dict)
    merged_field_units: dict[tuple[str, str], Unit] = field(default_factory=dict)
    # Function / subroutine signatures resolved across the whole workset.
    signatures: dict[str, FuncSig] = field(default_factory=dict)


def _diag(file: str, line: int, code: str, message: str) -> Diagnostic:
    spec = CODES.get(code)
    severity = spec.severity if spec else Severity.ERROR
    pos = Position(line, 0)
    return Diagnostic(
        file=file, start=pos, end=pos, severity=severity, code=code, message=message
    )


def _attachment_diags(file: str, att: AttachmentResult) -> list[Diagnostic]:
    out: list[Diagnostic] = []
    for orph in att.orphans:
        # Make sure the reason reads as a proper sentence in the editor.
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
) -> tuple[dict[str, Unit], list[str]]:
    out: dict[str, Unit] = {}
    bad: list[str] = []
    for name, t in text.items():
        try:
            out[name] = _units_mod.parse(t, table)
        except UnitError:
            bad.append(name)
    return out, bad


def check_files(
    sources: list[Path],
    *,
    lfortran: str | os.PathLike[str] | None = None,
    table: UnitTable | None = None,
    implicit_interface: bool = False,
    overrides: dict[Path, str] | None = None,
) -> WorksetResult:
    """Scan, attach, and check every file in ``sources`` together.

    ``overrides`` lets the caller substitute in-memory text for one or
    more files — used by the LSP server to check unsaved buffers. The
    keys are the same absolute paths as in ``sources``; for any path
    present, the buffer text is what we scan and what lfortran sees.
    """
    abs_sources = [Path(p).resolve() for p in sources]
    overrides = {Path(p).resolve(): t for p, t in (overrides or {}).items()}
    active_table = table if table is not None else _units_mod.DEFAULT_TABLE
    if active_table is None:
        raise RuntimeError(
            "no unit table available — import dimfort.core.unit_config"
        )

    result = WorksetResult()

    with tempfile.TemporaryDirectory(prefix="dimfort-") as tmp:
        tmp_dir = Path(tmp)
        # For each source: either symlink to disk, or (if overridden)
        # write the in-memory buffer text as a real file in the temp dir.
        # Either way the rest of the pipeline runs against the temp-dir
        # entry, so `.mod` files emitted by `lfortran -c` land next to
        # them and `use` statements resolve.
        basename_to_path: dict[str, Path] = {}
        for src in abs_sources:
            link = tmp_dir / src.name
            if src in basename_to_path.values():
                continue  # duplicate basename — keep the first
            if src in overrides:
                link.write_text(overrides[src])
            else:
                try:
                    link.symlink_to(src)
                except FileExistsError:
                    continue
            basename_to_path[src.name] = src

        # Phase 1: compile every module file (retry-loop until stable).
        module_basenames = [
            name for name, p in basename_to_path.items() if lf.has_module(p)
        ]
        compile_errors = lf.compile_modules_retrying(
            module_basenames,
            cwd=tmp_dir,
            lfortran=lfortran,
            implicit_interface=implicit_interface,
        )
        for base, msg in compile_errors.items():
            result.compile_failures[basename_to_path[base]] = msg

        # Phase 2: scan + attach every file. Merge annotation tables.
        merged_var_units_text: dict[str, str] = {}
        merged_field_units_text: dict[tuple[str, str], str] = {}
        per_file_attached: dict[Path, AttachmentResult] = {}

        scans = {}
        for src in abs_sources:
            scans[src] = scan_text(overrides[src]) if src in overrides else scan_file(src)
            att = attach(scans[src])
            per_file_attached[src] = att
            for n, u in att.var_units.items():
                merged_var_units_text.setdefault(n, u)
            for k, u in att.field_units.items():
                merged_field_units_text.setdefault(k, u)
        result.attachments = per_file_attached

        # Parse the merged textual tables once.
        merged_var_units, _ = _parse_var_units(merged_var_units_text, active_table)
        result.merged_var_units = merged_var_units
        for (tn, fn), t in merged_field_units_text.items():
            with contextlib.suppress(UnitError):
                result.merged_field_units[(tn, fn)] = _units_mod.parse(t, active_table)

        # Phase 3: dump AST + ASR for every file (from the temp cwd so
        # `.mod` files are visible). Aggregate function signatures.
        trees = result.trees
        global_signatures = result.signatures
        for src in abs_sources:
            if src in result.compile_failures:
                continue
            try:
                ast, asr = lf.load_trees(
                    src.name,
                    lfortran=lfortran,
                    cwd=tmp_dir,
                    implicit_interface=implicit_interface,
                )
            except lf.LFortranError as exc:
                result.load_failures[src] = FileLoadFailure(stderr=exc.stderr)
                continue
            trees[src] = (ast, asr)
            for name, sig in collect_function_signatures(asr, merged_var_units).items():
                global_signatures.setdefault(name, sig)

        # Phase 4: run the per-file check with merged tables.
        for src in abs_sources:
            diags: list[Diagnostic] = []
            # Attachment-time issues (orphans, conflicts, U010).
            diags.extend(_attachment_diags(str(src), per_file_attached[src]))
            # Stage-1 malformed annotations.
            for err in scans[src].errors:
                diags.append(
                    Diagnostic(
                        file=str(src),
                        start=Position(err.line, err.column),
                        end=Position(err.line, err.column),
                        severity=Severity.ERROR,
                        code="U001",
                        message=err.reason,
                    )
                )
            # Compile and load failures surface as U007.
            if src in result.compile_failures:
                head = result.compile_failures[src].strip().splitlines()
                diags.append(
                    _diag(
                        str(src), 0, "U007",
                        f"Module compile failed: {head[0] if head else '(no message)'}",
                    )
                )
            elif src in result.load_failures:
                head = result.load_failures[src].stderr.strip().splitlines()
                diags.append(
                    _diag(
                        str(src), 0, "U007",
                        f"LFortran could not load this file: {head[0] if head else '(no message)'}",
                    )
                )
            # Semantic checks.
            if src in trees:
                ast, asr = trees[src]
                diags.extend(
                    check(
                        asr,
                        merged_var_units_text,
                        ast=ast,
                        field_units_text=merged_field_units_text,
                        functions=global_signatures,
                        table=active_table,
                        file=str(src),
                    )
                )
            result.diagnostics[src] = diags

    return result
