# Coverage visualization — design spec (FUTURE)

**Status:** future feature, design exploration. Captures the design
direction reached at the close of the 0.2.4 planning discussion. Targets
0.2.4 as the first item in that release.

The goal is a per-line view of "what does DimFort know about this line"
that lets a user — typically a climate scientist onboarding DimFort onto
a partly-annotated codebase — see at a glance which regions are
checked-and-OK, which need attention, and which are out of scope.
Reuses the existing checker output: no new analysis, only a different
projection of data already computed by `check_files`.

## 1. Problem this solves

A team adopting DimFort on an existing Fortran codebase needs to know,
without running through diagnostics one by one, which regions of the
code are dimensionally verified, which are flagged, and which DimFort
hasn't been told about at all.

Today, the surface answer is the **Problems panel** (LSP) or the
diagnostic stream (CLI). Both list issues; neither communicates the
*positive* signal: "these lines are dimensionally consistent." A user
inspecting a 500-line subroutine has no way to see "this 80% is fine,
this 20% needs work" without scanning for the absence of squiggles —
which doesn't distinguish "no squiggle because it's clean" from "no
squiggle because DimFort hasn't analysed this region."

The visualisation closes that gap. It paints a per-line marker for
**every** line: green for verified-OK, yellow for needs-attention, red
for hard fire, blue for unparsed, uncoloured for out-of-scope. The
user reads adoption progress at a glance.

## 2. Why visualisation, not a new diagnostic

A diagnostic-driven approach (emit an info-level diagnostic on every
verified line) would flood the diagnostic stream. The Problems panel is
designed to show **issues**, not adoption status. A 500-line
fully-annotated routine emitting 500 INFO diagnostics drowns the
two warnings that actually matter.

Visualisation lives in a separate channel: a per-line decoration layer
the user toggles independently of diagnostics. The diagnostic stream
stays a stream of issues; the coverage layer stays a stream of status.

## 3. Status taxonomy

Four tiers + a no-decoration default. Reuses every shipped colour from
`markers.md`; introduces no new colour.

| Tier | Trigger | Existing parity |
| --- | --- | --- |
| **Green** | Line carries unit-typed expressions; checker resolved them and no consistency-family diagnostic owns the line | matches panel/hover green |
| **Yellow** | A `U005` (unannotated-but-used) or `H010` (hint-level) diagnostic owns the line | matches panel/hover yellow |
| **Red** | An `H001` / `H002` / `H003` / `H004` (hard fire) diagnostic owns the line | matches panel/hover red |
| **Blue** | A `P001` (unparsed region) diagnostic owns the line | matches panel/hover blue |
| **— (no decoration)** | Line has no unit semantics (string assignments, control flow, comments, blank lines, decl-only lines with no expression) | uncoloured |

Three observations:

- The four-colour scheme is exactly the existing markers system. A user
  who has internalised `🟢🟡🔴🔵` from the side panel will read the
  gutter the same way.
- **No-decoration is distinct from green.** Uncoloured means "this line
  doesn't need checking" (a string assignment, a `goto`, a blank line).
  Green means "DimFort actively verified this line and it passed."
  Adoption progress reads as `green / (green + yellow + red + blue)`,
  with the no-decoration count showing how much of the file is out of
  scope.
- **Blue is distinct from no-decoration.** Blue means "DimFort tried to
  reach this line but the parser couldn't recover a unit-checkable
  AST." Blue lines are candidates for `#ifdef` cleanup or syntax fixes;
  no-decoration lines aren't.

### 3.1 Why no orange for `H010`

The earlier design proposed orange for hint-level fires. The shipped
scheme does not include orange; introducing it would commit a fifth
colour across panel, hover, and now this layer. The cost-benefit
doesn't justify it: `H010` is a hint-level fire that the panel and
hover already render in yellow, and the coverage layer's purpose is
"what's the status here" — `H010` is "needs attention," which yellow
already conveys. Folded into the yellow tier.

### 3.2 No green sub-tier for polymorphic verification

