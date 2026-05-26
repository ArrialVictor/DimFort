"""Pure markdown rendering for the LSP hover surfaces.

Parser-agnostic: these functions turn already-resolved units, call
signatures, and module exports into the Unicode markdown VSCode shows in a
hover popup. They hold no LSP state and never touch tree-sitter — extracted
from ``server.py`` (the LSP-split refactor) so the dispatch and panel code can
share one rendering definition.
"""
from __future__ import annotations

from dimfort.core.symbols import FuncSig, ModuleExports
from dimfort.core.units import UnitExpr
from dimfort.core.units import base_symbols as _base_symbols

_SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "-": "⁻", "(": "⁽", ")": "⁾", "/": "ᐟ",
}


def _to_superscript(s: str) -> str:
    return "".join(_SUPERSCRIPTS.get(c, c) for c in s)


def _unit_pretty(u: UnitExpr | None) -> str:
    """Render a Unit using Unicode (× for product, ⁿ superscripts, /
    for division). KaTeX isn't enabled in VSCode's default hover, so
    we keep everything in plain text.

    ``LogWrap`` / ``ExpWrap`` recursively print as ``LOG(...)`` /
    ``EXP(...)`` per spec §9.
    """
    if u is None:
        return "?"
    from dimfort.core.units import ExpWrap as _ExpWrap
    from dimfort.core.units import LogWrap as _LogWrap
    if isinstance(u, _LogWrap):
        return f"LOG({_unit_pretty(u.inner)})"
    if isinstance(u, _ExpWrap):
        return f"EXP({_unit_pretty(u.inner)})"
    names = _base_symbols()
    pos: list[str] = []
    neg: list[str] = []
    for sym, exp in zip(names, u.dimension, strict=False):
        if exp.is_zero():
            continue
        q = exp.as_fraction()
        if q is not None:
            mag = abs(q)
            if mag == 1:
                term = sym
            elif mag.denominator == 1:
                term = sym + _to_superscript(str(int(mag)))
            else:
                term = f"{sym}^({mag})"
            (pos if q > 0 else neg).append(term)
        else:
            term = f"{sym}^({exp})"
            pos.append(term)
    body = " × ".join(pos) if pos else "1"
    if neg:
        denom = " × ".join(neg)
        if len(neg) > 1:
            denom = f"({denom})"
        body = f"{body} / {denom}"
    return body


def _hover_text(
    name: str,
    unit_or_message: str,
    *,
    show_unit_label: bool = True,
    unit_source: str | None = None,
) -> str:
    """Render a single-symbol hover (variable or struct member).

    Marker convention mirrors the trace-mode hover header:
    🟢 = known unit, 🟡 = no annotation / unresolved.

    ``unit_source`` (``"explicit"`` / ``"intrinsic_default"`` / ``None``)
    annotates *how* the unit was determined. ``"intrinsic_default"``
    appends *(implicit — INTEGER default)* so the user can see the
    Fortran-type-driven default at work rather than wondering why a
    bare ``integer :: i`` is showing as dim'less.
    """
    if show_unit_label:
        body = f"**{name}** : {unit_or_message}"
        if unit_source == "intrinsic_default":
            body += " *(implicit — INTEGER default)*"
        marker = "🟢"
    else:
        body = f"**{name}** — {unit_or_message}"
        marker = "🟡"
    return f"**{marker} DimFort**\n\n{body}"


def _sig_render_md(name: str, sig: FuncSig) -> str:
    """Markdown rendering of a call signature."""
    args = ", ".join(
        f"{arg_name}: {_unit_pretty(arg_unit) if arg_unit is not None else '?'}"
        for arg_name, arg_unit in zip(sig.arg_names, sig.arg_units, strict=False)
    )
    if sig.is_subroutine:
        return f"`{name}({args})`"
    ret = _unit_pretty(sig.return_unit) if sig.return_unit is not None else "?"
    return f"`{name}({args})` : {ret}"


