"""Command-line entry point.

Wires the ``dimfort`` console script: ``check``, ``interactions``, and
``lsp`` subcommands. The ``check`` and ``interactions`` paths share a
config-loading + file-discovery prologue; ``lsp`` hands off to
``dimfort.lsp.server.run_stdio``.

Exit codes (uniform across subcommands):

* ``0`` — no error-severity diagnostics.
* ``1`` — at least one error-severity diagnostic (or an X001 conflict
  for ``interactions``).
* ``2`` — usage error: missing subcommand, missing path, no Fortran
  sources discovered, or invalid ``dimfort.toml``.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from dimfort import __version__

if TYPE_CHECKING:
    from dimfort.core.coverage import WorksetCoverage

_BOLD = "\033[1m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _color_enabled(no_color: bool) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level ``argparse`` parser.

    Wires the three subcommands (``check``, ``interactions``, ``lsp``)
    and their flags. Built lazily by :func:`main` so ``dimfort --version``
    stays fast — none of the heavier checker / LSP modules are imported
    here.

    Returns:
        Configured root parser, with subparsers attached.
    """
    parser = argparse.ArgumentParser(
        prog="dimfort",
        description="Check dimensional homogeneity of Fortran projects.",
    )
    parser.add_argument(
        "--version", action="version", version=f"dimfort {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=False)

    check = sub.add_parser("check", help="Check one or more Fortran files.")
    check.add_argument(
        "paths", nargs="+", help="Fortran source files to check."
    )
    check.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Suppress diagnostic output; only return an exit code.",
    )
    check.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour (also auto-disabled outside a TTY).",
    )
    check.add_argument(
        "--summary",
        action="store_true",
        help=(
            "Print a per-file H/U diagnostic count summary after the "
            "individual diagnostics."
        ),
    )
    check.add_argument(
        "--timings",
        action="store_true",
        help=(
            "Print wall-clock seconds per pipeline phase "
            "(load / aggregate / index / check / total) at the end of the run."
        ),
    )
    check.add_argument(
        "--cache",
        choices=("off", "read-only", "read-write"),
        default="off",
        help=(
            "Content-hash cache mode. 'off' (default): no caching. "
            "'read-only': consult cache but never write. "
            "'read-write': consult and update the cache. The cache "
            "directory defaults to '.dimfort-cache/' under the first "
            "path argument; override with --cache-dir."
        ),
    )
    check.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help=(
            "Override the cache directory location. Defaults to "
            "'.dimfort-cache/' under the first path argument."
        ),
    )
    check.add_argument(
        "--clear-cache",
        action="store_true",
        help=(
            "Remove all entries in the cache directory before checking. "
            "Combine with --cache read-write to repopulate from scratch."
        ),
    )
    check.add_argument(
        "--trace",
        action="store_true",
        help=(
            "Attach a unit-algebra rule-chain trace to each diagnostic "
            "and render it below the message. Useful for explaining "
            "wrapper-arithmetic diagnostics (D1.2 / D1.3 / D1.6)."
        ),
    )
    check.add_argument(
        "--scale",
        action="store_true",
        help=(
            "Opt-in scale checking: flag operands of the same dimension "
            "but different magnitude (e.g. hPa vs Pa, g/kg vs kg/kg) as "
            "S001 (multiplicative), and offset-differing operands (e.g. "
            "K vs degC) as S002 (affine). Dimension-only is the default. "
            "Can also be enabled via [scale] enabled=true in dimfort.toml."
        ),
    )

    interactions = sub.add_parser(
        "interactions",
        help=(
            "List every site that reads/writes a symbol across the workset, "
            "tagged with the unit each site requires or contributes, and flag "
            "sites whose unit constraints conflict (X001)."
        ),
    )
    interactions.add_argument("symbol", help="Variable name to analyse (case-insensitive).")
    interactions.add_argument(
        "paths", nargs="+", help="Fortran source files / directories to search."
    )
    interactions.add_argument(
        "--file",
        default=None,
        help="Restrict to occurrences in this file (name or path suffix).",
    )
    interactions.add_argument(
        "--scope",
        default=None,
        help="Restrict to occurrences in this routine (case-insensitive name).",
    )
    interactions.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour (also auto-disabled outside a TTY).",
    )
    interactions.add_argument(
        "--scale",
        action="store_true",
        help=(
            "Also treat magnitude (factor) disagreements between sites as "
            "conflicts, not just dimension mismatches. Mirrors `check --scale`."
        ),
    )

    show = sub.add_parser(
        "show-defaults",
        help="Print the bundled default unit table to stdout.",
        description=(
            "Print the contents of the bundled default unit-table "
            "TOML file to stdout. Useful as a starting point for "
            "a project-local units file (used by the companions' "
            "`Open Config` command); also handy for reference / "
            "documentation snippets."
        ),
    )
    show.add_argument(
        "kind",
        choices=["units"],
        help=(
            "What to print. Currently only ``units`` is supported "
            "(prints ``dimfort/core/default_units.toml``)."
        ),
    )

    lsp = sub.add_parser("lsp", help="Start the DimFort language server (stdio).")
    # Some LSP clients (vscode-languageclient with TransportKind.stdio) tack
    # this argument on automatically. We only speak stdio, so it's a no-op
    # but we accept it so the server doesn't crash on launch.
    lsp.add_argument(
        "--stdio", action="store_true", help=argparse.SUPPRESS
    )
    lsp.add_argument(
        "--no-tree-cache",
        action="store_true",
        help=(
            "Disable the session-scoped tree-sitter parse cache; every "
            "check_files call re-parses every file. Debugging knob — "
            "use when chasing a suspected stale-tree bug."
        ),
    )
    lsp.add_argument(
        "--no-exports-cache",
        action="store_true",
        help=(
            "Disable the session-scoped module-exports cache; every "
            "check_files call re-runs the index walk. Debugging knob — "
            "use when chasing a suspected stale-exports bug."
        ),
    )

    cov = sub.add_parser(
        "coverage",
        help=(
            "Report per-file and workset coverage tier counts (green / "
            "yellow / red / blue / out-of-scope). Reuses the check "
            "pipeline; output is a per-file table plus a workset total."
        ),
    )
    cov.add_argument(
        "paths", nargs="+", help="Fortran source files / directories to scan."
    )
    cov.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour (also auto-disabled outside a TTY).",
    )
    cov.add_argument(
        "--summary",
        action="store_true",
        help="Print only the workset total, omit per-file rows.",
    )
    cov.add_argument(
        "--by-module",
        action="store_true",
        help=(
            "Group by Fortran module instead of per-file. The workset "
            "total stays the same; only the row breakdown changes."
        ),
    )
    cov.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the human-readable table.",
    )

    return parser