A line whose result type is `'a` (a tyvar — unit depends on the caller)
passes the polymorphic check just like a line whose result is `Pa*s`.
The polymorphic check verifies that any concrete instantiation will be
dimensionally consistent — soundness over an infinite family. There is
no weaker-than-concrete sense; both are fully verified. The coverage
view does not differentiate.

## 4. Three rendering layers

Two visible layers in v1, plus a mode-controlled escalation.

### 4.1 Gutter signs

Coloured dot in the editor's left margin, one per line. This is the
universal pattern for test-coverage tools.

| Companion | Mechanism | Notes |
| --- | --- | --- |
| VSCode | `TextEditorDecorationType` with `gutterIconPath` | Custom SVGs per tier |
| Nvim | `vim.fn.sign_define` + `vim.fn.sign_place` | Built-in `signcolumn` |
| Emacs | `fringe` bitmaps + overlays | Built-in `left-fringe` |

All three companions support gutter signs natively. No plugin
dependencies.

### 4.2 Line background tint

Subtle background colour wash on the whole line. Opt-in via mode
`verbose`. Heavier visual weight.

| Companion | Mechanism |
| --- | --- |
| VSCode | `TextEditorDecorationType` with `backgroundColor` (rgba with low alpha) |
| Nvim | extmark with `line_hl_group` |
| Emacs | overlay with `face :background` |

All three companions support per-line background colour natively.

### 4.3 Scrollbar markers — explicitly out of scope

VSCode supports `overviewRulerColor` natively, giving an at-a-glance
file-overview navigation strip. Nvim and Emacs do not:

- Nvim has no built-in scrollbar; the common pattern is via the
  `nvim-scrollbar` plugin or `statuscolumn` workarounds.
- Emacs's `scroll-bar-mode` is rendered by the GTK / Cocoa toolkit
  and does not expose per-line colouring.

Adding plugin dependencies for a marginal navigation gain would
expand the companion surface area for unclear benefit; the LSP's
existing Problems panel and `:lopen`-style commands already serve the
"navigate to issues" need.

Decision: gutter + tint only, uniformly across the three companions.
Scrollbar markers are a parked extension; they would land in a future
patch only if a strong user signal demands them.

## 5. Mode setting

A single setting key per companion controls the visualisation level:

```
dimfort.coverage.mode: "disabled" | "gutter" | "verbose"
```

| Mode | Renders | Use case |
| --- | --- | --- |
| **disabled** | Nothing. Companion suppresses the LSP request and the rendering pass entirely. | User who wants the diagnostic stream only |
| **gutter** | Gutter signs in the four tiers. | Default for users who want at-a-glance status without visual noise |
| **verbose** | Gutter signs + line background tint. | User actively driving adoption / coverage up |

Three modes is enough granularity. Two would conflate "I want a quick
hint" with "show me everything"; four would over-specify.

Default: `disabled` in v1. Users opt in explicitly. Rationale: an
opt-in feature on first ship lets early adopters validate the
taxonomy and the visual choice before the default flips. The default
can be promoted to `gutter` once the feature stabilises.

## 6. Gutter-clash resolution

The editor already paints native diagnostic icons in the same gutter
column it provides to our coverage layer:

- VSCode shows a red circle-X for `Error`, a yellow triangle for
  `Warning`, etc., automatically from `publishDiagnostics`.
- Nvim's `signcolumn` shows similar via the `LspDiagnosticSignError`
  / `LspDiagnosticSignWarn` / etc. signs registered by the LSP client.
- Emacs's flycheck-style display does the same.

If we also paint a red coverage sign on a line with a hard fire, the
gutter would render two red icons (or one would overwrite the other,
depending on priority resolution). Visual noise without information
gain.

**Resolution:** the coverage layer paints **green** and **blue** only
on lines without a native diagnostic icon. Lines with a yellow / red
diagnostic skip the coverage gutter — the native icon already
communicates that level of attention. The coverage layer's actual
value-add is the **positive signal**: green for "this line is verified
fine," which the native diagnostic stream cannot express.

Verbose mode is different: line background tint sits behind the text,
not in the gutter column, so the tint applies to every line including
those with native diagnostic icons. No competition.

Concretely:

