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
    from dimfort.core.diagnostics import Severity
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
    result = check_files(
        paths,
        cpp_defines=config.cpp_defines,
        include_paths=config.include_paths,
        external_modules=frozenset(config.external_modules),
    )

    per_file_counts: list[tuple[Path, int, int]] = []
    for p in paths:
        diags = result.diagnostics.get(p.resolve(), [])
        h = sum(1 for d in diags if d.code.startswith("H"))
        u = sum(1 for d in diags if d.code.startswith("U"))
        per_file_counts.append((p, h, u))
        for d in diags:
            severity = "error" if d.severity is Severity.ERROR else "warning"
            emit(d.file, d.start.line, severity, d.code, d.message)

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

    parser.print_help()
    return 0