# ---------------------------------------------------------------------------
# `check` subcommand
# ---------------------------------------------------------------------------


def _format_diag(
    file: str,
    line: int,
    severity: str,
    code: str,
    message: str,
    *,
    color: bool,
) -> str:
    if color:
        sev_color = _RED if severity == "error" else _YELLOW
        return (
            f"{_BOLD}{file}{_RESET}:{line}: "
            f"{sev_color}{severity}{_RESET}: "
            f"{sev_color}{code}{_RESET} {message}"
        )
    return f"{file}:{line}: {severity}: {code} {message}"


def _run_check(args: argparse.Namespace) -> int:
    # Lazy imports keep ``dimfort --version`` fast.
    from dimfort.config import load_config
    from dimfort.core import unit_config  # populate DEFAULT_TABLE
    from dimfort.core._source_io import FORTRAN_EXTS, discover_fortran_files
    from dimfort.core.multifile import check_files
    from dimfort.core.unit_patterns import (
        compile_nonstructured_patterns,
        compile_nonunit_patterns,
        compile_structured_patterns,
        compile_unit_patterns,
    )

    roots: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if not p.exists():
            print(f"dimfort: path not found: {p}", file=sys.stderr)
            return 2
        roots.append(p)

    paths = discover_fortran_files(roots)
    if not paths:
        print(
            "dimfort: no Fortran sources found "
            f"(looked for {sorted(FORTRAN_EXTS)})",
            file=sys.stderr,
        )
        return 2

    color = _color_enabled(args.no_color)
    error_count = 0

    def emit(file: str, line: int, severity: str, code: str, message: str) -> None:
        nonlocal error_count
        if severity == "error":
            error_count += 1
        if args.quiet:
            return
        print(_format_diag(file, line, severity, code, message, color=color))

    # Pick up CPP defines + include paths from dimfort.toml, anchored
    # on the first path passed on the command line (file or directory).
    config = load_config(roots[0])
    if config.load_error is not None:
        # Documented in docs/reference/cli.md: invalid config → exit 2.
        sys.stderr.write(
            f"error: invalid config at {config.config_path}: "
            f"{config.load_error}\n"
        )
        return 2
    if config.units_file is not None:
        unit_config.install_default(config.units_file)
    if config.diagnostic_severities:
        from dimfort.core.diagnostics import set_severity_overrides
        set_severity_overrides(config.diagnostic_severities)
    # Phase D: activate unit-algebra tracing for the duration of the
    # check if --trace was passed. Per-statement traces inside the
    # checker pick up activation via current_trace() != None.
    from contextlib import nullcontext

    from dimfort.core.trace import format_trace, with_trace

    # Cache setup. ``--cache off`` (default) keeps the previous
    # behaviour exactly. Other modes construct a CacheStore rooted at
    # ``--cache-dir`` (or .dimfort-cache/ under the first input path).
    cache_obj = None
    cache_mode = getattr(args, "cache", "off")
    clear_cache = getattr(args, "clear_cache", False)
    if cache_mode != "off" or clear_cache:
        from dimfort.core.cache_store import CacheStore, default_cache_dir

        cache_root = (
            args.cache_dir if getattr(args, "cache_dir", None) is not None
            else default_cache_dir(roots[0])
        )
        cache_obj = CacheStore(root=cache_root)
        if clear_cache:
            cache_obj.clear()
            # Surface the action so a ``--clear-cache`` without
            # ``--cache=…`` (which silently wipes and then never
            # rewrites) doesn't look like a no-op to the user.
            sys.stderr.write(f"dimfort: cleared cache at {cache_root}\n")

    trace_ctx = with_trace() if getattr(args, "trace", False) else nullcontext()
    with trace_ctx:
        result = check_files(
            paths,
            cpp_defines=config.cpp_defines,
            include_paths=config.include_paths,
            external_modules=frozenset(config.external_modules),
            cache=cache_obj,
            cache_mode=cache_mode,
            units_file=config.units_file,
            diagnostic_severities=config.diagnostic_severities,
            scale_mode=getattr(args, "scale", False) or config.scale_mode,
            unit_patterns=compile_unit_patterns(config.unit_comments.unit),
            assume_patterns=compile_structured_patterns(
                config.unit_comments.unit_assume
            ),
            affine_patterns=compile_structured_patterns(
                config.unit_comments.unit_affine
            ),
            nonunit_patterns=compile_nonunit_patterns(
                config.unit_comments.nonunit
            ),
            nonunit_assume_patterns=compile_nonstructured_patterns(
                config.unit_comments.nonunit_assume
            ),
            nonunit_affine_patterns=compile_nonstructured_patterns(
                config.unit_comments.nonunit_affine
            ),
            unit_lexer=config.unit_lexer,
        )

    if cache_obj is not None and cache_mode == "read-write":
        # Best-effort lazy prune at the end of the run.
        with contextlib.suppress(OSError):
            cache_obj.prune()

    per_file_counts: list[tuple[Path, int, int]] = []
    for p in paths:
        diags = result.diagnostics.get(p.resolve(), [])
        h = sum(1 for d in diags if d.code.startswith("H"))
        u = sum(1 for d in diags if d.code.startswith("U"))
        per_file_counts.append((p, h, u))
        for d in diags:
            severity = d.severity.value
            emit(d.file, d.start.line, severity, d.code, d.message)
            if getattr(args, "trace", False) and d.trace and not args.quiet:
                for line in format_trace(d.trace).splitlines():
                    print(f"  {line}")

    if args.summary and not args.quiet:
        total_h = sum(h for _, h, _ in per_file_counts)
        total_u = sum(u for _, _, u in per_file_counts)
        files_with_issues = [
            (p, h, u) for p, h, u in per_file_counts if h or u
        ]
        header = f"{_BOLD}Summary{_RESET}" if color else "Summary"
        print()
        print(header)
        if files_with_issues:
            name_width = max(len(str(p)) for p, _, _ in files_with_issues)
            for p, h, u in files_with_issues:
                line = f"  {str(p).ljust(name_width)}  {h:>3} H  {u:>3} U"
                if color:
                    if h:
                        line = line.replace(
                            f"{h:>3} H", f"{_RED}{h:>3} H{_RESET}"
                        )
                    if u:
                        line = line.replace(
                            f"{u:>3} U", f"{_YELLOW}{u:>3} U{_RESET}"
                        )
                print(line)
        print(
            f"  {len(paths)} file(s), "
            f"{total_h} H-diagnostic(s), {total_u} U-diagnostic(s)"
        )

    if args.timings:
        header = f"{_BOLD}Phase timings{_RESET}" if color else "Phase timings"
        print()
        print(header)
        # Print in the canonical pipeline order, not dict order.
        for phase in ("load", "aggregate", "index", "check", "total"):
            seconds = result.phase_timings.get(phase)
            if seconds is None:
                continue
            print(f"  {phase:<10}  {seconds:7.2f} s")
        if cache_obj is not None:
            cache_header = (
                f"{_BOLD}Cache{_RESET}" if color else "Cache"
            )
            print()
            print(cache_header)
            print(f"  hits      {result.cache_hits:>6}")
            print(f"  misses    {result.cache_misses:>6}")
            print(f"  dirty     {result.cache_dirty:>6}")
            print(f"  writes    {result.cache_writes:>6}")

    return 1 if error_count else 0


