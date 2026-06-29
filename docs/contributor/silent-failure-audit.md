# Silent-failure audit — DimFort 0.2.7

**Date:** 2026-06-28.
**Scope:** every silent-failure-shaped pattern in `src/dimfort/lsp/`,
spot-checks in `src/dimfort/core/`. Companion-side equivalents
(VSCompanion / Nvim / Emacs) audited separately in their respective
0.2.7 PRs.
**Methodology:** exhaustive walk against 10 patterns —
`_notify(...)` calls, `log.exception` / `log.warning` / `log.error`
on user-handler paths, bare `except:` / `except Exception: pass`,
`return None` after caught error, daemon-thread targets without
exception capture, `contextlib.suppress` blocks, scheduled-not-
awaited notifications, workers marking complete on partial failure.

This file is the written deliverable the audit produces.

## Audit philosophy

The line between "silent is correct" and "silent is wrong":

- **Silent is correct** when the exception is expected, the
  recovery is documented, and the failure has no user-visible
  effect *or* the user-visible effect is the intended behaviour
  (e.g., "no result" rendering as an empty popup). Mark with
  `# audited(0.2.7): silent-OK — <reason>`.
- **Silent is wrong** when the user triggered the action,
  something failed, and the user sees no signal that anything
  went wrong. The fix is `log.warning` (Output channel) for
  high-frequency paths, `_notify(toast=True)` (popup) for
  user-blocking failures.

The annotation tag `audited(0.2.7)` enables future audits to diff
against this baseline via grep — adding a new `_notify(...)` or
silent `except` without an annotation fails the CI grep gate.

## Patterns covered

| # | Pattern | Findings |
|---|---|---|
| 1 | `_notify(...)` calls in `server.py` | 13 sites — all OK by construction (helper is for user-visible notifications); inventoried in §1 |
| 2 | `log.exception/warning/error` on user-handler paths | 10 sites — 5 fixed with toast, 1 left silent-OK with annotation, 4 stayed log-only with documented rationale (§2) |
| 3 | `except Exception: return None` in handlers | 9 sites — 4 fixed with `log.warning`, 5 annotated silent-OK (§3) |
| 4 | Daemon-thread targets without exception capture | 2 cache-save daemons — wrapped to distinguish "best-effort write failed" from "LSP crashed" (§4) |
| 5 | `contextlib.suppress` blocks | 11 sites — all transport/progress guards, OK by construction (§5) |
| 6 | Workers marking complete on partial failure | 2 sites — coverage refresh + index build, fixed (§6) |

## §1 `_notify` registry

Every `_notify(...)` call in `src/dimfort/lsp/server.py`.
Classification is the audit-baseline contract; the registry doubles
as documentation of where user-visible notifications fire.

| file:line | classification | toast? | message |
|---|---|---|---|
| server.py:534 | telemetry | no | "workset capped at {cap}" |
| server.py:639 *(new — toast added)* | error-surfacing | yes | "checker crashed on {file}" |
| server.py:1050 | telemetry | no | "DimFort LSP initialised" |
| server.py:1070 | telemetry | no | LSP log-level override confirmation |
| server.py:1087 | user-facing | yes | "no workspace folder open" |
| server.py:1131 | telemetry | no | "scanning workspace" |
| server.py:1276 *(new — toast added)* | error-surfacing | yes | "workspace index build failed" |
| server.py:1315 | user-facing | yes | "workspace index ready" |
| server.py:1331 | telemetry | no | post-index refresh count |
| server.py:1434 *(new — toast added)* | error-surfacing | yes | "post-check failure on {file}" |
| server.py:1489 *(new — toast added)* | error-surfacing | yes | "post-save check failure on {file}" |
| server.py:1616 *(new — toast added)* | error-surfacing | yes | "live-check failure on {file}" |
| server.py:2017 | user-facing | yes | "workspace check already in progress" |
| server.py:2092 | error-surfacing | yes | "workspace check: index not ready" |
| server.py:2106 | error-surfacing | yes | "workspace check: no files found" |
| server.py:2130 | telemetry | no | "checking workspace (N files)" |
| server.py:2244 *(new — toast added)* | error-surfacing | yes | "workspace check failed" |
| server.py:2332 | telemetry | no | "projecting workspace coverage" |
| server.py:2369 | user-facing | yes | "workspace check complete" |

