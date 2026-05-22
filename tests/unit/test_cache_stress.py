"""Cache stress test — the correctness gate for the content-hash cache.

Procedure (one iteration):

1. Start from a small known-bad workspace (mixed correct + incorrect
   unit code so diagnostics fire).
2. Run cold with cache disabled. Record diagnostics.
3. Mutate one random file in one of several ways.
4. Run cached on the mutated state. Record diagnostics.
5. Run cold on the same mutated state with a *fresh* cache dir.
   Record diagnostics.
6. (cached run) and (cold run) must produce identical diagnostics.

The test asserts byte-identical diagnostic sets across many random
edits. A failure here means the cache is invalidating too coarsely
(spurious diff) or — much worse — too aggressively (stale diff).

Runs 100 iterations by default — matches the gate the design doc
commits to before considering the cache trustworthy. Bump
``STRESS_ITERATIONS`` locally if a flake needs more samples.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import pytest

from dimfort.core.cache_store import CacheStore
from dimfort.core.multifile import check_files

STRESS_ITERATIONS = 100


# Three small fixtures. The variety here is what gives the stress test
# bite: cross-file dependencies, multiple modules, deliberate unit
# bugs that should fire diagnostics consistently across runs.
FIXTURES: dict[str, str] = {
    "csts.f90": (
        "module csts_mod\n"
        "  real, parameter :: speed_of_sound = 343.0  ! @unit{m/s}\n"
        "  real, parameter :: g = 9.81                ! @unit{m/s**2}\n"
        "end module\n"
    ),
    "geom.f90": (
        "module geom_mod\n"
        "  real :: r  ! @unit{m}\n"
        "  real :: a  ! @unit{m**2}\n"
        "end module\n"
    ),
    "user_a.f90": (
        "subroutine compute_a\n"
        "  use csts_mod\n"
        "  use geom_mod\n"
        "  real :: t  ! @unit{s}\n"
        "  real :: d  ! @unit{m}\n"
        "  d = speed_of_sound * t\n"
        "  d = r + d\n"
        "end subroutine\n"
    ),
    "user_b.f90": (
        "subroutine compute_b\n"
        "  use csts_mod\n"
        "  real :: h  ! @unit{m}\n"
        "  real :: t  ! @unit{s}\n"
        "  h = g * t * t\n"
        "end subroutine\n"
    ),
}


@dataclass(frozen=True)
class _DiagKey:
    """Comparable diagnostic projection — file + code + position + message."""
    file: str
    code: str
    severity: str
    sl: int
    sc: int
    el: int
    ec: int
    message: str


def _diag_set(result, paths: list[Path]) -> set[_DiagKey]:
    out: set[_DiagKey] = set()
    for p in paths:
        for d in result.diagnostics.get(p, []):
            out.add(_DiagKey(
                file=d.file, code=d.code, severity=d.severity.value,
                sl=d.start.line, sc=d.start.column,
                el=d.end.line, ec=d.end.column,
                message=d.message,
            ))
    return out


def _materialise(tmp_path: Path) -> list[Path]:
    paths = []
    for name, content in FIXTURES.items():
        p = tmp_path / name
        p.write_text(content)
        paths.append(p)
    return paths


def _mutate(rng: random.Random, paths: list[Path]) -> None:
    """Apply a random mutation to one random file."""
    target = rng.choice(paths)
    text = target.read_text()
    lines = text.splitlines(keepends=True)
    op = rng.choice(("insert_blank", "duplicate_line", "delete_blank",
                     "rename_unused", "touch_only"))
    if op == "insert_blank":
        idx = rng.randint(0, len(lines))
        lines.insert(idx, "\n")
    elif op == "duplicate_line":
        # Duplicate a comment-ish or blank line to avoid creating
        # syntax errors that would shift diagnostics dramatically.
        candidates = [i for i, ln in enumerate(lines) if ln.strip() in ("", "  ")]
        if candidates:
            i = rng.choice(candidates)
            lines.insert(i, lines[i])
    elif op == "delete_blank":
        candidates = [i for i, ln in enumerate(lines) if ln.strip() == ""]
        if candidates:
            lines.pop(rng.choice(candidates))
    elif op == "rename_unused":
        # Rename ``r`` → ``r_renamed`` only in geom_mod. This DOES alter
        # the consumer (user_a) — that's the point: dep invalidation
        # must catch it.
        if "geom.f90" in target.name:
            text = "".join(lines).replace("  real :: r", "  real :: r2")
            target.write_text(text)
            return
    # ``touch_only``: write the same content back (no-op semantically,
    # but bumps mtime — should still hit cache because hash unchanged).
    target.write_text("".join(lines))


@pytest.mark.parametrize("seed", list(range(STRESS_ITERATIONS)))
def test_cached_matches_cold(tmp_path: Path, seed: int):
    """One stress iteration: random edit, then assert cached == cold."""
    rng = random.Random(seed)
    paths = _materialise(tmp_path)
    cache_dir = tmp_path / ".dimfort-cache"

    # 1. Cold run to populate cache.
    cache = CacheStore(root=cache_dir)
    check_files(paths, cache=cache, cache_mode="read-write")

    # 2. Mutate.
    _mutate(rng, paths)

    # 3. Cached run on mutated state.
    cached_result = check_files(paths, cache=cache, cache_mode="read-write")

    # 4. Cold run on the same mutated state with a *fresh* cache dir.
    fresh_cache = CacheStore(root=tmp_path / ".fresh-cache")
    cold_result = check_files(paths, cache=fresh_cache, cache_mode="read-write")

    cached_set = _diag_set(cached_result, paths)
    cold_set = _diag_set(cold_result, paths)

    # Symmetric difference must be empty.
    diff = cached_set.symmetric_difference(cold_set)
    if diff:
        only_cached = cached_set - cold_set
        only_cold = cold_set - cached_set
        msg_parts = [f"seed={seed} divergence:"]
        for d in sorted(only_cached, key=lambda x: (x.file, x.sl, x.code)):
            msg_parts.append(f"  CACHED-ONLY {d}")
        for d in sorted(only_cold, key=lambda x: (x.file, x.sl, x.code)):
            msg_parts.append(f"  COLD-ONLY   {d}")
        pytest.fail("\n".join(msg_parts))
