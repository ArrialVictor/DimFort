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

    lsp = sub.add_parser("lsp", help="Start the DimFort language server (stdio).")
    # Some LSP clients (vscode-languageclient with TransportKind.stdio) tack
    # this argument on automatically. We only speak stdio, so it's a no-op
    # but we accept it so the server doesn't crash on launch.
    lsp.add_argument(
        "--stdio", action="store_true", help=argparse.SUPPRESS
    )

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
    from dimfort.core.diagnostics import Severity
    from dimfort.core.multifile import check_files

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
        print(_format_diag(file, line, severity, code, message, color=color))

    from dimfort import cache as _cache_mod

    if args.no_cache:
        cache_dir: Path | None = None
    elif args.cache_dir:
        cache_dir = Path(args.cache_dir)
    else:
        cache_dir = _cache_mod.default_cache_dir()

    try:
        result = check_files(
            paths, lfortran=args.lfortran, cache_dir=cache_dir
        )
    except lf.LFortranNotFound as exc:
        print(f"dimfort: {exc}", file=sys.stderr)
        return 2

    for p in paths:
        for d in result.diagnostics.get(p.resolve(), []):
            severity = "error" if d.severity is Severity.ERROR else "warning"
            emit(d.file, d.start.line, severity, d.code, d.message)

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