# ---------------------------------------------------------------------------
# `interactions` subcommand
# ---------------------------------------------------------------------------


def _run_interactions(args: argparse.Namespace) -> int:
    from dimfort.config import load_config
    from dimfort.core import unit_config  # populate DEFAULT_TABLE
    from dimfort.core._source_io import FORTRAN_EXTS, discover_fortran_files
    from dimfort.core.interactions import collect_interactions
    from dimfort.core.multifile import check_files
    from dimfort.core.unit_patterns import (
        compile_nonstructured_patterns,
        compile_nonunit_patterns,
        compile_structured_patterns,
        compile_unit_patterns,
    )

    roots: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if not p.exists():
            print(f"dimfort: path not found: {p}", file=sys.stderr)
            return 2
        roots.append(p)

    paths = discover_fortran_files(roots)
    if not paths:
        print(
            "dimfort: no Fortran sources found "
            f"(looked for {sorted(FORTRAN_EXTS)})",
            file=sys.stderr,
        )
        return 2

    config = load_config(roots[0])
    if config.load_error is not None:
        sys.stderr.write(
            f"error: invalid config at {config.config_path}: "
            f"{config.load_error}\n"
        )
        return 2
    if config.units_file is not None:
        unit_config.install_default(config.units_file)

    workset = check_files(
        paths,
        cpp_defines=config.cpp_defines,
        include_paths=config.include_paths,
        external_modules=frozenset(config.external_modules),
        units_file=config.units_file,
        scale_mode=args.scale or config.scale_mode,
        unit_patterns=compile_unit_patterns(config.unit_comments.unit),
        assume_patterns=compile_structured_patterns(
            config.unit_comments.unit_assume
        ),
        affine_patterns=compile_structured_patterns(
            config.unit_comments.unit_affine
        ),
        nonunit_patterns=compile_nonunit_patterns(
            config.unit_comments.nonunit
        ),
        nonunit_assume_patterns=compile_nonstructured_patterns(
            config.unit_comments.nonunit_assume
        ),
        nonunit_affine_patterns=compile_nonstructured_patterns(
            config.unit_comments.nonunit_affine
        ),
        unit_lexer=config.unit_lexer,
    )

    report = collect_interactions(
        workset,
        args.symbol,
        file=args.file,
        scope=args.scope,
        scale=args.scale or config.scale_mode,
    )

    color = _color_enabled(args.no_color)

    def _hdr(text: str) -> str:
        return f"{_BOLD}{text}{_RESET}" if color else text

    if not report.points:
        print(
            f"dimfort: no read/write of {args.symbol!r} found"
            + (f" in {args.file}" if args.file else "")
            + (f" (scope {args.scope})" if args.scope else "")
        )
        return 0

    print(_hdr(args.symbol))
    order = (
        ("declares", "Declaration"),
        ("contributes", "Write"),
        ("requires", "Read"),
        ("uses", "Undetermined"),
    )
    for kind, label in order:
        sites = [p for p in report.points if p.kind == kind]
        if not sites:
            continue
        print(f"  {label}:")
        for s in sites:
            scope = f" [{s.scope}]" if s.scope else ""
            # The Undetermined group has no derived unit by definition —
            # don't print a redundant "?" column.
            if kind == "uses":
                print(f"    {s.file}:{s.line}{scope}  {s.snippet}")
            else:
                print(f"    {s.file}:{s.line}{scope}  {s.unit_str.ljust(12)}  {s.snippet}")

    if report.conflicts:
        print()
        for c in report.conflicts:
            d = c.diagnostic
            line = _format_diag(d.file, d.start.line, "error", d.code, d.message, color=color)
            print(f"  ⚠ {line}")
        return 1

    return 0


