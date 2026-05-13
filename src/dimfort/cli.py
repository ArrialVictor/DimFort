import argparse
import sys
from collections.abc import Sequence

from dimfort import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dimfort",
        description="Check dimensional homogeneity of Fortran projects.",
    )
    parser.add_argument("--version", action="version", version=f"dimfort {__version__}")
    sub = parser.add_subparsers(dest="command", required=False)

    check = sub.add_parser("check", help="Check one or more Fortran files.")
    check.add_argument("paths", nargs="+", help="Fortran source files or directories.")
    check.add_argument("--no-cache", action="store_true", help="Disable the on-disk cache.")
    check.add_argument("--cache-dir", help="Override cache directory (default: ./.dimfort/cache).")

    sub.add_parser("lsp", help="Start the DimFort language server (stdio).")

    cache = sub.add_parser("cache", help="Manage the analysis cache.")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    cache_clean = cache_sub.add_parser("clean", help="Delete the cache directory.")
    cache_clean.add_argument("--cache-dir", help="Cache directory to clean.")
    cache_info = cache_sub.add_parser("info", help="Show cache location and size.")
    cache_info.add_argument("--cache-dir", help="Cache directory to inspect.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check":
        print(f"[dimfort] check is not implemented yet. paths={args.paths}", file=sys.stderr)
        return 0
    if args.command == "lsp":
        from dimfort.lsp.server import run_stdio

        run_stdio()
        return 0
    if args.command == "cache":
        from pathlib import Path

        from dimfort import cache

        cache_dir = Path(args.cache_dir) if args.cache_dir else cache.default_cache_dir()
        if args.cache_command == "clean":
            freed = cache.clean(cache_dir)
            print(f"removed {cache_dir} (freed {freed} bytes)")
            return 0
        if args.cache_command == "info":
            print(cache.info(cache_dir))
            return 0

    parser.print_help()
    return 0