def _hover_signature(name: str, sig: FuncSig) -> str:
    # 🟡 when any formal param (or the return unit, for a function)
    # has no annotation — the signature renders that arg as `?`, so
    # the header should reflect the partial-knowledge state.
    any_unknown = any(u is None for u in sig.arg_units)
    if not sig.is_subroutine and sig.return_unit is None:
        any_unknown = True
    marker = "🟡" if any_unknown else "🟢"
    return f"**{marker} DimFort**\n\n{_sig_render_md(name, sig)}"


# Module hover caps. VSCode's hover popup is scrollable, so we
# don't actually need to truncate to fit on screen — the cap is
# only a safety belt against pathological re-export modules with
# thousands of entries. Set well above realistic large-codebase module
# sizes (≤ ~100 vars, ≤ ~50 procs); anything bigger gets the "more"
# tail so the popup doesn't pretend to be authoritative.
_MODULE_HOVER_VAR_LIMIT = 500
_MODULE_HOVER_SIG_LIMIT = 100


def _module_hover_md(
    module_name: str, exports: ModuleExports | None,
    *, external: bool, unresolved: bool,
) -> str:
    """Render a module summary for a ``use foo`` hover.

    Three states matter to the reader:

    - ``external``: in the user's external-modules allowlist; we
      know not to expect a definition in the workset.
    - ``unresolved``: referenced by ``use`` but no module of that
      name was loaded (typical for libraries DimFort doesn't
      track).
    - resolved: ``exports`` is populated; render var + sig surface.
    """
    if external:
        return (
            f"**🟢 DimFort**\n\n"
            f"**module `{module_name}`** *(external — treated as known)*"
        )
    if exports is None or unresolved:
        return (
            f"**🟡 DimFort**\n\n"
            f"**module `{module_name}`** — *not found in workset*"
        )
    lines: list[str] = ["**🟢 DimFort**\n", f"**module `{exports.name}`**"]
    # Walk every declared module variable (in source order), emitting
    # the unit when one was attached and a "no unit annotation"
    # placeholder when not. Surfacing both states in the same list
    # makes the gap actionable: the hover doubles as a TODO of
    # which variables in this module still need annotation.
    if exports.all_var_names:
        lines.append("")
        annotated_count = sum(1 for n in exports.all_var_names if n in exports.var_units)
        total = len(exports.all_var_names)
        if annotated_count < total:
            lines.append(f"**Variables** ({annotated_count}/{total} annotated):")
        else:
            lines.append("**Variables**:")
        # Stable order: annotated first, then unannotated. Easier to
        # scan when you're looking for "what's known" vs "what's missing".
        annotated = [n for n in exports.all_var_names if n in exports.var_units]
        unannotated = [n for n in exports.all_var_names if n not in exports.var_units]
        shown: list[str] = []
        for n in annotated:
            shown.append(f"- `{n}`: {_unit_pretty(exports.var_units[n])}")
        for n in unannotated:
            shown.append(f"- `{n}` — *no unit annotation*")
        if len(shown) > _MODULE_HOVER_VAR_LIMIT:
            lines.extend(shown[:_MODULE_HOVER_VAR_LIMIT])
            lines.append(f"- *… {len(shown) - _MODULE_HOVER_VAR_LIMIT} more*")
        else:
            lines.extend(shown)
    sig_items = list(exports.signatures.items())
    if sig_items:
        lines.append("")
        lines.append("**Procedures**:")
        for n, sig in sig_items[:_MODULE_HOVER_SIG_LIMIT]:
            lines.append(f"- {_sig_render_md(n, sig)}")
        if len(sig_items) > _MODULE_HOVER_SIG_LIMIT:
            extra = len(sig_items) - _MODULE_HOVER_SIG_LIMIT
            lines.append(f"- *… {extra} more*")
    if not exports.all_var_names and not sig_items:
        lines.append("")
        lines.append("*(no module-level exports)*")
    return "\n".join(lines)