# ---------------------------------------------------------------------------
# `coverage` subcommand
# ---------------------------------------------------------------------------


def _run_coverage(args: argparse.Namespace) -> int:
    """Per-file and workset coverage report.

    Runs the check pipeline over ``args.paths``, projects each file's
    diagnostics + annotation surface into the four coverage tiers, and
    prints either a human-readable table or JSON.

    Args:
        args: Parsed argparse namespace for the ``coverage`` subcommand.

    Returns:
        ``0`` on success; ``2`` for usage errors (missing path / no
        Fortran sources / invalid config), matching the contract used
        by the other subcommands.
    """
    from dimfort.config import load_config
    from dimfort.core import unit_config
    from dimfort.core._source_io import FORTRAN_EXTS, discover_fortran_files
    from dimfort.core.coverage import (
        FileCoverage,
        aggregate_file,
        aggregate_workset,
        project_file,
    )
    from dimfort.core.multifile import check_files
    from dimfort.core.unit_patterns import (
        compile_nonstructured_patterns,
        compile_nonunit_patterns,
        compile_structured_patterns,
        compile_unit_patterns,
    )

    roots: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if not p.exists():
            print(f"dimfort: path not found: {p}", file=sys.stderr)
            return 2
        roots.append(p)

    paths = discover_fortran_files(roots)
    if not paths:
        print(
            "dimfort: no Fortran sources found "
            f"(looked for {sorted(FORTRAN_EXTS)})",
            file=sys.stderr,
        )
        return 2

    config = load_config(roots[0])
    if config.load_error is not None:
        sys.stderr.write(
            f"error: invalid config at {config.config_path}: "
            f"{config.load_error}\n"
        )
        return 2
    if config.units_file is not None:
        unit_config.install_default(config.units_file)

    result = check_files(
        paths,
        cpp_defines=config.cpp_defines,
        include_paths=config.include_paths,
        external_modules=frozenset(config.external_modules),
        units_file=config.units_file,
        diagnostic_severities=config.diagnostic_severities,
        scale_mode=config.scale_mode,
        unit_patterns=compile_unit_patterns(config.unit_comments.unit),
        assume_patterns=compile_structured_patterns(
            config.unit_comments.unit_assume
        ),
        affine_patterns=compile_structured_patterns(
            config.unit_comments.unit_affine
        ),
        nonunit_patterns=compile_nonunit_patterns(
            config.unit_comments.nonunit
        ),
        nonunit_assume_patterns=compile_nonstructured_patterns(
            config.unit_comments.nonunit_assume
        ),
        nonunit_affine_patterns=compile_nonstructured_patterns(
            config.unit_comments.nonunit_affine
        ),
        unit_lexer=config.unit_lexer,
    )

    rows: list[FileCoverage] = []
    for p in paths:
        resolved = p.resolve()
        statuses = project_file(resolved, result)
        tree_entry = result.trees.get(resolved)
        total_lines = (
            tree_entry[1].count(b"\n") + 1 if tree_entry is not None else 0
        )
        rows.append(aggregate_file(resolved, statuses, total_lines=total_lines))

    workset = aggregate_workset(rows)

    if args.json:
        _emit_coverage_json(workset)
        return 0

    color = _color_enabled(args.no_color)
    _emit_coverage_table(workset, color=color, summary_only=args.summary)
    return 0


