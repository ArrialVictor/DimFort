"""Disk persistence for the per-file module-exports cache (M5).

Mirrors the M4 / W3 pattern (:mod:`dimfort.core.multifile_cache_persist`,
:mod:`dimfort.core.workspace_index` save/load). The
:class:`~dimfort.core.multifile_cache.ModuleExportsCache` is in-memory
only by construction — it caches the output of
``collect_function_signatures_and_module_exports`` keyed by
``(content_hash, merged_units_digest)``. Without a disk layer the cache
warms from zero on every server restart and Phase C re-walks every
tree.

Codec strategy: hand-rolled JSON encode/decode per dataclass — no
pickle (the cache root is project-owned but session-shared on dev
boxes; pickle would be a security smell). The interesting work is
serializing :class:`~dimfort.core.units.UnitExpr`:

* ``Unit`` carries seven :class:`~dimfort.core.units.Exponent` dimension
  slots, a ``factor: Fraction``, an ``offset: Fraction`` (for degC and
  other affine quantities), and a list of polymorphic ``tyvars``.
* ``Exponent`` is a linear combination over Q with named generators.
* ``LogWrap`` / ``ExpWrap`` recurse on an inner ``UnitExpr``.

The high-level :func:`~dimfort.core.units.format_unit_source` /
:func:`~dimfort.core.units.parse` pair documented for the H010 quick-fix
is **lossy** for affine units (offset is dropped), so we cannot use it
as a round-trip codec — we serialize the structure directly.

``_EXPORTS_SCHEMA_VERSION`` is bumped whenever any of the serialised
dataclasses changes shape. Mismatch → silent drop + warm rebuild.

Bound
~~~~~
On disk: one file per workspace (``<cache_root>/exports-cache.json``);
size mirrors the in-memory :class:`~dimfort.core.multifile_cache.ModuleExportsCache`
entry count at save time. No on-disk cap; the in-memory cache's
``max_entries`` FIFO bound carries forward into the persisted file
(load → in-memory cap → next save reflects the post-cap state). One
file per session by construction.
"""
from __future__ import annotations

import contextlib
import json
import logging
from fractions import Fraction
from pathlib import Path
from typing import Any

from dimfort.core.multifile_cache import (
    ExportsKey,
    ModuleExportsCache,
)
from dimfort.core.symbols import FuncSig, ModuleExports
from dimfort.core.units import (
    Exponent,
    ExpWrap,
    LogWrap,
    Unit,
    UnitExpr,
)

log = logging.getLogger(__name__)

_EXPORTS_SCHEMA_VERSION = 1
_EXPORTS_CACHE_FILENAME = "module-exports-cache.json"


# ---------------------------------------------------------------------------
# Fraction
# ---------------------------------------------------------------------------


def _dump_fraction(f: Fraction) -> list[int]:
    """Serialise a ``Fraction`` as ``[numerator, denominator]``."""
    return [f.numerator, f.denominator]


def _load_fraction(d: Any) -> Fraction:
    if not isinstance(d, list) or len(d) != 2:
        raise ValueError(f"bad fraction payload: {d!r}")
    return Fraction(int(d[0]), int(d[1]))


# ---------------------------------------------------------------------------
# Exponent
# ---------------------------------------------------------------------------


def _dump_exponent(e: Exponent) -> dict[str, Any]:
    return {
        "terms": [
            [name, _dump_fraction(coef)] for name, coef in e.terms
        ],
        "constant": _dump_fraction(e.constant),
    }


def _load_exponent(d: dict[str, Any]) -> Exponent:
    terms = tuple(
        (str(name), _load_fraction(coef)) for name, coef in d["terms"]
    )
    return Exponent(terms=terms, constant=_load_fraction(d["constant"]))


# ---------------------------------------------------------------------------
# UnitExpr (Unit | LogWrap | ExpWrap)
# ---------------------------------------------------------------------------


