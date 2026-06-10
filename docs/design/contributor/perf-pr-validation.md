# Perf-PR validation checklist

Manual checks to run before merging a PR that changes wall-clock
behavior. Companion-side UI features have their own visual checklist
([MANUAL_QA.md in each companion repo](../../../README.md)) — this
doc covers the server-internal axes: parser, cache layer, workspace
check pipeline, anything where the test suite tells you the code is
correct but not that it's faster *or* still correct against a real
project.

When to run this:

- Cache layer changes (in-memory or disk-persisted)
- New parse-time work or removed parse-time work
- Anything claiming a wall-clock win against a benchmark
- Anything touching ``check_files`` phases A/B/C/D
- Any refactor of the LSP server's check-completion path

Skip this when the PR is pure-docstring, pure-formatting, a typed
test change, or a companion-only UI tweak.

## The three checks

### 1. Codec / state correctness

Unit tests for the data structures the change adds or modifies. The
bar is round-trip cleanliness on every documented data shape — not
just the easy cases. If you serialise a unit type, the test list
must include affine offsets (``degC``), polymorphic tyvars (``'a``),
log/exp wrappers, prefactors, and empty-but-present collections.
Missing one is how a codec ships a silent data-loss bug.

### 2. Bench against a real workset

The synthetic test suite times microseconds. Real workspaces live at
2000+ files with deep ``use``-chains. Use
``scripts/bench_multifile_cache.py`` against a representative
checkout — a real-world Fortran codebase from your ``sources/``
clone, not the demo fixtures.

For a cache-layer PR, run twice: with the change and with the new
behavior disabled (add a ``--no-X`` flag in the bench harness if the
PR doesn't already expose one). The "with" minus "without" delta is
the ship number. Plan docs state a threshold for each experiment;
hit it or revert.

### 3. Diagnostics-don't-drift against a real workset

This is the check that catches the silent codec bug the unit tests
missed. Procedure:

1. **First session — populate any disk caches the PR touches.**
   - Install the dev server (``uv pip install -e .`` from the
     ``DimFort/`` checkout).
   - Open the workspace in your usual editor.
   - Run ``DimFort: Refresh Workspace Coverage``.
   - **Record** the H-diag count, U-diag count, and (optionally) the
     full ``.dimfort/`` cache file listing.

2. **Restart the server / editor.**
   - Quit cleanly so any save-on-completion thread finishes.
   - The on-disk cache files survive; the in-memory state does not.

3. **Second session — verify.**
   - Open the same workspace, run the same refresh.
   - Counts **must match exactly**. Drift = the cache is producing a
     different result than a from-scratch run = bug. Revert.

4. **Paranoia pass (optional, recommended for codec PRs).**
   - Delete the disk cache file the PR touches.
   - Restart.
   - Refresh.
   - Counts should still match — confirms the cache hit-or-miss path
     produces the same observable behavior.

## Documenting the result

Paste the bench table into the PR body. Include the threshold from
the plan doc and whether the result hit it. Note whether step 3
above produced matching counts; if not, link the diagnostic that
shifted and revert before merge.

## Example: M5 disk-persistent ModuleExportsCache (PR #80)

The plan threshold was "≥ 2 s saved on cold-after-restart engine
time, codec round-trips clean." The bench produced:

| Phase                | no-M5    | with M5  | delta   |
|----------------------|----------|----------|---------|
| index (post-restart) | 2.76 s   |  192 ms  | −2.57 s |
| engine (post-restart)| 16.71 s  | 10.73 s  | −5.98 s |
| user-wall (post-rs)  | 21.26 s  | 15.77 s  | −5.49 s |

5.5 s clears the threshold by a wide margin. Codec round-trip tests
covered affine offset, polymorphic ``'a``, nested
``LogWrap`` / ``ExpWrap``, prefactors, symbolic-term Exponent, and
empty ``ModuleExports``. Diagnostics-don't-drift confirmed against
the workspace used for the bench.