def _emit_coverage_table(
    workset: WorksetCoverage,
    *,
    color: bool,
    summary_only: bool,
) -> None:
    """Print the human-readable per-file + workset-total coverage table.

    Args:
        workset: Aggregated coverage record produced by
            :func:`aggregate_workset`.
        color: Whether to apply ANSI colouring.
        summary_only: If true, skip the per-file rows and print only
            the workset-total footer.
    """
    header_text = (
        "File                                          "
        "OK  Warn  Fire  Unpars   Out  Coverage"
    )
    if color:
        print(f"{_BOLD}{header_text}{_RESET}")
    else:
        print(header_text)

    if not summary_only:
        # Truncate path columns from the left to keep the table readable
        # on terminals narrower than the path. We render up to 44 chars
        # of the path, with leading "…" if truncated.
        max_path = 44
        for f in workset.files:
            shown = str(f.path)
            if len(shown) > max_path:
                shown = "…" + shown[-(max_path - 1):]
            print(
                f"{shown.ljust(max_path)}  "
                f"{f.ok:>4}  {f.warn:>4}  {f.fire:>4}  {f.unparsed:>6}  "
                f"{f.out:>4}  {f.coverage_pct:>5.1f}%"
            )

    total_label = "Workset total"
    line = (
        f"{total_label.ljust(44)}  "
        f"{workset.ok:>4}  {workset.warn:>4}  {workset.fire:>4}  "
        f"{workset.unparsed:>6}  {workset.out:>4}  "
        f"{workset.coverage_pct:>5.1f}%"
    )
    if color:
        print(f"{_BOLD}{line}{_RESET}")
    else:
        print(line)