def _dump_unit_expr(u: UnitExpr) -> dict[str, Any]:
    """Serialise a UnitExpr (Unit / LogWrap / ExpWrap) to a JSON-friendly dict."""
    if isinstance(u, LogWrap):
        return {"kind": "log", "inner": _dump_unit_expr(u.inner)}
    if isinstance(u, ExpWrap):
        return {"kind": "exp", "inner": _dump_unit_expr(u.inner)}
    if isinstance(u, Unit):
        return {
            "kind": "unit",
            "dimension": [_dump_exponent(e) for e in u.dimension],
            "factor": _dump_fraction(u.factor),
            "offset": _dump_fraction(u.offset),
            "tyvars": [
                [name, _dump_exponent(e)] for name, e in u.tyvars
            ],
        }
    raise TypeError(f"unsupported UnitExpr type: {type(u)!r}")


def _load_unit_expr(d: dict[str, Any]) -> UnitExpr:
    kind = d.get("kind")
    if kind == "log":
        return LogWrap(_load_unit_expr(d["inner"]))
    if kind == "exp":
        return ExpWrap(_load_unit_expr(d["inner"]))
    if kind == "unit":
        dimension = tuple(_load_exponent(e) for e in d["dimension"])
        tyvars = tuple(
            (str(name), _load_exponent(e)) for name, e in d["tyvars"]
        )
        return Unit(
            dimension=dimension,
            factor=_load_fraction(d["factor"]),
            offset=_load_fraction(d["offset"]),
            tyvars=tyvars,
        )
    raise ValueError(f"bad UnitExpr payload kind: {kind!r}")


def _dump_unit_expr_opt(u: UnitExpr | None) -> dict[str, Any] | None:
    return None if u is None else _dump_unit_expr(u)


def _load_unit_expr_opt(d: dict[str, Any] | None) -> UnitExpr | None:
    return None if d is None else _load_unit_expr(d)


# ---------------------------------------------------------------------------
# FuncSig
# ---------------------------------------------------------------------------


def _dump_func_sig(s: FuncSig) -> dict[str, Any]:
    return {
        "arg_names": list(s.arg_names),
        "arg_units": [_dump_unit_expr_opt(u) for u in s.arg_units],
        "return_unit": _dump_unit_expr_opt(s.return_unit),
        "is_subroutine": s.is_subroutine,
    }


def _load_func_sig(d: dict[str, Any]) -> FuncSig:
    return FuncSig(
        arg_names=tuple(str(n) for n in d["arg_names"]),
        arg_units=tuple(_load_unit_expr_opt(u) for u in d["arg_units"]),
        return_unit=_load_unit_expr_opt(d["return_unit"]),
        is_subroutine=bool(d["is_subroutine"]),
    )


# ---------------------------------------------------------------------------
# UseRef (inner_uses) — duck-typed shape ``.module / .only / .renames``
# ---------------------------------------------------------------------------


def _dump_use_ref(u: Any) -> dict[str, Any]:
    """Serialise an inner_uses element (UseRef duck-typed shape)."""
    return {
        "module": u.module,
        "only": None if u.only is None else list(u.only),
        "renames": [list(pair) for pair in u.renames],
    }


def _load_use_ref(d: dict[str, Any]) -> Any:
    """Reconstruct a UseRef from a serialised dict.

    Imported lazily to avoid a top-level cycle with
    :mod:`dimfort.core.workspace_index`.
    """
    from dimfort.core.workspace_index import UseRef
    only = d["only"]
    return UseRef(
        module=str(d["module"]),
        only=None if only is None else tuple(str(n) for n in only),
        renames=tuple(
            (str(pair[0]), str(pair[1])) for pair in d["renames"]
        ),
    )


# ---------------------------------------------------------------------------
# ModuleExports
# ---------------------------------------------------------------------------


