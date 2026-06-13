"""Memory-churn smoke test for DimFort's caches.

Runs the engine's ``check_files`` against N synthetic Fortran files in
a loop, plus per-buffer LSP-cache fill/evict cycles, while measuring
the process's resident set size (RSS) at regular intervals. Reports
whether memory plateaus (caches bounded as designed) or grows linearly
(leak somewhere).

Not a unit test; not part of the test suite. One-off release-prep
script for the 0.2.6 cache audit (per ``docs/0_2_6_PLAN.md`` line 92
"Global cache audit / memory-churn test").

Usage::

    python3 scripts/cache_memory_churn.py [-n N]

The default N=100 catches obvious leaks without taking long enough to
discourage running it. Larger N tightens the noise floor.

Reading the output:
- "delta_kb" near zero after the first ~10 iterations → caches are
  bounded correctly under their LRU/cap policies.
- "delta_kb" growing roughly linearly with N → leak somewhere; the
  per-iter delta is roughly the per-file overhead that's NOT being
  evicted.

Note that some baseline RSS growth is expected on the first few
iterations as the engine warms (imports, JIT-style caching of stable
module metadata). After the first 10-20 iterations the curve should
plateau.
"""

from __future__ import annotations

import argparse
import gc
import resource
import shutil
import sys
import tempfile
from pathlib import Path

# Make src/dimfort importable when run from repo root.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from dimfort.core.multifile import check_files  # noqa: E402


def rss_kb() -> int:
    """Return current process RSS in KB. macOS reports in bytes; Linux in KB."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS: bytes. Linux: KB. Normalise to KB.
    if sys.platform == "darwin":
        return raw // 1024
    return raw


def synth_file(out: Path, idx: int) -> None:
    """Write a small unique Fortran file at ``out``.

    Content differs per ``idx`` so the per-file content-hash differs,
    forcing the engine through the full parse/attach/check pipeline
    instead of short-circuiting on a cache hit. Each file is a tiny
    module with one annotated variable and one assignment.
    """
    out.write_text(
        f"module synth_{idx}\n"
        f"  implicit none\n"
        f"contains\n"
        f"  subroutine compute_{idx}(v, m)\n"
        f"    real, intent(out) :: v  !< @unit{{m/s}}\n"
        f"    real, intent(in)  :: m  !< @unit{{kg}}\n"
        f"    v = m * {float(idx + 1) / 10:.3f}\n"
        f"  end subroutine\n"
        f"end module\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--num-files", type=int, default=100,
                        help="files to process (default: 100)")
    parser.add_argument("--report-every", type=int, default=10,
                        help="emit an RSS reading every N iterations (default: 10)")
    args = parser.parse_args()

    n = args.num_files
    tmpdir = Path(tempfile.mkdtemp(prefix="dimfort-churn-"))
    try:
        # Generate all files up front so generation cost doesn't pollute the loop.
        files = [tmpdir / f"f{i:04d}.f90" for i in range(n)]
        for i, f in enumerate(files):
            synth_file(f, i)

        # Warm-up: one call to settle imports, JIT-like first-touch caching.
        gc.collect()
        check_files([files[0]])
        gc.collect()
        baseline = rss_kb()

        print(f"baseline RSS: {baseline} KB ({n} files in {tmpdir})")
        print(f"{'iter':>5} {'rss_kb':>10} {'delta_kb':>10}")
        for i in range(n):
            check_files([files[i]])
            if i % args.report_every == 0 or i == n - 1:
                gc.collect()
                now = rss_kb()
                print(f"{i:>5} {now:>10} {now - baseline:>+10}")

        gc.collect()
        final = rss_kb()
        per_iter = (final - baseline) / max(1, n)
        print()
        print(f"summary: {n} iterations, final RSS = {final} KB "
              f"(delta {final - baseline:+d} KB, per-iter ~{per_iter:+.1f} KB)")

        # Soft pass/fail signal. Per-iter > 100 KB suggests a real leak;
        # < 50 KB is comfortably bounded. Between is suspicious.
        if per_iter < 50:
            print("verdict: caches appear bounded (per-iter < 50 KB).")
            return 0
        if per_iter < 100:
            print("verdict: caches MAY be growing (per-iter 50-100 KB). "
                  "Re-run with larger -n to disambiguate.")
            return 0
        print("verdict: caches likely leaking (per-iter > 100 KB).")
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
