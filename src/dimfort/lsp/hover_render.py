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
    """Render a unit expression for any hover surface.

    Delegates to the shared display formatter (:func:`format_unit`) so
    hovers, diagnostics, and the side panel all read identically:
    Unicode superscripts, ``·`` products, signed-exponent powers
    (``kg·m·s⁻²``), and ``LOG(...)`` / ``EXP(...)`` per spec §9.
    ``None`` (an unannotated / unresolved unit) becomes the literal
    ``"?"`` so caller code can substitute the rendered string into a
    template without a ``None`` check.

    Args:
        u: Resolved unit expression for the symbol being hovered, or
            ``None`` when no unit could be derived (unannotated
            variable, unresolved call, etc.).

    Returns:
        Display string ready to embed in a markdown hover body.

    Note:
        Callers should NOT post-process the returned string — it is
        already the canonical display form.
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

    The body sits inside a fenced code block so every DimFort hover
    surface — variable, signature, tree — uses the same visual form
    across clients. Clients that tint code blocks (e.g. Neovim) get
    consistent colouring; clients that don't (VSCode) render it as
    plain monospace, which still aligns nicely.

    Marker convention mirrors the trace-mode hover header: 🟢 = known
    unit, 🟡 = no annotation / unresolved, 🔴 = owning diagnostic in
    error severity.

    Args:
        name: Symbol name to display before the separator (variable,
            struct member, derived-type field).
        unit_or_message: Either the formatted unit string (when
            ``show_unit_label`` is ``True``) or a short free-text
            message (when ``False``) describing why no unit is
            available.
        show_unit_label: ``True`` for the "name : unit" form (known
            unit); ``False`` for the "name — message" form (unresolved
            / unannotated state).
        unit_source: How the unit was determined. ``"explicit"`` for a
            user ``@unit{}`` annotation, ``"intrinsic_default"`` for a
            Fortran-type-driven default (which appends
            ``(implicit — INTEGER default)`` so the reader sees the
            default at work), ``None`` to skip the annotation entirely.
        marker: Override the default 🟢 / 🟡 derivation. Callers that
            have already computed a node's marker (via
            :func:`dimfort.lsp.expr_tree._node_marker`) should pass it
            through so an LHS identifier flagged 🔴 by a diagnostic
            doesn't render 🟢 here.

    Returns:
        A markdown string with the bolded ``**<marker> DimFort**``
        header and the fenced code-block body, ready to drop into an
        LSP ``Hover`` payload.
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
    """Render a bare dimensional-signature for embedding.

    Used by surfaces that supply their own framing (e.g. module hover
    list items wrap the result in inline backticks themselves), so the
    returned string carries no fenced block, no header, and no marker.

    Format is ``name(u1, u2, …) : ret`` for functions or
    ``name(u1, u2, …) : -`` for subroutines — the ``-`` is the
    structural-no-unit glyph, matching panel rows and call hovers, so
    ``:`` is the universal "has unit" separator across every surface.
    Unannotated formal slots render as ``?``. Parameter names are
    intentionally omitted — physicists reading a call site want the
    dimensional interface, not the callee-internal naming.

    Polymorphic signatures (any type variable present in an arg or the
    return unit) are prefixed with ``∀ 'a.`` (one quantifier per
    declared tyvar, in sorted order) per the polymorphism spec, so a
    generic helper is distinguishable from a concrete one at a glance.

    Args:
        name: Callable name to render in the signature head.
        sig: Resolved :class:`FuncSig` carrying argument units, return
            unit, and the subroutine flag.

    Returns:
        Display string for the signature, without surrounding markdown
        framing.
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
    """Render a standalone signature hover for a function or subroutine.

    Wraps :func:`_sig_render_md` in the standard ``**<marker>
    DimFort**`` header + fenced code block so the signature popup
    renders consistently with the variable and tree hovers. The marker
    derives from the signature's completeness: 🟢 when every formal
    arg (and the return unit, for a function) is annotated, 🟡 as
    soon as any slot is ``None`` — matching the partial-knowledge state
    the rendered ``?`` placeholders convey.

    Args:
        name: Callable name to render in the signature head.
        sig: Resolved :class:`FuncSig` for the callee.

    Returns:
        Markdown hover body ready to drop into an LSP ``Hover``
        payload.
    """
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


# Module hover caps. Some clients render hover popups scrollably
# (VSCode); others (e.g. Neovim's default floating preview) do not —
# the cap is a safety belt for both cases against pathological
# re-export modules with thousands of entries. Set well above realistic
# large-codebase module sizes (≤ ~100 vars, ≤ ~50 procs); anything
# bigger gets the "more" tail so the popup doesn't pretend to be
# authoritative.
_MODULE_HOVER_VAR_LIMIT = 500
_MODULE_HOVER_SIG_LIMIT = 100


def _module_hover_md(
    module_name: str, exports: ModuleExports | None,
    *, external: bool, unresolved: bool,
) -> str:
    """Render a module summary for a ``use foo`` hover.

    Three states matter to the reader and are encoded in the
    boolean-flag pair:

    - **external** (``external=True``): the module sits in the user's
      external-modules allowlist; we know not to expect a definition
      in the workset, and the hover says so explicitly.
    - **unresolved** (``unresolved=True`` or ``exports is None``):
      referenced by ``use`` but no module of that name was loaded
      (typical for libraries DimFort doesn't track).
    - **resolved**: ``exports`` is populated; render the variable and
      procedure-signature surfaces.

    For the resolved branch the variable list is split into annotated
    rows (showing the unit) followed by unannotated rows (with a
    placeholder), which doubles the hover as a TODO list of what still
    needs ``@unit{}``. Both lists are capped (see
    :data:`_MODULE_HOVER_VAR_LIMIT` and
    :data:`_MODULE_HOVER_SIG_LIMIT`) with a ``… N more`` tail so a
    pathological re-export module can't blow up the popup.

    Args:
        module_name: Module name as written by the user at the ``use``
            site, used verbatim in the unresolved / external messages.
        exports: Resolved :class:`ModuleExports` for the module, or
            ``None`` when the module couldn't be loaded.
        external: ``True`` if the module is on the external-modules
            allowlist.
        unresolved: ``True`` if the module was referenced but no
            definition was found in the workset.

    Returns:
        Markdown hover body for the ``use`` statement, including the
        ``**<marker> DimFort**`` header and the variable / procedure
        sections.
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
