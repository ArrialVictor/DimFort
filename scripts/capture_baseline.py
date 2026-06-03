"""Capture a pre-implementation snapshot of var_units_by_scope and
diagnostics for the validation workspace.

Output (under .baseline-0.2.2/):
  - workset.json: {meta, files: {relpath: {var_units_by_scope, diagnostics}}}
  - diag_counts.json: {code: count} aggregated across the workset
  - summary.txt: human-readable counts

Run BEFORE any 0.2.2 implementation lands. Re-run after; diff.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


def _serialize_attachment(att) -> dict:
    return {
        "var_units_by_scope": sorted(
            [
                {"scope": s, "name": n, "unit": u}
                for (s, n), u in att.var_units_by_scope.items()
            ],
            key=lambda r: (r["scope"] or "", r["name"]),
        ),
        "field_units": sorted(
            [
                {"type": t, "field": f, "unit": u}
                for (t, f), u in att.field_units.items()
            ],
            key=lambda r: (r["type"], r["field"]),
        ),
        "var_unit_sources": sorted(
            [
                {"scope": s, "name": n, "source": v}
                for (s, n), v in att.var_unit_sources.items()
            ],
            key=lambda r: (r["scope"] or "", r["name"]),
        ),
    }


def _serialize_diags(diags) -> list[dict]:
    rows = [
        {
            "line": d.start.line,
            "column": d.start.column,
            "severity": d.severity.value,
            "code": d.code,
            "message": d.message,
        }
        for d in diags
    ]
    rows.sort(key=lambda r: (r["line"], r["column"], r["code"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "workspace",
        type=Path,
        help="root directory of the validation workspace",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=Path(".baseline-0.2.2"),
        help="output directory (default: .baseline-0.2.2)",
    )
    p.add_argument(
        "--scale",
        action="store_true",
        default=True,
        help="enable scale-mode checking (matches the canonical "
             "`dimfort check ... --scale` invocation)",
    )
    args = p.parse_args(argv)

    workspace: Path = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"capture_baseline: not a directory: {workspace}", file=sys.stderr)
        return 2

    from dimfort.config import load_config
    from dimfort.core import unit_config  # noqa: F401  populate DEFAULT_TABLE
    from dimfort.core._source_io import discover_fortran_files
    from dimfort.core.multifile import check_files

    paths = discover_fortran_files([workspace])
    if not paths:
        print("capture_baseline: no Fortran sources found", file=sys.stderr)
        return 2

    config = load_config(workspace)
    if config.units_file is not None:
        unit_config.install_default(config.units_file)
    if config.diagnostic_severities:
        from dimfort.core.diagnostics import set_severity_overrides
        set_severity_overrides(config.diagnostic_severities)

    print(
        f"capture_baseline: {len(paths)} files; scale={args.scale}",
        file=sys.stderr,
    )
    result = check_files(
        paths,
        cpp_defines=config.cpp_defines,
        include_paths=config.include_paths,
        external_modules=frozenset(config.external_modules),
        units_file=config.units_file,
        diagnostic_severities=config.diagnostic_severities,
        scale_mode=args.scale or config.scale_mode,
    )

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    files_payload: dict[str, dict] = {}
    diag_counter: Counter[str] = Counter()
    sev_counter: Counter[str] = Counter()

    for path in sorted(paths, key=str):
        abs_path = path.resolve()
        rel = (
            str(abs_path.relative_to(workspace))
            if abs_path.is_relative_to(workspace)
            else str(abs_path)
        )
        att = result.attachments.get(abs_path)
        diags = result.diagnostics.get(abs_path, [])
        for d in diags:
            diag_counter[d.code] += 1
            sev_counter[d.severity.value] += 1
        files_payload[rel] = {
            "attachment": _serialize_attachment(att) if att is not None else None,
            "diagnostics": _serialize_diags(diags),
            "load_failure": (
                result.load_failures[abs_path].stderr
                if abs_path in result.load_failures
                else None
            ),
            "compile_failure": result.compile_failures.get(abs_path),
        }

    workset_payload = {
        "meta": {
            "workspace": str(workspace),
            "file_count": len(paths),
            "scale_mode": bool(args.scale or config.scale_mode),
        },
        "files": files_payload,
    }

    (out / "workset.json").write_text(
        json.dumps(workset_payload, indent=2, sort_keys=False) + "\n"
    )
    (out / "diag_counts.json").write_text(
        json.dumps(
            {
                "by_code": dict(sorted(diag_counter.items())),
                "by_severity": dict(sorted(sev_counter.items())),
                "total": sum(diag_counter.values()),
            },
            indent=2,
        )
        + "\n"
    )

    lines = [
        f"workspace: {workspace}",
        f"files: {len(paths)}",
        f"scale_mode: {bool(args.scale or config.scale_mode)}",
        "",
        "diagnostics by code:",
    ]
    for code, n in sorted(diag_counter.items()):
        lines.append(f"  {code:<8} {n:>5}")
    lines.append("")
    lines.append("diagnostics by severity:")
    for sev, n in sorted(sev_counter.items()):
        lines.append(f"  {sev:<10} {n:>5}")
    lines.append("")
    lines.append(f"total diagnostics: {sum(diag_counter.values())}")
    (out / "summary.txt").write_text("\n".join(lines) + "\n")

    print(f"capture_baseline: wrote {out}/", file=sys.stderr)
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