def _emit_coverage_json(workset: WorksetCoverage) -> None:
    """Print the JSON form of the coverage report on stdout.

    Args:
        workset: Aggregated coverage record produced by
            :func:`aggregate_workset`.
    """
    import json as _json

    payload = {
        "files": [
            {
                "path": str(f.path),
                "ok": f.ok,
                "warn": f.warn,
                "fire": f.fire,
                "unparsed": f.unparsed,
                "out": f.out,
                "coverage_pct": f.coverage_pct,
            }
            for f in workset.files
        ],
        "total": {
            "ok": workset.ok,
            "warn": workset.warn,
            "fire": workset.fire,
            "unparsed": workset.unparsed,
            "out": workset.out,
            "coverage_pct": workset.coverage_pct,
        },
    }
    print(_json.dumps(payload, indent=2))


def _run_show_defaults(args: argparse.Namespace) -> int:
    """Print the contents of a bundled default table file to stdout.

    Used by the companions' ``Open Config…`` command flow when the
    user wants to seed a fresh project units file with the bundled
    defaults (all entries commented out, ready to uncomment +
    customise). Shell-out from the companion keeps the defaults
    fresh — no need to vendor a copy in each companion repo.

    Args:
        args: Parsed argparse namespace; ``args.kind`` selects which
            default file to print (currently only ``"units"``).

    Returns:
        ``0`` on success, ``1`` if the bundled file can't be located
        (would indicate a broken install).
    """
    from importlib import resources

    if args.kind == "units":
        try:
            content = (
                resources.files("dimfort.core")
                .joinpath("default_units.toml")
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
            print(
                f"dimfort: cannot read bundled default_units.toml: {exc}",
                file=sys.stderr,
            )
            return 1
        sys.stdout.write(content)
        return 0
    # argparse's ``choices=`` should prevent this path, but be defensive.
    print(f"dimfort: unknown defaults kind {args.kind!r}", file=sys.stderr)
    return 2


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``dimfort`` console script.

    Parses ``argv`` against :func:`build_parser` and dispatches to the
    matching subcommand handler. With no subcommand supplied, prints
    help to stderr and returns exit code 2 per the documented contract.

    Args:
        argv: Argument list to parse. ``None`` falls back to
            ``sys.argv[1:]`` via argparse's default.

    Returns:
        Process exit code (see module docstring for the 0 / 1 / 2 scheme).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _run_check(args)
    if args.command == "interactions":
        return _run_interactions(args)
    if args.command == "coverage":
        return _run_coverage(args)
    if args.command == "show-defaults":
        return _run_show_defaults(args)
    if args.command == "lsp":
        from dimfort.lsp.server import run_stdio
        from dimfort.lsp.state import state

        if args.no_tree_cache:
            state.tree_cache = None
        if args.no_exports_cache:
            state.exports_cache = None
        run_stdio()
        return 0

    # No subcommand supplied — print help to stderr and exit 2
    # (matches the documented contract: usage error → exit 2).
    parser.print_help(sys.stderr)
    return 2
