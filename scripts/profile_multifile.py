"""cProfile the workspace check pipeline.

Runs ``check_files`` over a workset twice (cold + warm) under
cProfile and prints the top-N hot functions by cumulative time for
each. Used by the 0.2.5 perf audit to ground optimisation decisions
in measured data rather than hunches.
"""
from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
import tempfile
from io import StringIO
from pathlib import Path

from dimfort.core import unit_config  # noqa: F401  (installs DEFAULT_TABLE)
from dimfort.core.cache_store import CacheStore
from dimfort.core.multifile import check_files
from dimfort.core.multifile_cache import ModuleExportsCache, TreeCache


def _collect(root: Path) -> list[Path]:
    if root.is_file():
        return [root.resolve()]
    out: list[Path] = []
    for ext in (".f90", ".F90"):
        out.extend(root.rglob(f"*{ext}"))
    return sorted(p.resolve() for p in out)


def _profile_run(label: str, fn, top: int = 25) -> None:
    pr = cProfile.Profile()
    pr.enable()
    fn()
    pr.disable()
    buf = StringIO()
    ps = pstats.Stats(pr, stream=buf).strip_dirs().sort_stats("cumulative")
    ps.print_stats(top)
    print(f"\n{'=' * 78}\n{label}\n{'=' * 78}")
    print(buf.getvalue())


def main(argv: list[str] | None = None) -> int:
    """Parse args, run cold + warm passes under cProfile."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--top", type=int, default=25)
    args = parser.parse_args(argv)

    files = _collect(args.path)
    if args.limit is not None:
        files = files[: args.limit]
    if not files:
        print(f"no Fortran files found under {args.path}", file=sys.stderr)
        return 2

    tree_cache = TreeCache()
    exports_cache = ModuleExportsCache()
    cache_root = Path(tempfile.mkdtemp(prefix="dimfort-prof-cache-"))
    cache_store = CacheStore(root=cache_root)

    print(f"workset: {len(files)} files under {args.path}")

    def cold() -> None:
        check_files(
            files, tree_cache=tree_cache, exports_cache=exports_cache,
            cache=cache_store, cache_mode="read-write",
        )

    def warm() -> None:
        check_files(
            files, tree_cache=tree_cache, exports_cache=exports_cache,
            cache=cache_store, cache_mode="read-write",
        )

    _profile_run("COLD pass", cold, top=args.top)
    _profile_run("WARM pass", warm, top=args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
