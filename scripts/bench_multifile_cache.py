"""Measure the multifile-cache effect on the load + index phases.

Runs ``check_files`` three times over the same workset:

1. **cold** — every cache empty. Populates the disk CacheStore and the
   in-memory Tree / ModuleExports / Projection caches.
2. **warm** — same process, every cache populated. The fully-warm
   path users see when they re-run a check inside an already-running
   server session.
3. **post-restart** — disk CacheStore retained, but Tree / Exports /
   Projection caches dropped. Models a fresh server process opening
   onto a project whose on-disk caches survived from a prior session.
   This is the regime real users hit on every ``nvim`` start.

Comparing **post-restart** against **warm** isolates the cost that an
M4-style disk-persistent ProjectionCache could eliminate. Comparing
**post-restart** against **cold** shows the (already shipped) win the
disk CacheStore delivers.

After each engine pass, the bench also runs the **LSP-layer post-check
work** — ``build_workspace_payload`` — and reports its time as a
separate row. This is the user-perceived-wall-clock column that the
0.2.5 perf cycle was blind to: the engine bench measured ``check_files``
total but never the wrapping work the LSP server adds before returning
the response to the companion. ``check_files + payload`` is the closest
proxy to "wall-clock from ``:DimFortCheckWorkspace`` to bar update"
that doesn't need a live ``pygls`` server.

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

import time

from dimfort.core import unit_config  # noqa: F401  (installs DEFAULT_TABLE)
from dimfort.core.cache_store import CacheStore
from dimfort.core.multifile import check_files
from dimfort.core.multifile_cache import (
    ModuleExportsCache,
    ProjectionCache,
    TreeCache,
)
from dimfort.core.multifile_cache_persist import (
    load_persistent_projection_cache,
    save_persistent_projection_cache,
)
from dimfort.lsp.coverage import build_workspace_payload


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
    parser.add_argument(
        "--no-cache-store",
        action="store_true",
        help=(
            "Skip the per-file diagnostic CacheStore (shipped in 0.2.4). "
            "Default-on so the bench mirrors what users see in LSP use; "
            "disable to isolate the load + index gains."
        ),
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
    projection_cache = ProjectionCache()

    import tempfile
    cache_store: CacheStore | None = None
    cache_mode = "off"
    if not args.no_cache_store:
        cache_root = Path(tempfile.mkdtemp(prefix="dimfort-bench-cache-"))
        cache_store = CacheStore(root=cache_root)
        cache_mode = "read-write"

    print(f"workset: {len(files)} files under {args.path}")
    print(
        f"CacheStore: {'on (' + cache_mode + ')' if cache_store else 'off'}"
    )
    print()
    print(f"{'phase':<10} {'cold':>10} {'warm':>10} {'post-rs':>10}")
    print("-" * 55)

    # Cold pass populates every cache; warm pass consumes them. The
    # CacheStore default-on mirrors the LSP / `dimfort check` runtime
    # contract where the diagnostic cache is engaged.
    cold = check_files(
        files, tree_cache=tree_cache, exports_cache=exports_cache,
        projection_cache=projection_cache,
        cache=cache_store, cache_mode=cache_mode,
    )
    warm = check_files(
        files, tree_cache=tree_cache, exports_cache=exports_cache,
        projection_cache=projection_cache,
        cache=cache_store, cache_mode=cache_mode,
    )
    # Post-restart pass: drop the in-memory caches but keep the disk
    # CacheStore. Mirrors what happens when a fresh ``dimfort lsp``
    # process attaches to a project whose ``.dimfort-cache/`` survived
    # from yesterday's session.
    #
    # M4: after the warm pass finishes, persist the ProjectionCache
    # to disk and reload it into the fresh post-restart cache. The
    # cache populated by the warm pass is what the LSP would have
    # written at the end of its prior session.
    if cache_store is not None:
        save_persistent_projection_cache(projection_cache, cache_store.root)
    tree_cache_pr = TreeCache()
    exports_cache_pr = ModuleExportsCache()
    if cache_store is not None:
        projection_cache_pr = (
            load_persistent_projection_cache(cache_store.root)
            or ProjectionCache()
        )
    else:
        projection_cache_pr = ProjectionCache()
    post_restart = check_files(
        files, tree_cache=tree_cache_pr, exports_cache=exports_cache_pr,
        projection_cache=projection_cache_pr,
        cache=cache_store, cache_mode=cache_mode,
    )

    # LSP-layer post-check work: ``build_workspace_payload`` is the
    # single biggest chunk of "post-check, pre-return" work in
    # ``_check_whole_workspace`` (see lsp/coverage.py:122). Time it
    # for each engine regime so the wall-clock perceived by the
    # editor user is visible in the bench.
    def _time_payload(result):
        t0 = time.monotonic()
        build_workspace_payload(result)
        return time.monotonic() - t0
    cold_payload = _time_payload(cold)
    warm_payload = _time_payload(warm)
    pr_payload = _time_payload(post_restart)

    for phase in ("load", "aggregate", "index", "check"):
        c = cold.phase_timings.get(phase, 0.0)
        w = warm.phase_timings.get(phase, 0.0)
        pr = post_restart.phase_timings.get(phase, 0.0)
        print(f"{phase:<10} {_fmt(c):>10} {_fmt(w):>10} {_fmt(pr):>10}")

    c_total = sum(cold.phase_timings.values())
    w_total = sum(warm.phase_timings.values())
    pr_total = sum(post_restart.phase_timings.values())
    print("-" * 55)
    print(
        f"{'engine':<10} {_fmt(c_total):>10} {_fmt(w_total):>10} "
        f"{_fmt(pr_total):>10}"
    )
    # LSP-layer rows below the engine totals — what the user actually
    # waits for between hitting :DimFortCheckWorkspace and seeing the
    # bar update. payload = ``build_workspace_payload``; user-wall is
    # the engine + payload sum (a lower-bound proxy — the real
    # LSP layer adds per-file publishDiagnostics fan-out, inlay
    # refresh, and the disk save, none of which are reproducible
    # without a live pygls server).
    print(
        f"{'payload':<10} {_fmt(cold_payload):>10} {_fmt(warm_payload):>10} "
        f"{_fmt(pr_payload):>10}"
    )
    print("-" * 55)
    print(
        f"{'user-wall':<10} {_fmt(c_total + cold_payload):>10} "
        f"{_fmt(w_total + warm_payload):>10} "
        f"{_fmt(pr_total + pr_payload):>10}"
    )
    print()
    print(f"tree_cache:    {len(tree_cache)} entries (warm)")
    print(f"exports_cache: {len(exports_cache)} entries (warm)")
    print()
    # Engine-side ceilings (what disk-persisting in-memory caches
    # could still buy): post-restart − warm is the gap a TreeCache /
    # ModuleExportsCache disk layer would have to close.
    pr_minus_w = pr_total - w_total
    c_minus_pr = c_total - pr_total
    print(f"Engine ceiling (post-restart − warm): {_fmt(pr_minus_w)}")
    print(f"CacheStore wins (cold − post-rs):     {_fmt(c_minus_pr)}")
    # LSP-layer ceiling: build_workspace_payload runs at a flat
    # ~constant cost regardless of engine cache state — it's a
    # cache-size invariant (workspace file count is what scales it).
    # Reducing that floor needs either the cache routing fix
    # (_project_and_aggregate → _get_file_coverage) or merging the
    # three-walk projection into one, both audit findings.
    print(
        f"LSP-layer floor (max payload cost):   "
        f"{_fmt(max(cold_payload, warm_payload, pr_payload))}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