```
gutter mode, line with H001:        [native red ×] foo = bar * baz
gutter mode, line with U005:        [native yellow !] qux = quux
gutter mode, line verified OK:      [coverage green ●] x = y * z   ! @unit{m}
gutter mode, line with P001:        [coverage blue ●] some_unparseable_stmt
gutter mode, line out of scope:     [no icon] do i = 1, n

verbose mode adds a tinted background to every coloured line.
```

## 7. Wire format

A new LSP method, paralleling `dimfort/panelInfo` / `dimfort/interactions`:

### 7.1 Request

```
method:  "dimfort/lineStatus"
params:  { "uri": "file:///path/to/file.f90" }
```

### 7.2 Response

```jsonc
{
  "uri": "file:///path/to/file.f90",
  "version": 12,                    // textDocument version this status reflects
  "lines": [
    { "line": 12, "status": "green" },
    { "line": 13, "status": "yellow" },
    { "line": 14, "status": "green" },
    { "line": 17, "status": "red" },
    { "line": 23, "status": "blue" }
  ]
}
```

Lines not appearing in the array are out-of-scope (no decoration).

### 7.3 Edge cases

- **File not in workset.** Response with empty `lines` array, current
  `version`. Companions render no decoration; uncoloured = absence of
  data is consistent with the no-decoration baseline.