**Plus one out-of-server.py site added by this audit:**
`coverage.py:_run_workspace_check` — inline `ls.window_show_message`
on workspace-coverage-refresh failure. Mirrors `_notify(toast=True)`
without the circular-import overhead. Consolidating the toast helper
into a shared `lsp/notify.py` is queued for 0.2.8 (sub-`#3` items
above each routed through that path).

## §2 `log.*` on user-handler paths

| file:line | level | classification | resolution |
|---|---|---|---|
| server.py:631 | log.exception | error-surfacing — pipeline crash on didOpen/didSave/didChange | Toast added (§1) |
| server.py:1263 | log.exception | error-surfacing — workspace index build failed | Toast added (§1) |
| server.py:1294 | log.exception | telemetry — background post-index refresh | OK (log-only sufficient; not user-triggered) |
| server.py:1334 | log.exception | NEEDS-ANNOTATION — workspace index update on save/change | Annotated silent-OK (background refresh; failure surfaces on next pipeline run) |
| server.py:1413 | log.exception | error-surfacing — didOpen worker crash | Toast added (§1) |
| server.py:1472 | log.exception | error-surfacing — didSave worker crash | Toast added (§1) |
| server.py:1591 | log.debug | telemetry — hover buffer fetch fallback | OK |
| server.py:1609 | log.exception | error-surfacing — debounced check (per keystroke) | Toast added (§1) |
| server.py:1996 | log.exception | error-surfacing — workspace check worker crashed | OK (notification chain already covers via `dimfort/workspaceCheckCompleted{failed:True}`) |
| server.py:2220 | log.exception | error-surfacing — workspace check failed | Toast added (§1) |
| coverage.py:482 | log.exception | error-surfacing — workspace-coverage refresh | Toast added (inline `show_message`, §1) |

## §3 `except Exception: return None` registry

| file:line | resolution |
|---|---|
| code_action.py:75 — workspace lookup fails | `log.warning` added (no toast — fires per cursor event) |
| completion.py:174 — workspace lookup fails | `log.warning` added (empty popup is indistinguishable from "no units defined" without log; no toast — fires per keystroke) |
| decl_scan.py:159 — document not open | Annotated silent-OK — disk fallback is correct path |
| decl_scan.py:173 — scan_text fails on live buffer | `log.warning` added — most insidious finding (silent stale-disk fallback; could hide parser regression for days) |
| decl_scan.py:121 — narrow OSError | Annotated silent-OK |
| panel.py:152 — unparseable buffer / closed doc | Annotated silent-OK (None render is documented contract) |
| interactions.py:202 — unparseable buffer / closed doc | Annotated silent-OK (same shape as panel.py) |
| expr_tree.py:136 — Path.resolve narrow error | Annotated silent-OK |
| tree_nav.py:454 — unit-parse fallback | Annotated silent-OK |

## §4 Daemon-thread targets

| file:line | resolution |
|---|---|
| server.py:2395 — `save_persistent_projection_cache` daemon target | Wrapped with `_cache_save_wrapped(...)` — catches & log.warnings, distinguishes "cache write failed" (best-effort) from "LSP crashed" (urgent) in the `_install_crash_trace_hook` crash-trace file |
| server.py:2406 — `save_persistent_exports_cache` daemon target | Same wrapper |

The other daemon-thread sites in the codebase
(server.py:1144, 1428, 1487, 1611, 1991) already have local
try/except inside the target function — fixes in §2/§3 cover them.

## §5 `contextlib.suppress` blocks

All 11 sites reviewed:

