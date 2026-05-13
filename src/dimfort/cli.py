"""Command-line entry point.

Exit codes:
    0 — no error-severity diagnostics
    1 — at least one error-severity diagnostic
    2 — usage error, missing file, invalid config
"""
from __future__ import annotations

import argparse
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
        "--lfortran",
        metavar="PATH",
        help="Path to the lfortran binary (overrides $LFORTRAN_BIN).",
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
        "--no-cache", action="store_true", help="Disable the on-disk cache."
    )
    check.add_argument(
        "--cache-dir",
        help="Override cache directory (default: ./.dimfort/cache).",
    )

    sub.add_parser("lsp", help="Start the DimFort language server (stdio).")

    cache = sub.add_parser("cache", help="Manage the analysis cache.")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_clean = cache_sub.add_parser("clean", help="Delete the cache directory.")
    cache_clean.add_argument("--cache-dir", help="Cache directory to clean.")
    cache_info = cache_sub.add_parser("info", help="Show cache location and size.")
    cache_info.add_argument("--cache-dir", help="Cache directory to inspect.")

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
    # Lazy imports keep `dimfort --version` fast and avoid pulling LFortran
    # plumbing for users who only need `cache` or `lsp`.
    from dimfort.core import lfortran as lf
    from dimfort.core import unit_config  # noqa: F401 — populate DEFAULT_TABLE
    from dimfort.core.annotations import scan_file
    from dimfort.core.attach import attach
    from dimfort.core.checker import check
    from dimfort.core.diagnostics import Severity

    paths: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if p.is_dir():
            print(
                f"dimfort: directory paths are not supported yet: {p}",
                file=sys.stderr,
            )
            return 2
        if not p.is_file():
            print(f"dimfort: file not found: {p}", file=sys.stderr)
            return 2
        paths.append(p)

    color = _color_enabled(args.no_color)
    error_count = 0

    def emit(file: str, line: int, severity: str, code: str, message: str) -> None:
        nonlocal error_count
        if severity == "error":
            error_count += 1
        if args.quiet:
            return
        print(
            _format_diag(file, line, severity, code, message, color=color)
        )

    for p in paths:
        scan = scan_file(p)

        # Stage-1 errors (malformed @unit{...} occurrences).
        for err in scan.errors:
            emit(str(p), err.line, "error", "U001", err.reason)

        att = attach(scan)

        # Stage-2 diagnostics.
        for orph in att.orphans:
            emit(str(p), orph.line, "warning", "U006", orph.reason)
        for confl in att.conflicts:
            emit(
                str(p),
                confl.second_line,
                "error",
                "U-conflict",
                (
                    f"conflicting unit for {confl.variable!r}: "
                    f"{confl.first_unit} vs {confl.second_unit}"
                ),
            )
        for inter in att.intermediate_continuations:
            emit(str(p), inter.line, "error", "U010", inter.reason)

        # Semantic check needs the ASR.
        try:
            asr = lf.dump_tree(p, "asr", lfortran=args.lfortran)
        except lf.LFortranNotFound as exc:
            print(f"dimfort: {exc}", file=sys.stderr)
            return 2
        except lf.LFortranError as exc:
            stderr = exc.stderr.strip()
            head = stderr.splitlines()[0] if stderr else "no error message"
            emit(
                str(p),
                0,
                "error",
                "U007",
                f"lfortran could not load this file: {head}",
            )
            continue

        for d in check(asr, att.var_units, file=str(p)):
            severity = "error" if d.severity is Severity.ERROR else "warning"
            emit(str(p), d.start.line, severity, d.code, d.message)

    return 1 if error_count else 0


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        return _run_check(args)
    if args.command == "lsp":
        from dimfort.lsp.server import run_stdio

        run_stdio()
        return 0
    if args.command == "cache":
        from dimfort import cache

        cache_dir = (
            Path(args.cache_dir) if args.cache_dir else cache.default_cache_dir()
        )
        if args.cache_command == "clean":
            freed = cache.clean(cache_dir)
            print(f"removed {cache_dir} (freed {freed} bytes)")
            return 0
        if args.cache_command == "info":
            print(cache.info(cache_dir))
            return 0

    parser.print_help()
    return 0