- **File parse failure (no AST).** Same as not-in-workset: empty
  `lines`. The companion may surface a status-bar note ("DimFort
  could not parse this file") via a separate channel.
- **Partial parse failure (`P001` regions interleaved with parsed
  regions).** Per-line: parsed lines get their `green` / `yellow` /
  `red` per the taxonomy; `P001`-region lines get `blue`.
- **`@unit_assume` accepted lines.** The line carries an asserted
  unit; it counts as `green` for coverage purposes. The 🔵 in the
  panel marker scheme refers to per-row overlay (see `markers.md`
  §4.6); coverage `blue` is reserved for `P001`. They do not collide
  because `@unit_assume` carries no `P001`.

### 7.4 Lifecycle

- Companions request on `didOpen` and re-request on `didChange` (with
  debounce, matching the existing checker debounce) and `didSave`.
- Server responds from the last cached `WorksetResult` (`_last_result`
  in `state.py`, same source the markers system reads — see
  `markers.md` §4 caveat 1).
- Companions cache the response keyed by `(uri, version)` and replay
  decoration when the user toggles the visualisation mode on, without
  another round-trip.

## 8. Coverage statistics

The same data feeds a second feature: aggregate counts. Two surfaces:

### 8.1 CLI: `dimfort coverage <paths>`

New CLI subcommand alongside `check`, `interactions`, `lsp`. Output
similar to test-coverage tooling:

```
$ dimfort coverage src/

File                                       OK    Warn   Fire  Unparsed   Out  Coverage
src/dynamics/dyn3d_common.f90              412     8     0        0     78      98.1%
src/physics/large_scale_clouds.f90         234    45     3        2    102      83.5%
src/physics/turb_mod.f90                     0    87    11        0    120       0.0%
...
Workset total                            12847   612    47       18   4231      95.1%

Coverage = OK / (OK + Warn + Fire + Unparsed). Out-of-scope lines
are excluded from the denominator.
```

The denominator deliberately excludes out-of-scope lines: a 500-line
file with 100 lines of comments / blank / control flow has 400
checkable lines. The coverage percentage measures how many of the 400
DimFort verified, not 400/500.

Flags:

- `--json` — emit machine-readable JSON for CI consumption.
- `--threshold N` — exit non-zero if workset coverage is below N%
  (CI gate).
- `--by-module` — group by module instead of per-file.
- `--summary` — workset total only, no per-file rows.

### 8.2 LSP: `dimfort/coverageStats`

```
method:  "dimfort/coverageStats"
params:  { "uri"?: string }      // optional; omit for workspace-wide
response:
{
  "scope": "file" | "workspace",
  "uri":   "file://..."?,         // present if scope=file
  "files": [
    {
      "uri": "file://...",
      "ok": 412,
      "warn": 8,
      "fire": 0,
      "unparsed": 0,
      "out": 78,
      "coverage_pct": 98.1
    },
    ...
  ],
  "total": { "ok": 12847, "warn": 612, "fire": 47, "unparsed": 18, "out": 4231, "coverage_pct": 95.1 }
}
```

Companions surface as a status-bar widget (workspace total) and/or a
`:DimfortCoverage` command (per-file or workspace view).

## 9. Companion implementation notes

The per-companion work is mechanical — each companion already has the
infrastructure for decoration. Sketch shapes:

### 9.1 VSCode

```typescript
// One DecorationType per tier.
const decorations = {
  green: vscode.window.createTextEditorDecorationType({
    gutterIconPath: ctx.asAbsolutePath("media/coverage-green.svg"),
    gutterIconSize: "contain",
    backgroundColor: "rgba(40, 167, 69, 0.08)",  // verbose mode only
  }),
  yellow: /* ... */,
  red:    /* ... */,
  blue:   /* ... */,
};

async function refreshCoverage(uri: vscode.Uri): Promise<void> {
  if (mode === "disabled") return;
  const { lines } = await client.sendRequest("dimfort/lineStatus", { uri: uri.toString() });
  const buckets = bucketByStatus(lines);
  const editor = vscode.window.activeTextEditor;
  for (const [status, ranges] of buckets) {
    editor.setDecorations(decorations[status], ranges);
  }
}
```

### 9.2 Nvim

```lua
-- Sign definitions (gutter).
vim.fn.sign_define("DimfortCoverGreen", { text = "●", texthl = "DimfortCoverGreen" })
-- ... yellow, red, blue ...

-- Per-line background (verbose mode).
local ns = vim.api.nvim_create_namespace("DimfortCoverage")

local function refresh_coverage(bufnr, uri)
  if mode == "disabled" then return end
  client.request("dimfort/lineStatus", { uri = uri }, function(err, result)
    if err then return end
    vim.api.nvim_buf_clear_namespace(bufnr, ns, 0, -1)
    vim.fn.sign_unplace("DimfortCoverage", { buffer = bufnr })
    for _, entry in ipairs(result.lines) do
      vim.fn.sign_place(0, "DimfortCoverage", "DimfortCover" .. capitalize(entry.status),
                        bufnr, { lnum = entry.line })
      if mode == "verbose" then
        vim.api.nvim_buf_set_extmark(bufnr, ns, entry.line - 1, 0, {
          line_hl_group = "DimfortCoverBg" .. capitalize(entry.status),
        })
      end
    end
  end)
end
```

### 9.3 Emacs

```elisp
(defvar dimfort-coverage--overlays nil)

(defun dimfort-coverage-refresh (uri)
  (when (not (eq dimfort-coverage-mode 'disabled))
    (lsp-request-async "dimfort/lineStatus"
                       `(:uri ,uri)
                       (lambda (result)
                         (dolist (ov dimfort-coverage--overlays) (delete-overlay ov))
                         (setq dimfort-coverage--overlays nil)
                         (dolist (entry (gethash "lines" result))
                           (let* ((line (gethash "line" entry))
                                  (status (gethash "status" entry))
                                  (face (intern (format "dimfort-coverage-%s" status))))
                             ;; Fringe (gutter).
                             (overlay-put (make-overlay ...) 'before-string
                                          (propertize " " 'display
                                                      `((left-fringe dimfort-cover-bitmap ,face))))
                             ;; Background (verbose mode).
                             (when (eq dimfort-coverage-mode 'verbose)
                               (overlay-put (make-overlay ...) 'face
                                            `(:background ,(face-background face))))))))))