def _dump_module_exports(m: ModuleExports) -> dict[str, Any]:
    return {
        "name": m.name,
        "var_units": [
            [name, _dump_unit_expr(u)] for name, u in m.var_units.items()
        ],
        "signatures": [
            [name, _dump_func_sig(s)] for name, s in m.signatures.items()
        ],
        "all_var_names": list(m.all_var_names),
        "inner_uses": [_dump_use_ref(u) for u in m.inner_uses],
        "default_private": m.default_private,
        "public_names": sorted(m.public_names),
        "private_names": sorted(m.private_names),
    }


def _load_module_exports(d: dict[str, Any]) -> ModuleExports:
    var_units = {
        str(name): _load_unit_expr(u) for name, u in d["var_units"]
    }
    signatures = {
        str(name): _load_func_sig(s) for name, s in d["signatures"]
    }
    inner_uses = tuple(_load_use_ref(u) for u in d["inner_uses"])
    return ModuleExports(
        name=str(d["name"]),
        var_units=var_units,
        signatures=signatures,
        all_var_names=tuple(str(n) for n in d["all_var_names"]),
        inner_uses=inner_uses,
        default_private=bool(d["default_private"]),
        public_names=frozenset(str(n) for n in d["public_names"]),
        private_names=frozenset(str(n) for n in d["private_names"]),
    )


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------


def _exports_cache_path(cache_root: Path) -> Path:
    return cache_root / _EXPORTS_CACHE_FILENAME


def save_persistent_exports_cache(
    cache: ModuleExportsCache, cache_root: Path,
) -> None:
    """Atomically write ``cache._entries`` to ``cache_root``.

    Mirrors the ProjectionCache disk layer (M4): tempfile + replace.
    Best-effort — any exception is logged and swallowed because losing
    the persisted cache only re-introduces the warm-rebuild cost on the
    next session start.
    """
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("M5 exports cache: cannot create %s: %s", cache_root, exc)
        return

    snapshot = list(cache._entries.items())  # noqa: SLF001
    payload: dict[str, Any] = {
        "version": _EXPORTS_SCHEMA_VERSION,
        "entries": [
            {
                "key": {
                    "content_hash": k.content_hash,
                    "merged_units_digest": k.merged_units_digest,
                },
                "signatures": [
                    [name, _dump_func_sig(s)]
                    for name, s in value[0].items()
                ],
                "modules": [
                    [name, _dump_module_exports(m)]
                    for name, m in value[1].items()
                ],
            }
            for k, value in snapshot
        ],
    }

    target = _exports_cache_path(cache_root)
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        tmp.replace(target)
    except OSError as exc:
        log.warning("M5 exports cache: write %s failed: %s", target, exc)
        # Best-effort cleanup of the partial file.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def load_persistent_exports_cache(cache_root: Path) -> ModuleExportsCache | None:
    """Return a populated cache from disk, or ``None`` on any failure.

    Failures (missing file, corrupt JSON, version mismatch, codec
    error) are silent: the LSP just starts cold. Best-effort by design.
    """
    path = _exports_cache_path(cache_root)
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("M5 exports cache: load %s failed: %s", path, exc)
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _EXPORTS_SCHEMA_VERSION:
        return None
    entries_raw = payload.get("entries")
    if not isinstance(entries_raw, list):
        return None

    cache = ModuleExportsCache()
    try:
        for entry in entries_raw:
            key_raw = entry["key"]
            key = ExportsKey(
                content_hash=str(key_raw["content_hash"]),
                merged_units_digest=str(key_raw["merged_units_digest"]),
            )
            signatures = {
                str(name): _load_func_sig(s)
                for name, s in entry["signatures"]
            }
            modules = {
                str(name): _load_module_exports(m)
                for name, m in entry["modules"]
            }
            cache.put(key, (signatures, modules))
    except (KeyError, ValueError, TypeError) as exc:
        log.warning("M5 exports cache: codec error in %s: %s", path, exc)
        return None
    return cache


__all__ = [
    "load_persistent_exports_cache",
    "save_persistent_exports_cache",
]
