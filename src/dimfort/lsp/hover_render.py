"""Pure markdown rendering for the LSP hover surfaces.

Parser-agnostic: these functions turn already-resolved units, call
signatures, and module exports into the Unicode markdown VSCode shows in a
hover popup. They hold no LSP state and never touch tree-sitter — extracted
from ``server.py`` (the LSP-split refactor) so the dispatch and panel code can
share one rendering definition.
"""
from __future__ import annotations

from dimfort.core.symbols import FuncSig, ModuleExports
from dimfort.core.units import UnitExpr, format_unit


def _unit_pretty(u: UnitExpr | None) -> str:
    """Render a unit for the hover surfaces, delegating to the shared
    display formatter (:func:`format_unit`) so hovers, diagnostics, and
    the side panel all read identically — Unicode superscripts, ``·``
    products, signed-exponent powers (``kg·m·s⁻²``), and ``LOG(...)`` /
    ``EXP(...)`` per spec §9. ``None`` renders as ``"?"``.
    """
    return "?" if u is None else format_unit(u)


def _hover_text(
    name: str,
    unit_or_message: str,
    *,
    show_unit_label: bool = True,
    unit_source: str | None = None,
    marker: str | None = None,
) -> str:
    """Render a single-symbol hover (variable or struct member).

    Marker convention mirrors the trace-mode hover header:
    🟢 = known unit, 🟡 = no annotation / unresolved, 🔴 = owning
    diagnostic in error severity.

    The body sits inside a fenced code block so every DimFort hover
    surface — variable, signature, tree — uses the same visual form
    across clients. Clients that tint code blocks (e.g. Neovim) get
    consistent coloring; clients that don't (VSCode) render it as plain
    monospace, which still aligns nicely.

    ``unit_source`` (``"explicit"`` / ``"intrinsic_default"`` / ``None``)
    annotates *how* the unit was determined. ``"intrinsic_default"``
    appends ``(implicit — INTEGER default)`` so the user can see the
    Fortran-type-driven default at work rather than wondering why a
    bare ``integer :: i`` is showing as dim'less.

    ``marker`` overrides the default 🟢/🟡 derivation. Callers that
    have already computed the node's marker (via
    ``expr_tree._node_marker``) should pass it through so an LHS
    identifier flagged 🔴 by a diagnostic doesn't render 🟢 here.
    """
    if show_unit_label:
        body = f"{name} : {unit_or_message}"
        if unit_source == "intrinsic_default":
            body += " (implicit — INTEGER default)"
        default_marker = "🟢"
    else:
        body = f"{name} — {unit_or_message}"
        default_marker = "🟡"
    effective_marker = marker if marker is not None else default_marker
    return f"**{effective_marker} DimFort**\n\n```\n{body}\n```"


def _sig_render_md(name: str, sig: FuncSig) -> str:
    """Bare dimensional-signature text for embedding (e.g. module hover
    list items wrap it in inline backticks themselves).

    Format: ``name(u1, u2, …) : ret`` (functions) or
    ``name(u1, u2, …) : -`` (subroutines — the ``-`` is the
    structural-no-unit glyph, same as panel rows and call hovers, so the
    ":" is the universal "has unit" separator across every surface).
    Unannotated formal slots render as ``?``. Param names are
    intentionally omitted — physicists reading a call site want the
    dimensional interface, not the callee-internal naming.

    Polymorphic signatures (any tyvar present in arg or return units)
    prefix the rendering with ``∀ 'a.`` (one quantifier per declared
    tyvar, in sorted order) per the polymorphism spec — the marker
    distinguishes a generic helper from a concrete one at a glance.
    """
    from dimfort.core.polymorphism import free_tyvars_of_sig
    arg_units = ", ".join(
        _unit_pretty(u) if u is not None else "?" for u in sig.arg_units
    )
    if sig.is_subroutine:
        base = f"{name}({arg_units}) : -"
    else:
        ret = _unit_pretty(sig.return_unit) if sig.return_unit is not None else "?"
        base = f"{name}({arg_units}) : {ret}"
    tyvars = free_tyvars_of_sig(sig)
    if tyvars:
        prefix = " ".join(f"∀ {tv}." for tv in sorted(tyvars))
        return f"{prefix} {base}"
    return base


def _hover_signature(name: str, sig: FuncSig) -> str:
    # 🟡 when any formal param (or the return unit, for a function)
    # has no annotation — the signature renders that arg as `?`, so
    # the header should reflect the partial-knowledge state.
    any_unknown = any(u is None for u in sig.arg_units)
    if not sig.is_subroutine and sig.return_unit is None:
        any_unknown = True
    marker = "🟡" if any_unknown else "🟢"
    # Fenced block keeps the standalone signature hover visually aligned
    # with the variable + tree hovers (single rule across surfaces).
    return f"**{marker} DimFort**\n\n```\n{_sig_render_md(name, sig)}\n```"


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
            lines.append(f"- `{_sig_render_md(n, sig)}`")
        if len(sig_items) > _MODULE_HOVER_SIG_LIMIT:
            extra = len(sig_items) - _MODULE_HOVER_SIG_LIMIT
            lines.append(f"- *… {extra} more*")
    if not exports.all_var_names and not sig_items:
        lines.append("")
        lines.append("*(no module-level exports)*")
    return "\n".join(lines)
