"""Measure the multifile-cache effect on the load + index phases.

Runs ``check_files`` twice over the same workset: the first pass
populates the caches (cold), the second consumes them (warm). Prints
per-phase wall-clock timings side by side so the collapse on the warm
pass is visible.

Usage::

    python scripts/bench_multifile_cache.py <path> [--limit N]

``path`` should be a directory or single file; ``.f90`` / ``.F90``
files are picked up recursively. ``--limit`` truncates the workset for
quick spot checks.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dimfort.core import unit_config  # noqa: F401  (installs DEFAULT_TABLE)
from dimfort.core.multifile import check_files
from dimfort.core.multifile_cache import ModuleExportsCache, TreeCache


def _collect(root: Path) -> list[Path]:
    if root.is_file():
        return [root.resolve()]
    out: list[Path] = []
    for ext in (".f90", ".F90"):
        out.extend(root.rglob(f"*{ext}"))
    return sorted(p.resolve() for p in out)


def _fmt(secs: float) -> str:
    if secs < 0.001:
        return f"{secs * 1e6:6.1f} us"
    if secs < 1.0:
        return f"{secs * 1e3:6.1f} ms"
    return f"{secs:6.2f} s "


def main(argv: list[str] | None = None) -> int:
    """Parse args, run cold + warm passes, print a per-phase table."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Truncate the workset to the first N files (for quick checks).",
    )
    args = parser.parse_args(argv)

    files = _collect(args.path)
    if args.limit is not None:
        files = files[: args.limit]
    if not files:
        print(f"no Fortran files found under {args.path}", file=sys.stderr)
        return 2

    tree_cache = TreeCache()
    exports_cache = ModuleExportsCache()

    print(f"workset: {len(files)} files under {args.path}")
    print()
    print(f"{'phase':<10} {'cold':>10} {'warm':>10} {'speedup':>10}")
    print("-" * 44)

    # Cold pass populates the empty caches; warm pass consumes them.
    cold = check_files(
        files, tree_cache=tree_cache, exports_cache=exports_cache,
    )
    warm = check_files(
        files, tree_cache=tree_cache, exports_cache=exports_cache,
    )

    for phase in ("load", "aggregate", "index", "check"):
        c = cold.phase_timings.get(phase, 0.0)
        w = warm.phase_timings.get(phase, 0.0)
        speedup = (c / w) if w > 0 else float("inf")
        print(f"{phase:<10} {_fmt(c):>10} {_fmt(w):>10} {speedup:>9.1f}x")

    c_total = sum(cold.phase_timings.values())
    w_total = sum(warm.phase_timings.values())
    print("-" * 44)
    print(
        f"{'total':<10} {_fmt(c_total):>10} {_fmt(w_total):>10} "
        f"{(c_total / w_total if w_total > 0 else float('inf')):>9.1f}x"
    )
    print()
    print(f"tree_cache:    {len(tree_cache)} entries")
    print(f"exports_cache: {len(exports_cache)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
