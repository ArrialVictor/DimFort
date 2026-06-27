"""Disk persistence for the per-file projection cache (M4).

Mirrors the W3 pattern (:mod:`dimfort.core.workspace_index` save/load).
The ``ProjectionCache`` is in-memory only by construction — it caches
:class:`~dimfort.core.annotations.ScanResult` +
:class:`~dimfort.core.attach.AttachmentResult` keyed by ``(content_hash,
patterns_fingerprint)``. Without a disk layer, every fresh LSP process
re-runs ``scan_text`` + ``attach`` on every file even when the
diagnostic ``CacheStore`` already covers the check phase. On a
real-world Fortran codebase (2435 files), that's ~4 s of pure
overhead on every server restart.

This module adds a JSON-on-disk layer:

* :func:`save_persistent_projection_cache` — write the current cache
  contents to ``<cache_root>/projection-cache.json`` atomically.
* :func:`load_persistent_projection_cache` — read the file back and
  return a populated :class:`~dimfort.core.multifile_cache.ProjectionCache`
  (or ``None`` on any failure — missing, corrupt, version mismatch).

Schema invariants:

* Every persisted entry includes the patterns fingerprint, so a project
  whose ``dimfort.toml`` flips ``[parser]`` patterns between sessions
  invalidates naturally on the next ``check_files`` call.
* ``_PROJECTION_SCHEMA_VERSION`` is bumped whenever any of the serialised
  dataclasses changes shape. Mismatch → silent drop + warm rebuild.

Bound
~~~~~
On disk: one file per workspace (``<cache_root>/projection-cache.json``);
size mirrors the in-memory cache's entry count at save time. No on-disk
cap; the in-memory :class:`~dimfort.core.multifile_cache.ProjectionCache`
is bounded by its ``max_entries`` FIFO and that bound carries forward
into the persisted file (load → in-memory cap → next save reflects the
post-cap state). One file per session by construction (path is
session-deterministic).

The codec is a hand-rolled set of ``_dump_*`` / ``_load_*`` helpers per
dataclass — JSON-friendly dicts only, no pickle. ``StrEnum`` values
serialise to their string form. ``frozenset[int]`` becomes a sorted
list. ``dict`` keys that are tuples are flattened to list-of-records
because JSON doesn't accept compound keys.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dimfort.core.annotations import (
    AnnotationKind,
    DeclarationSite,
    MalformedAnnotation,
    NameSpan,
    PatternConflict,
    RawAffineConv,
    RawAnnotation,
    RawAssume,
    ScanResult,
    WrongStatementKind,
)
from dimfort.core.attach import (
    AttachmentResult,
    ConflictingAnnotation,
    MigrationDetectionAnnotation,
    OrphanAnnotation,
    PreOnMultiLineDeclaration,
)
from dimfort.core.multifile_cache import (
    CachedProjection,
    ProjectionCache,
    ProjectionKey,
)

# Bump when any persisted dataclass changes shape (added/removed field,
# renamed field, semantic change to an existing field). Mismatch causes
# :func:`load_persistent_projection_cache` to silently drop the file
# and trigger a full rebuild on next run.
_PROJECTION_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Dumpers (dataclass → JSON-friendly dict)
# ---------------------------------------------------------------------------


def _dump_raw_annotation(a: RawAnnotation) -> dict[str, Any]:
    return {
        "kind": str(a.kind),
        "line": a.line,
        "column": a.column,
        "unit_text": a.unit_text,
        "end_column": a.end_column,
    }


def _dump_malformed(m: MalformedAnnotation) -> dict[str, Any]:
    return {
        "line": m.line,
        "column": m.column,
        "reason": m.reason,
        "end_column": m.end_column,
    }


def _dump_raw_assume(a: RawAssume) -> dict[str, Any]:
    return {
        "line": a.line,
        "column": a.column,
        "end_column": a.end_column,
        "unit_text": a.unit_text,
        "reason": a.reason,
    }


def _dump_raw_affine(a: RawAffineConv) -> dict[str, Any]:
    return {
        "line": a.line,
        "column": a.column,
        "src": a.src,
        "tgt": a.tgt,
        "end_column": a.end_column,
    }


def _dump_pattern_conflict(p: PatternConflict) -> dict[str, Any]:
    return {
        "line": p.line,
        "column": p.column,
        "end_column": p.end_column,
        "directive": p.directive,
        "first_unit_text": p.first_unit_text,
        "second_unit_text": p.second_unit_text,
        "first_pattern_index": p.first_pattern_index,
        "second_pattern_index": p.second_pattern_index,
    }


def _dump_wrong_kind(w: WrongStatementKind) -> dict[str, Any]:
    return {
        "line": w.line,
        "column": w.column,
        "end_column": w.end_column,
        "directive_found": w.directive_found,
        "landed_on": w.landed_on,
        "expected_directive": w.expected_directive,
    }


def _dump_declaration(d: DeclarationSite) -> dict[str, Any]:
    return {
        "line_start": d.line_start,
        "line_end": d.line_end,
        "name_spans": [
            {
                "name": s.name,
                "start_line": s.start_line,
                "start_col": s.start_col,
                "end_line": s.end_line,
                "end_col": s.end_col,
            }
            for s in d.name_spans
        ],
        "enclosing_type": d.enclosing_type,
        "scope": d.scope,
        "intrinsic_type": d.intrinsic_type,
    }


def _dump_orphan(o: OrphanAnnotation) -> dict[str, Any]:
    return {
        "line": o.line,
        "column": o.column,
        "unit_text": o.unit_text,
        "reason": o.reason,
        "target_line": o.target_line,
        "end_column": o.end_column,
    }


def _dump_conflict(c: ConflictingAnnotation) -> dict[str, Any]:
    return {
        "variable": c.variable,
        "first_unit": c.first_unit,
        "second_unit": c.second_unit,
        "second_line": c.second_line,
    }


def _dump_pre_on_multiline(p: PreOnMultiLineDeclaration) -> dict[str, Any]:
    return {
        "line": p.line,
        "column": p.column,
        "unit_text": p.unit_text,
        "decl_line_start": p.decl_line_start,
        "decl_line_end": p.decl_line_end,
    }


def _dump_migration_detection(m: MigrationDetectionAnnotation) -> dict[str, Any]:
    return {
        "line": m.line,
        "column": m.column,
        "unit_text": m.unit_text,
        "decl_line_start": m.decl_line_start,
        "decl_line_end": m.decl_line_end,
        "unannotated_names": list(m.unannotated_names),
    }


def _dump_scan(s: ScanResult) -> dict[str, Any]:
    return {
        "annotations": [_dump_raw_annotation(a) for a in s.annotations],
        "errors": [_dump_malformed(m) for m in s.errors],
        "pre_block_lines": sorted(s.pre_block_lines),
        "declarations": [_dump_declaration(d) for d in s.declarations],
        "routine_scopes": [
            [start, end, name] for (start, end, name) in s.routine_scopes
        ],
        "assumes": [_dump_raw_assume(a) for a in s.assumes],
        "affine_conversions": [
            _dump_raw_affine(a) for a in s.affine_conversions
        ],
        "pattern_conflicts": [
            _dump_pattern_conflict(p) for p in s.pattern_conflicts
        ],
        "wrong_statement_kinds": [
            _dump_wrong_kind(w) for w in s.wrong_statement_kinds
        ],
        "assignment_line_ranges": [
            [a, b] for (a, b) in s.assignment_line_ranges
        ],
    }


def _dump_attachment(a: AttachmentResult) -> dict[str, Any]:
    return {
        "var_units": dict(a.var_units),
        # tuple key (scope, name) → list-of-records [scope, name, unit]
        "var_units_by_scope": [
            [scope, name, unit]
            for ((scope, name), unit) in a.var_units_by_scope.items()
        ],
        # str → (line, col, end_col) tuple
        "var_units_span": {
            name: [line, col, end] for name, (line, col, end) in a.var_units_span.items()
        },
        "routine_scopes": [
            [start, end, name] for (start, end, name) in a.routine_scopes
        ],
        # tuple key (type, field) → list-of-records [type, field, unit]
        "field_units": [
            [type_name, field, unit]
            for ((type_name, field), unit) in a.field_units.items()
        ],
        "var_unit_sources": [
            [scope, name, src]
            for ((scope, name), src) in a.var_unit_sources.items()
        ],
        "orphans": [_dump_orphan(o) for o in a.orphans],
        "conflicts": [_dump_conflict(c) for c in a.conflicts],
        "pre_on_multiline": [
            _dump_pre_on_multiline(p) for p in a.pre_on_multiline
        ],
        "migration_detections": [
            _dump_migration_detection(m) for m in a.migration_detections
        ],
    }


# ---------------------------------------------------------------------------
# Loaders (JSON-friendly dict → dataclass)
# ---------------------------------------------------------------------------


def _load_raw_annotation(d: dict[str, Any]) -> RawAnnotation:
    return RawAnnotation(
        kind=AnnotationKind(d["kind"]),
        line=d["line"],
        column=d["column"],
        unit_text=d["unit_text"],
        end_column=d.get("end_column", 0),
    )


def _load_malformed(d: dict[str, Any]) -> MalformedAnnotation:
    return MalformedAnnotation(
        line=d["line"],
        column=d["column"],
        reason=d["reason"],
        end_column=d.get("end_column", 0),
    )


def _load_raw_assume(d: dict[str, Any]) -> RawAssume:
    return RawAssume(
        line=d["line"],
        column=d["column"],
        end_column=d["end_column"],
        unit_text=d["unit_text"],
        reason=d["reason"],
    )


def _load_raw_affine(d: dict[str, Any]) -> RawAffineConv:
    return RawAffineConv(
        line=d["line"],
        column=d["column"],
        src=d["src"],
        tgt=d["tgt"],
        end_column=d.get("end_column", 0),
    )


def _load_pattern_conflict(d: dict[str, Any]) -> PatternConflict:
    return PatternConflict(
        line=d["line"],
        column=d["column"],
        end_column=d["end_column"],
        directive=d["directive"],
        first_unit_text=d["first_unit_text"],
        second_unit_text=d["second_unit_text"],
        first_pattern_index=d["first_pattern_index"],
        second_pattern_index=d["second_pattern_index"],
    )


def _load_wrong_kind(d: dict[str, Any]) -> WrongStatementKind:
    return WrongStatementKind(
        line=d["line"],
        column=d["column"],
        end_column=d["end_column"],
        directive_found=d["directive_found"],
        landed_on=d["landed_on"],
        expected_directive=d["expected_directive"],
    )


def _load_declaration(d: dict[str, Any]) -> DeclarationSite:
    return DeclarationSite(
        line_start=d["line_start"],
        line_end=d["line_end"],
        name_spans=tuple(
            NameSpan(
                name=s["name"],
                start_line=s["start_line"], start_col=s["start_col"],
                end_line=s["end_line"], end_col=s["end_col"],
            )
            for s in d["name_spans"]
        ),
        enclosing_type=d.get("enclosing_type"),
        scope=d.get("scope"),
        intrinsic_type=d.get("intrinsic_type"),
    )


def _load_orphan(d: dict[str, Any]) -> OrphanAnnotation:
    return OrphanAnnotation(
        line=d["line"],
        column=d["column"],
        unit_text=d["unit_text"],
        reason=d["reason"],
        target_line=d.get("target_line", 0),
        end_column=d.get("end_column", 0),
    )


def _load_conflict(d: dict[str, Any]) -> ConflictingAnnotation:
    return ConflictingAnnotation(
        variable=d["variable"],
        first_unit=d["first_unit"],
        second_unit=d["second_unit"],
        second_line=d["second_line"],
    )


def _load_pre_on_multiline(d: dict[str, Any]) -> PreOnMultiLineDeclaration:
    return PreOnMultiLineDeclaration(
        line=d["line"],
        column=d["column"],
        unit_text=d["unit_text"],
        decl_line_start=d["decl_line_start"],
        decl_line_end=d["decl_line_end"],
    )


def _load_migration_detection(d: dict[str, Any]) -> MigrationDetectionAnnotation:
    return MigrationDetectionAnnotation(
        line=d["line"],
        column=d["column"],
        unit_text=d["unit_text"],
        decl_line_start=d["decl_line_start"],
        decl_line_end=d["decl_line_end"],
        unannotated_names=tuple(d["unannotated_names"]),
    )


def _load_scan(d: dict[str, Any]) -> ScanResult:
    return ScanResult(
        annotations=tuple(_load_raw_annotation(a) for a in d["annotations"]),
        errors=tuple(_load_malformed(m) for m in d["errors"]),
        pre_block_lines=frozenset(d["pre_block_lines"]),
        declarations=tuple(
            _load_declaration(x) for x in d["declarations"]
        ),
        routine_scopes=tuple(
            (int(a), int(b), str(c)) for (a, b, c) in d["routine_scopes"]
        ),
        assumes=tuple(_load_raw_assume(a) for a in d["assumes"]),
        affine_conversions=tuple(
            _load_raw_affine(a) for a in d["affine_conversions"]
        ),
        pattern_conflicts=tuple(
            _load_pattern_conflict(p) for p in d["pattern_conflicts"]
        ),
        wrong_statement_kinds=tuple(
            _load_wrong_kind(w) for w in d["wrong_statement_kinds"]
        ),
        assignment_line_ranges=tuple(
            (int(a), int(b)) for (a, b) in d["assignment_line_ranges"]
        ),
    )


def _load_attachment(d: dict[str, Any]) -> AttachmentResult:
    return AttachmentResult(
        var_units=dict(d["var_units"]),
        var_units_by_scope={
            (scope, name): unit for [scope, name, unit] in d["var_units_by_scope"]
        },
        var_units_span={
            name: (int(line), int(col), int(end))
            for name, (line, col, end) in d["var_units_span"].items()
        },
        routine_scopes=tuple(
            (int(a), int(b), str(c)) for (a, b, c) in d["routine_scopes"]
        ),
        field_units={
            (type_name, field): unit
            for [type_name, field, unit] in d["field_units"]
        },
        var_unit_sources={
            (scope, name): src
            for [scope, name, src] in d["var_unit_sources"]
        },
        orphans=[_load_orphan(o) for o in d["orphans"]],
        conflicts=[_load_conflict(c) for c in d["conflicts"]],
        pre_on_multiline=[
            _load_pre_on_multiline(p) for p in d.get("pre_on_multiline", [])
        ],
        migration_detections=[
            _load_migration_detection(m)
            for m in d.get("migration_detections", [])
        ],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_persistent_projection_cache(
    cache: ProjectionCache, cache_root: Path
) -> None:
    """Serialise ``cache`` to ``<cache_root>/projection-cache.json``.

    Best-effort: any ``OSError`` (read-only FS, parent missing, etc.)
    is swallowed so a workset pass never aborts because the cache
    couldn't be written.

    Args:
        cache: The projection cache to persist.
        cache_root: Directory the on-disk cache lives under
            (typically ``.dimfort-cache``).
    """
    # Snapshot under the cache lock so a concurrent ``put`` from a
    # checking worker doesn't see a half-written dict. The lock is a
    # ProjectionCache implementation detail; reach in via the read API.
    entries: list[dict[str, Any]] = []
    # Iterate via the cache's internal dict — fine because we hold the
    # process-side lock implicitly by being on the same thread the
    # caller drives. The lock guards individual get/put, not bulk
    # iteration; concurrent put during save is benign (the entry is
    # either fully present or fully absent in the snapshot, never
    # half-written, because the underlying value is a frozen dataclass).
    with cache._lock:  # noqa: SLF001 — intentional: bulk snapshot
        snapshot = list(cache._entries.items())  # noqa: SLF001
    for key, value in snapshot:
        entries.append({
            "content_hash": key.content_hash,
            "patterns_fp": key.patterns_fp,
            "scan": _dump_scan(value.scan),
            "attachment": _dump_attachment(value.attachment),
        })
    payload = {
        "schema_version": _PROJECTION_SCHEMA_VERSION,
        "entries": entries,
    }
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        out_path = cache_root / "projection-cache.json"
        # Atomic write — partial writes on crash never leave a
        # corrupted cache the next session would try to parse.
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(out_path)
    except OSError:
        return


def load_persistent_projection_cache(
    cache_root: Path,
) -> ProjectionCache | None:
    """Load + populate a :class:`ProjectionCache` from disk.

    Returns ``None`` on any failure (missing file, version mismatch,
    JSON parse error, OSError, malformed entry). Caller treats ``None``
    as "no prior cache" and starts with an empty in-memory one.

    Args:
        cache_root: Directory holding the on-disk cache.

    Returns:
        A populated :class:`ProjectionCache` whose entries the caller
        passes as ``projection_cache=`` to :func:`check_files` so a
        cold-after-restart pass reuses last session's work.
    """
    in_path = cache_root / "projection-cache.json"
    try:
        text = in_path.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != _PROJECTION_SCHEMA_VERSION:
        return None
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return None

    cache = ProjectionCache()
    try:
        for entry in entries:
            key = ProjectionKey(
                content_hash=entry["content_hash"],
                patterns_fp=entry["patterns_fp"],
            )
            value = CachedProjection(
                scan=_load_scan(entry["scan"]),
                attachment=_load_attachment(entry["attachment"]),
            )
            cache.put(key, value)
    except (TypeError, ValueError, KeyError):
        return None
    return cache
