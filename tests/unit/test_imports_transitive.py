"""Transitive re-export resolution for the panel's Imports section.

Covers :func:`dimfort.core.symbols.compute_transitive_exports` at the
algebra level (so the rules can be exercised without spinning up the
whole tree-sitter pipeline), plus an end-to-end check that
:func:`dimfort.lsp.imports.build_imports` surfaces a transitively
re-exported symbol with the correct provenance.

Fortran rules under test:

1. Default visibility is PUBLIC — a re-exporting module passes every
   imported symbol through unless it explicitly hides it.
2. ``use foo, only: …`` along the chain narrows what's re-exported.
3. ``use foo, local => remote`` renames carry through to consumers.
4. Module-level ``private`` flips the default; ``public :: name`` /
   ``private :: name`` override per name.
5. Cycles between two modules are tolerated (no infinite loop).
6. The closure is computed once per workspace pass, not per cursor call.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from textwrap import dedent

import pytest

from dimfort.core.symbols import (
    FuncSig,
    ModuleExports,
    compute_transitive_exports,
)
from dimfort.core.units import ZERO_DIM, Unit
from dimfort.core.workspace_index import UseRef


def _u(symbol: str = "m") -> Unit:
    """Build a distinguishable ``Unit`` keyed off a symbol string.

    The closure logic is unit-agnostic — it just shuttles ``UnitExpr``
    references around — so we only need distinguishable instances to
    assert "this exact one came through". Using a per-symbol rational
    factor in an otherwise-dimensionless slot is the cheapest way to do
    that without dragging in the unit-parser machinery.
    """
    return Unit(ZERO_DIM, Fraction(hash(symbol) % 9973 + 1, 1))


def _mod(
    name: str,
    *,
    var_units: dict[str, Unit] | None = None,
    all_var_names: tuple[str, ...] | None = None,
    signatures: dict[str, FuncSig] | None = None,
    inner_uses: tuple[UseRef, ...] = (),
    default_private: bool = False,
    public_names: frozenset[str] = frozenset(),
    private_names: frozenset[str] = frozenset(),
) -> ModuleExports:
    vu = var_units or {}
    return ModuleExports(
        name=name,
        var_units=dict(vu),
        signatures=signatures or {},
        all_var_names=all_var_names if all_var_names is not None else tuple(vu),
        inner_uses=inner_uses,
        default_private=default_private,
        public_names=public_names,
        private_names=private_names,
    )


class TestTransitiveClosure:
    def test_two_hop_reexport_surfaces_symbol(self):
        """``solver use phys_constants use phys_base`` sees ``g0``."""
        g0 = _u("g0")
        play = _u("play")
        modules = {
            "phys_base": _mod("phys_base", var_units={"g0": g0}),
            "phys_constants": _mod(
                "phys_constants",
                var_units={"play": play},
                inner_uses=(UseRef("phys_base", None, ()),),
            ),
            "solver": _mod(
                "solver",
                inner_uses=(UseRef("phys_constants", None, ()),),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        # phys_constants re-exports g0 with origin = phys_base.
        assert v["phys_constants"]["g0"] == (g0, "phys_base")
        assert v["phys_constants"]["play"] == (play, "phys_constants")
        # solver sees both transitively, still tracking phys_base origin
        # for g0 (so panel nav jumps to the real declaration site).
        assert v["solver"]["g0"] == (g0, "phys_base")
        assert v["solver"]["play"] == (play, "phys_constants")

    def test_only_filter_narrows_reexport(self):
        """``use foo, only: bar`` at the intermediate hop drops the rest."""
        g0 = _u("g0")
        other = _u("other")
        modules = {
            "phys_base": _mod(
                "phys_base", var_units={"g0": g0, "other": other},
            ),
            "phys_constants": _mod(
                "phys_constants",
                inner_uses=(UseRef("phys_base", ("g0",), ()),),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        assert "g0" in v["phys_constants"]
        assert "other" not in v["phys_constants"]

    def test_rename_composes_across_hops(self):
        """``use foo, gravity => g0`` exposes ``gravity`` (not ``g0``)."""
        g0 = _u("g0")
        modules = {
            "phys_base": _mod("phys_base", var_units={"g0": g0}),
            "phys_constants": _mod(
                "phys_constants",
                inner_uses=(
                    UseRef("phys_base", ("gravity",), (("gravity", "g0"),)),
                ),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        assert v["phys_constants"]["gravity"] == (g0, "phys_base")
        assert "g0" not in v["phys_constants"]

    def test_module_level_private_blocks_reexport(self):
        """A bare ``private`` flips the default → nothing leaves."""
        g0 = _u("g0")
        modules = {
            "phys_base": _mod("phys_base", var_units={"g0": g0}),
            "phys_constants": _mod(
                "phys_constants",
                var_units={"play": _u("play")},
                inner_uses=(UseRef("phys_base", None, ()),),
                default_private=True,
            ),
        }
        v, _ = compute_transitive_exports(modules)
        assert v["phys_constants"] == {}

    def test_public_reopens_named_symbol_after_default_private(self):
        """``private`` + ``public :: g0`` re-exposes just ``g0``."""
        g0 = _u("g0")
        modules = {
            "phys_base": _mod("phys_base", var_units={"g0": g0}),
            "phys_constants": _mod(
                "phys_constants",
                var_units={"play": _u("play")},
                inner_uses=(UseRef("phys_base", None, ()),),
                default_private=True,
                public_names=frozenset({"g0"}),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        assert set(v["phys_constants"].keys()) == {"g0"}
        assert v["phys_constants"]["g0"] == (g0, "phys_base")

    def test_private_named_symbol_hides_specific_name(self):
        """``private :: g0`` shuts one name even with default public."""
        g0 = _u("g0")
        play = _u("play")
        modules = {
            "phys_base": _mod("phys_base", var_units={"g0": g0}),
            "phys_constants": _mod(
                "phys_constants",
                var_units={"play": play},
                inner_uses=(UseRef("phys_base", None, ()),),
                private_names=frozenset({"g0"}),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        assert "g0" not in v["phys_constants"]
        assert "play" in v["phys_constants"]

    def test_cycle_does_not_infinite_loop(self):
        """Two modules that ``use`` each other terminate."""
        a_local = _u("a")
        b_local = _u("b")
        modules = {
            "a": _mod(
                "a",
                var_units={"a_var": a_local},
                inner_uses=(UseRef("b", None, ()),),
            ),
            "b": _mod(
                "b",
                var_units={"b_var": b_local},
                inner_uses=(UseRef("a", None, ()),),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        # Each module sees at least its own var. The back-edge is cut
        # on cycle (rather than hanging) — symmetric resolution is not
        # guaranteed in either direction, but both sides terminate.
        assert "a_var" in v["a"]
        assert "b_var" in v["b"]

    def test_signatures_reexport_through_chain(self):
        """Procedures pass through transitive ``use`` like variables do."""
        sig = FuncSig(
            arg_names=("x",),
            arg_units=(None,),
            return_unit=None,
            is_subroutine=False,
        )
        modules = {
            "base": _mod("base", signatures={"helper": sig}),
            "middle": _mod(
                "middle",
                inner_uses=(UseRef("base", None, ()),),
            ),
        }
        _, s = compute_transitive_exports(modules)
        assert s["middle"]["helper"] == (sig, "base")

    def test_local_declaration_wins_over_transitive(self):
        """Same-named local declaration shadows the transitive import."""
        local_g0 = _u("local_g0")
        upstream_g0 = _u("upstream_g0")
        modules = {
            "base": _mod("base", var_units={"g0": upstream_g0}),
            "consumer": _mod(
                "consumer",
                var_units={"g0": local_g0},
                inner_uses=(UseRef("base", None, ()),),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        # The consumer's own declaration is what gets re-exported.
        assert v["consumer"]["g0"] == (local_g0, "consumer")

    def test_closure_computed_once_per_pass_not_per_cursor(self):
        """Performance smoke: 50-module straight chain resolves fast.

        Asserts the memoised closure terminates near-instantly even on
        a deep chain — a non-memoised walk would be O(N²) in the
        re-exported symbol count and start to show wallclock cost.
        """
        import time

        seed = _u("seed")
        modules: dict[str, ModuleExports] = {
            "m0": _mod("m0", var_units={"seed": seed}),
        }
        for i in range(1, 50):
            modules[f"m{i}"] = _mod(
                f"m{i}",
                inner_uses=(UseRef(f"m{i - 1}", None, ()),),
            )
        t0 = time.perf_counter()
        v, _ = compute_transitive_exports(modules)
        dt = time.perf_counter() - t0
        # 50 modules through a memoised closure should run in
        # well under 50ms on any reasonable machine.
        assert dt < 0.05, f"closure too slow: {dt:.3f}s"
        assert v["m49"]["seed"] == (seed, "m0")

    def test_unannotated_local_var_carries_through_with_none_unit(self):
        """A var declared without ``@unit{}`` re-exports as ``(None, origin)``."""
        modules = {
            "base": _mod(
                "base",
                var_units={},
                all_var_names=("density",),
            ),
            "middle": _mod(
                "middle",
                inner_uses=(UseRef("base", None, ()),),
            ),
        }
        v, _ = compute_transitive_exports(modules)
        assert v["middle"]["density"] == (None, "base")


@pytest.fixture()
def imports_qa_workset(tmp_path: Path) -> Path:
    """Write the manual-QA ``imports_qa.f90`` fixture and return its path.

    Mirrors the fixture in the companion MANUAL_QA scenes, with the
    ``solver use phys_constants`` line *unfiltered* so transitive
    re-export of ``g0`` is exercised.
    """
    src = dedent("""\
        module phys_base
          real :: g0   !< @unit{m/s^2}
        end module phys_base

        module phys_constants
          use phys_base
          real :: play     !< @unit{Pa}
          real :: grav     !< @unit{m/s^2}
          real :: density
        contains
          function gravity_at(h) result(g)
            real, intent(in) :: h   !< @unit{m}
            real             :: g   !< @unit{m/s^2}
            g = grav
          end function gravity_at
          subroutine set_play(p)
            real, intent(in) :: p   !< @unit{Pa}
            play = p
          end subroutine set_play
        end module phys_constants

        module solver
          use phys_constants
          real :: local_p   !< @unit{Pa}
        contains
          subroutine step()
            local_p = play
            call set_play(local_p)
          end subroutine step
        end module solver
        """)
    p = tmp_path / "imports_qa.f90"
    p.write_text(src)
    return p


class TestBuildImportsEndToEnd:
    def test_g0_appears_under_phys_base_via_phys_constants(
        self, imports_qa_workset: Path,
    ):
        """End-to-end: cursor inside ``step`` lists ``g0`` from ``phys_base``."""
        pytest.importorskip("pygls")
        from dimfort.core.multifile import check_files
        from dimfort.lsp.imports import build_imports

        result = check_files([imports_qa_workset])
        tree_path = next(iter(result.trees.keys()))
        tree, source = result.trees[tree_path]

        text = source.decode("utf-8")
        cursor_line = next(
            i for i, line in enumerate(text.splitlines(), start=1)
            if "local_p = play" in line
        )
        rows = build_imports(
            tree, source, cursor_line, result, frozenset({"local_p"}),
        )

        by_name = {r["name"]: r for r in rows}
        # g0 is the smoking gun — present, with origin phys_base and a
        # ``viaModule`` of phys_constants (the directly-used module).
        assert "g0" in by_name, f"g0 missing from imports; got {list(by_name)}"
        g0_row = by_name["g0"]
        assert g0_row["module"] == "phys_base"
        assert g0_row["viaModule"] == "phys_constants"
        assert g0_row["kind"] == "annotated"
        assert g0_row["unit"]  # non-empty unit string
        # Nav jumps to phys_base's declaration site, line 2 in the fixture.
        assert g0_row["line"] == 2
        # Existing direct imports unchanged.
        for direct in ("play", "grav", "density", "gravity_at", "set_play"):
            assert direct in by_name, f"missing direct import {direct}"
            assert by_name[direct]["module"] == "phys_constants"
            assert "viaModule" not in by_name[direct]