```

## 10. Server-side implementation

Server-side work re-uses the per-line data the checker already produces.

### 10.1 Data source

For each file in the last `WorksetResult`:

- The checker emits a `list[Diagnostic]`; each diagnostic carries
  `start.line`, `end.line`, and `code`.
- The checker's tree-walker visits expression nodes; for each, the
  walk knows whether the node resolved to a `Unit` (green-eligible)
  or didn't (out-of-scope unless a diagnostic owns the line).

### 10.2 Per-line projection

For each file:

1. Initialise an empty per-line status map.
2. Walk the diagnostics: for each, map its tier (red / yellow / blue)
   to every line in `start.line ..= end.line`, taking the worst tier
   on collision.
3. Walk the checker's visited expression nodes: for each line that
   contains a resolved expression and isn't already painted by step
   2, mark green.
4. Lines not painted by 2 or 3 stay out-of-scope (omitted from the
   response).

This is one extra pass over data already in memory; no re-check.

### 10.3 Handler placement

A new `lsp/coverage.py` module, paralleling `lsp/panel.py` /
`lsp/interactions.py`. The `server.py` spine adds a `@server.feature
("dimfort/lineStatus")` registration and delegates to
`coverage.resolve`. The handler does not need `state.ts_handler_lock`
— it reads from the cached `WorksetResult` only, like `panel.resolve`.

A second handler for `dimfort/coverageStats` aggregates over the same
projection.

### 10.4 CLI placement

A new `_run_coverage(args)` in `cli.py`, paralleling `_run_check` and
`_run_interactions`. Re-uses `check_files` for the analysis pipeline;
adds a counting + formatting pass over the result. The CLI dispatch in
`main()` gains a third subcommand branch.

## 11. Open questions

These are the points worth resolving in the spec review before
implementation begins:

1. **Default mode at first ship.** This doc proposes `disabled`
   (opt-in). An alternative is `gutter` (on by default). Trade-off:
   on-by-default is the better demo on first install, but uses
   gutter column real estate every user pays for whether they wanted
   it or not. Lean: `disabled`.

2. **Coverage-percentage semantics: per-line or per-AST-node?** The
   spec above measures per-line: a line with 3 expressions, 2
   verified + 1 yellow, counts as yellow. A per-AST-node count would
   be more granular (66.7% verified) but harder to communicate.
   Lean: per-line, with an `--ast-nodes` CLI flag as a future
   enhancement.

3. **CI gate semantics.** `--threshold 80` exits non-zero if
   workspace coverage is below 80%. Should this also support
   per-file thresholds (e.g. fail if any file is < 50%)? Lean: not
   in v1; aggregate threshold only.

4. **Promoting default to `gutter` later.** When the feature
   stabilises (post-0.2.4 user feedback), the default could flip
   from `disabled` to `gutter`. The flip is a one-line companion
   change but is visible to every user. Decision deferred to the
   release that flips it.

5. **`dimfort/lineStatus` revalidation cost.** For a 5000-line file
   with frequent edits, the per-line projection runs once per
   `didChange` debounce. Cheap, but worth measuring on a
   representative large workset before shipping to confirm.

## 12. Migration

For implementation in 0.2.4:

1. Server-side: add `lsp/coverage.py` with `resolve` (lineStatus) and
   `stats` handlers. Wire to `server.py`. Add CLI subcommand to
   `cli.py`. Tests under `tests/unit/test_lsp_coverage.py` and
   `tests/unit/test_cli_coverage.py`.
2. Companion-side: per-companion PR adding the decoration layer,
   setting key, status-bar widget. VSCode first (richest test
   surface), then Nvim, then Emacs.
3. Documentation: this doc moves from `docs/design/future/` to
   `docs/design/shipped/` when the implementation lands. A
   user-facing page at `docs/editor-integration/coverage.md`
   describes how to enable it per companion.
4. Release sequencing: server-side and CLI ship in DimFort 0.2.4
   together. Companions ship at the matching companion version
   tracking server 0.2.4.

## 13. Out of scope for this design

- **Tracking coverage over time.** Coverage history (commit-over-
  commit deltas) would require persistent storage. Out of scope.
- **CI integration beyond `--threshold`.** A GitHub Action /
  reviewdog plugin / etc. would consume the `--json` output but is
  not part of DimFort itself.
- **HTML report generation.** Test-coverage tools often emit static
  HTML; DimFort would defer to external consumers of the `--json`
  output if that ever becomes a demand.
- **Coverage of `@unit_assume`-accepted lines distinction.** The
  spec counts them as green (verified, just verified via an
  assertion). A future enhancement could split them into a
  green-but-assumed sub-bucket; not in v1.
- **Per-symbol coverage.** "How many symbols carry `@unit{}` vs not"
  is a different question, served by the `interactions` subcommand
  and (in the future) `dimfort audit`. Coverage is per-line.