- `server.py:777, 784` — inlay refresh client calls; transport-disconnected guard. OK.
- `server.py:_notify:170` — same shape; OK.
- `server.py:2002, 2008, 2154, 2243, 2266, 2308, 2329` — progress-protocol guards (the LSP `$/progress` calls); clients without progress widgets drop these silently and the suppress prevents that from killing handlers. OK.
- `expr_tree.py:141` — slotted-dataclass cache stash; AttributeError/TypeError caught because the optional attribute isn't a declared field. Documented in comment. OK.

## §6 Workers marking complete on partial failure

| file:line | resolution |
|---|---|
| server.py:1263 — workspace index build silent degrade | Toast added; user now sees the cross-file-disabled degradation |
| coverage.py:481 — workspace coverage refresh silently returns empty | Toast added (§1) |

## What this audit verifies

- ✅ Every `_notify` call is classified with annotation context.
- ✅ Every user-triggered handler that catches a pipeline crash now
  surfaces a toast in addition to logging. The "silent diagnostics
  stale" failure mode is now observable.
- ✅ Every silent `except Exception: return None` in user-handler
  paths is either `log.warning`-instrumented or annotated as
  intentional silent-OK with a documented reason.
- ✅ Cache-save daemon failures no longer trip the crash-trace
  file; "best-effort write failed" is distinct from "LSP crashed".

## Carry-forward to 0.2.8

- **Shared `lsp/notify.py` helper.** `_notify` lives in `server.py`
  today; the circular-import risk (server.py imports coverage.py)
  required an inline `ls.window_show_message` workaround in
  `coverage.py:482`. A shared module would let other handler
  modules opt into toast surfacing without the dance. Small
  refactor; not blocking.
- **Unaudited core modules** — the audit spot-checked `multifile.py`
  but didn't exhaustively walk `ts_checker.py`, `multifile_cache.py`,
  `workspace_index.py`, `symbols.py`, `attach.py`, `annotations.py`,
  `cache_store.py`, `_source_io.py`. These are pipeline-internal
  (LSP handlers call into them but they don't decide what's
  user-surfaced); silence at this layer means the handler-layer
  catches handle it. Worth a focused audit if a future bug class
  proves silent-pipeline-failure-resistant.

## CI grep gate

The audit annotations enable a regression-prevention gate. Future
PRs that add a new `_notify(...)` or `contextlib.suppress(...)` in
`src/dimfort/lsp/` without an `audited(0.2.X)` annotation within
±5 lines fail the check. Anti-pattern `except` shapes — bare
`except:`, `except Exception: pass`, `except Exception: return
None` — are hard-banned in the same directory.

The gate is implemented as `scripts/silent_failure_gate.py` and
runs as a CI step in `.github/workflows/ci.yml`. Two modes:

- **Hard bans** are always enforced (work on the current tree).
- **Annotation requirements** are diff-aware: they fire only for
  NEW occurrences added against the PR's base ref, so pre-audit
  un-annotated calls don't block CI.

Scope is intentionally limited to `src/dimfort/lsp/`. Core
modules (`ts_checker`, `units`, `rewrite`, …) are 0.2.8
carry-forward — they hold legitimate silent-fallback patterns
(e.g., unit-parse failures returning `None` as a documented
contract) that pre-date the audit's classification discipline
and need their own focused review before joining the gate.

Locally:

```bash
python scripts/silent_failure_gate.py            # hard bans only
BASE_REF=origin/main python scripts/silent_failure_gate.py
```

## See also

- `docs/contributor/cache-audit-0-2-7.md` — sibling Track D Ring 2
  audit covering cache invalidation + memory churn.
- Internal release planning for the 0.2.7 cycle — the plan entry
  this audit fulfils.
- The pygls-filter observation that birthed this audit's
  "log.warning to Output vs toast to user" distinction: `log.info`
  is filtered below WARNING by pygls's default routing, so
  handler-level `log.info` doesn't reach the client. Use
  `_notify(ls, ...)` for any user-visible server message, or
  `log.warning` / `log.error` for Output-channel-visible logs.
  Canonical reference for the routing: `_notify` at
  `src/dimfort/lsp/server.py:126` (docstring spells it out).
