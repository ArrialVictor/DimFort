"""Command-line entry point.

Exit codes:
    0 — no error-severity diagnostics
    1 — at least one error-severity diagnostic
    2 — usage error, missing file, invalid config
"""
from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dimfort import __version__

_BOLD = "\033[1m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"


def _color_enabled(no_color: bool) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def build_parser() -> argparse.ArgumentParser:
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
            "Opt-in multiplicative-scale checking (Phase 1): flag operands "
            "of the same dimension but different magnitude (e.g. hPa vs Pa, "
            "g/kg vs kg/kg) as S001. Dimension-only is the default. Can also "
            "be enabled via [scale] enabled=true in .dimfort.toml."
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

    lsp = sub.add_parser("lsp", help="Start the DimFort language server (stdio).")
    # Some LSP clients (vscode-languageclient with TransportKind.stdio) tack
    # this argument on automatically. We only speak stdio, so it's a no-op
    # but we accept it so the server doesn't crash on launch.
    lsp.add_argument(
        "--stdio", action="store_true", help=argparse.SUPPRESS
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

    # Pick up CPP defines + include paths from .dimfort.toml, anchored
    # on the first path passed on the command line (file or directory).
    config = load_config(roots[0])
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
    if config.units_file is not None:
        unit_config.install_default(config.units_file)

    workset = check_files(
        paths,
        cpp_defines=config.cpp_defines,
        include_paths=config.include_paths,
        external_modules=frozenset(config.external_modules),
        units_file=config.units_file,
        scale_mode=args.scale or config.scale_mode,
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
        ("uses", "Undetermined read"),
    )
    for kind, label in order:
        sites = [p for p in report.points if p.kind == kind]
        if not sites:
            continue
        print(f"  {label}:")
        for s in sites:
            scope = f" [{s.scope}]" if s.scope else ""
            # The Undetermined group has no derived unit — its label already
            # says so, so don't print a redundant "?" column.
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
# Top-level dispatch
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _run_check(args)
    if args.command == "interactions":
        return _run_interactions(args)
    if args.command == "lsp":
        from dimfort.lsp.server import run_stdio

        run_stdio()
        return 0

    parser.print_help()
    return 0
