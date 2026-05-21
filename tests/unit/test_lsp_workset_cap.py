"""Unit test for the LSP workset-cap helper.

Trimming the workset is what keeps the LSP process alive on
large workspaces: resolving the full `use` closure of a deep
entry point (e.g. a top-level driver pulling ~350 dependent files)
holds enough AST/ASR JSON in memory to get the Python process
SIGKILLed by macOS jetsam. The cap trades cross-file coverage for
stability.
"""
from __future__ import annotations

from pathlib import Path

from dimfort.lsp.server import _cap_workset


def _mk(name: str) -> Path:
    return Path(f"/tmp/{name}")


def test_no_cap_when_below_limit():
    paths = [_mk("a.f90"), _mk("b.f90"), _mk("c.f90")]
    out = _cap_workset(paths, _mk("c.f90"), limit=10)
    assert out == paths


def test_cap_keeps_last_n_when_over_limit():
    paths = [_mk(f"f{i}.f90") for i in range(10)]
    active = _mk("f9.f90")
    out = _cap_workset(paths, active, limit=3)
    assert out == [_mk("f7.f90"), _mk("f8.f90"), _mk("f9.f90")]


def test_cap_ensures_active_is_present_even_if_not_in_tail():
    # Construct a case where the active file would be sliced out by
    # naive tail-take. Shouldn't happen via resolve_workset (active is
    # always last in topo) but guard anyway.
    paths = [_mk(f"f{i}.f90") for i in range(10)]
    active = _mk("f0.f90")  # active sits at the head
    out = _cap_workset(paths, active, limit=3)
    assert active in out
    assert len(out) == 3


def test_cap_preserves_must_keep_entries_against_topo_truncation():
    # Active file is at the end; must_keep entries sit early in topo
    # order (typical for direct external-procedure callees with
    # shallow dep trees). Without the pin, they'd be sliced out by
    # `paths[-limit:]`; with the pin, they survive.
    paths = [_mk(f"f{i}.f90") for i in range(10)]
    active = _mk("f9.f90")
    must_keep = frozenset({_mk("f0.f90"), _mk("f2.f90")})
    out = _cap_workset(paths, active, limit=4, must_keep=must_keep)
    assert active in out
    for p in must_keep:
        assert p in out, f"{p} should have been pinned"
    assert len(out) == 4


def test_cap_keeps_topo_order_in_output():
    # Output must remain in topo order so downstream consumers
    # process dependencies before users.
    paths = [_mk(f"f{i}.f90") for i in range(10)]
    out = _cap_workset(
        paths, _mk("f9.f90"), limit=5,
        must_keep=frozenset({_mk("f1.f90")}),
    )
    # Out is a subset of `paths` in original order.
    indices = [paths.index(p) for p in out]
    assert indices == sorted(indices)
