"""Cache memory-churn regression gate (Track D Ring 2 deliverable).

Pushes 200 unique-content synthesized Fortran files through
:func:`dimfort.core.multifile.check_files` in a loop and asserts that
per-iteration RSS growth stays under 50 KB. Catches accidental
unbounded cache growth introduced by future cache changes — a real
leak shows up as roughly-constant per-iteration delta, well above
the threshold.

The 50 KB threshold sits in the malloc-fragmentation / GC slack noise
floor at N=200; sustained growth above it indicates a missing
eviction or unbounded sub-memo. The 0.2.6 baseline measured per-iter
~+1.8 KB, well inside the cap.

Companion: :mod:`scripts.cache_memory_churn` is the interactive
variant — same logic, prints the growth curve, useful for diagnosing
which iteration the growth starts. The test only asserts the
post-loop ratio; the script lets a human see *where* in the loop it
went bad.
"""
from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

# ``resource`` is Unix-only. Windows doesn't ship it, so a bare
# ``import resource`` at module top errors at collection time and
# aborts the whole suite before pytest can honour any test-level
# skipif marker. ``importorskip`` fails soft: the whole module is
# skipped on platforms where the module can't be imported.
resource = pytest.importorskip(
    "resource",
    reason="resource.getrusage not available on Windows",
)

from dimfort.core.multifile import check_files  # noqa: E402 -- after importorskip

# Per Track D Ring 2 plan: 50 KB / iter with N>=200 is the regression
# gate. Below ~10 KB indicates true bounded steady state; 10-50 KB is
# malloc-fragmentation slack; above 50 KB suggests a real leak.
_PER_ITER_KB_CAP = 50
_N = 200


def _rss_kb() -> int:
    """Return current process RSS in KB (cross-platform normalisation).

    macOS reports ``ru_maxrss`` in bytes, Linux in KB. Caller wants KB.
    """
    # ``resource`` is loaded via ``importorskip`` so mypy sees ``Any``;
    # ``ru_maxrss`` is an int on every platform where the field exists.
    raw: int = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if sys.platform == "darwin":
        return raw // 1024
    return raw


def _synth_file(out: Path, idx: int) -> None:
    """Write a small unique Fortran module at ``out``.

    Content varies with ``idx`` so the content hash differs, forcing
    the engine through the full parse/attach/check pipeline instead
    of short-circuiting on a cache hit. Tiny module, one annotated
    variable, one assignment — enough to fill every cache slot.
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


def test_cache_memory_churn_under_threshold(tmp_path: Path) -> None:
    """Per-iteration RSS growth must stay under 50 KB across 200 files."""
    files = [tmp_path / f"f{i:04d}.f90" for i in range(_N)]
    for i, f in enumerate(files):
        _synth_file(f, i)

    # Warm-up: settle imports + first-touch caching before baseline.
    gc.collect()
    check_files([files[0]])
    gc.collect()
    baseline = _rss_kb()

    for f in files:
        check_files([f])

    gc.collect()
    final = _rss_kb()
    delta = final - baseline
    per_iter_kb = delta / _N

    assert per_iter_kb < _PER_ITER_KB_CAP, (
        f"cache memory churn regressed: per-iter RSS growth "
        f"{per_iter_kb:.1f} KB exceeds {_PER_ITER_KB_CAP} KB threshold "
        f"(baseline={baseline} KB, final={final} KB, "
        f"delta={delta:+d} KB, N={_N})"
    )
