"""Measure ``scan_workspace`` wall-clock on a real workset.

Times two configurations:
- ``--max-workers=1`` (effectively sequential, matches pre-W1 behaviour)
- default parallel (cpu_count - 1)

Prints the speedup ratio. Used to ground the W1 commit's claim.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dimfort.core.workspace_index import scan_workspace


def main(argv: list[str] | None = None) -> int:
    """Parse args, run sequential + parallel scans, print results."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if not args.path.exists():
        print(f"path not found: {args.path}", file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    idx_seq = scan_workspace([args.path], max_workers=1)
    seq = time.perf_counter() - t0

    t0 = time.perf_counter()
    idx_par = scan_workspace([args.path])
    par = time.perf_counter() - t0

    n_files = (
        len(idx_seq.uses_by_file) + len(idx_seq.scan_failures)
    )
    print(f"workset: {n_files} files under {args.path}")
    print(f"sequential (--max-workers=1): {seq:.2f} s")
    print(f"parallel   (default):         {par:.2f} s")
    print(f"speedup: {seq / par:.2f}x")
    # Sanity: indexes should be identical.
    assert idx_seq.modules == idx_par.modules
    assert idx_seq.procedures == idx_par.procedures
    assert idx_seq.uses_by_file == idx_par.uses_by_file
    assert idx_seq.calls_by_file == idx_par.calls_by_file
    print("indexes identical: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
