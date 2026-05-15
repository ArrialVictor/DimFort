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
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from dimfort import cache as _cache
from dimfort.core import lfortran as lf
from dimfort.core import units as _units_mod
from dimfort.core import workspace_index as _wsi
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


# LFortran emits ANSI colour codes on stderr unconditionally. They'd
# leak verbatim into our diagnostic messages (and the editor UI), so
# we strip them before composing the U007 message.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean_stderr(text: str) -> str:
    return _ANSI_RE.sub("", text).strip()


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
    cache_dir: Path | None = None,
) -> WorksetResult:
    """Scan, attach, and check every file in ``sources`` together.

    ``overrides`` lets the caller substitute in-memory text for one or
    more files — used by the LSP server to check unsaved buffers. The
    keys are the same absolute paths as in ``sources``; for any path
    present, the buffer text is what we scan and what lfortran sees.

    ``cache_dir`` enables the AST/ASR cache: any file whose content
    matches its cached entry skips the LFortran subprocess. Files
    present in ``overrides`` always bypass the cache (their content is
    the buffer text, not what's on disk). Pass ``None`` to disable
    caching entirely.
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
        # When a cache_dir is set, the loop is preceded by a restore
        # pass that drops cached .mod files into ``tmp_dir`` so each
        # unchanged source can skip ``lfortran -c`` entirely. Cascade
        # invariant: a source's .mod is only trusted when every one of
        # its workset-internal use-deps was also restored — LFortran's
        # .mod format embeds info about used modules, so a single
        # stale dep silently propagates.
        module_basenames = [
            name for name, p in basename_to_path.items() if lf.has_module(p)
        ]

        # Map module-name → producing basename for cascade resolution.
        module_to_basename: dict[str, str] = {}
        for name in module_basenames:
            for mod_name in lf.modules_provided(basename_to_path[name]):
                module_to_basename[mod_name] = name

        restored: set[str] = set()
        to_compile: list[str] = []
        for basename in module_basenames:
            src = basename_to_path[basename]
            if cache_dir is None or src in overrides:
                to_compile.append(basename)
                continue
            # Force recompile if any workset-internal use-dep wasn't
            # restored from cache (or was itself force-recompiled).
            try:
                uses = _wsi.extract_uses(src.read_text(errors="replace"))
            except OSError:
                uses = ()
            dep_dirty = any(
                module_to_basename.get(u.module) not in (None, *restored)
                for u in uses
                if u.module in module_to_basename
            )
            if dep_dirty:
                to_compile.append(basename)
                continue
            mods = _cache.load_mods_cached(
                src,
                lfortran=lfortran,
                implicit_interface=implicit_interface,
                cache_dir=cache_dir,
            )
            if mods is None:
                to_compile.append(basename)
                continue
            for mod_name, mod_bytes in mods.items():
                (tmp_dir / f"{mod_name}.mod").write_bytes(mod_bytes)
            restored.add(basename)

        compile_errors = lf.compile_modules_retrying(
            to_compile,
            cwd=tmp_dir,
            lfortran=lfortran,
            implicit_interface=implicit_interface,
        )
        for base, msg in compile_errors.items():
            result.compile_failures[basename_to_path[base]] = msg

        # Write back newly-produced .mod files for next run.
        if cache_dir is not None:
            for basename in to_compile:
                if basename in compile_errors:
                    continue
                src = basename_to_path[basename]
                if src in overrides:
                    continue
                produced: dict[str, bytes] = {}
                for mod_name in lf.modules_provided(src):
                    mod_file = tmp_dir / f"{mod_name}.mod"
                    if mod_file.is_file():
                        produced[mod_name] = mod_file.read_bytes()
                _cache.save_mods_cached(
                    src,
                    produced,
                    lfortran=lfortran,
                    implicit_interface=implicit_interface,
                    cache_dir=cache_dir,
                )

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
                # Cache by absolute source path; the temp-dir symlink
                # we pass to LFortran is just so `.mod` files resolve.
                # Overridden buffers bypass the cache (their content
                # differs from disk).
                override_text = overrides.get(src)
                ast, asr = _cache.load_trees_cached(
                    src.name,
                    source_path=src,
                    lfortran=lfortran,
                    cwd=tmp_dir,
                    implicit_interface=implicit_interface,
                    cache_dir=cache_dir,
                    content=(
                        override_text.encode("utf-8")
                        if override_text is not None
                        else None
                    ),
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
                head = _clean_stderr(result.compile_failures[src]).splitlines()
                diags.append(
                    _diag(
                        str(src), 0, "U007",
                        f"Module compile failed: {head[0] if head else '(no message)'}",
                    )
                )
            elif src in result.load_failures:
                head = _clean_stderr(result.load_failures[src].stderr).splitlines()
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
