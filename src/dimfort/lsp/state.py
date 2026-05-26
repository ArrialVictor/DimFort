"""Shared mutable state for the LSP server: locks, caches, configuration.

The language server splits its feature handlers across modules (hover,
definition, inlay hints, …). They all reach the same mutable globals — the
last check result, the workspace index, the per-URI debounce versions, the
open-URI map — and the locks that guard them. Centralising those on a single
``state`` object gives every module one authoritative thing to import,
instead of cross-importing one another's module globals (``from … import
_last_result`` would capture a stale reference the instant a handler
reassigned it).

Concurrency contract (load-bearing — read before touching a handler):

- ``ts_handler_lock`` serialises tree-sitter tree traversal across feature
  handlers. The Python bindings call into the underlying C library, which is
  NOT thread-safe for concurrent traversal of the same tree. VSCode's
  Cmd-hover fires ``textDocument/hover`` and ``textDocument/definition``
  nearly simultaneously; pygls schedules sync handlers on a worker pool, so
  both run on different threads and can race on the same tree-sitter ``Tree``,
  producing silent native-level crashes (no Python traceback). Serialising the
  bodies of the affected handlers eliminates the race; each handler is
  sub-millisecond, so the cost is invisible to the user. Every tree-walking
  handler MUST acquire it.
- ``check_lock`` serialises every pipeline run across didOpen / didSave /
  didChange. Without it, VSCode restoring N tabs after a reload fires N
  concurrent didOpens, each spawning its own LFortran subprocesses and ASR
  JSON in memory; the pile-up exceeds macOS jetsam's budget and the LSP
  process gets SIGKILLed.
- Each mutable field below is guarded by its matching ``*_lock`` and never
  touched without it. The exception is ``project_config`` /
  ``external_modules`` / ``max_workset_size`` / ``scale_mode`` / ``cache`` /
  ``cache_mode``: these are written once inside ``server._initialize`` before
  the client is allowed to send any ``textDocument/*`` request, so the write
  happens-before every worker-thread read. Don't add code paths that read them
  before the initialize handler returns.
"""
from __future__ import annotations

import threading
from pathlib import Path

from dimfort.config import DimfortConfig
from dimfort.core.cache_store import CacheStore
from dimfort.core.multifile import WorksetResult
from dimfort.core.workspace_index import WorkspaceIndex

# Modules treated as known-external (Fortran intrinsics + common libs).
# Anything `use`d that matches this set is silently dropped from the
# dep chain rather than producing a missing-module diagnostic.
DEFAULT_EXTERNAL_MODULES: frozenset[str] = frozenset({
    # Fortran 2003+ intrinsic modules
    "iso_fortran_env", "iso_c_binding",
    "ieee_arithmetic", "ieee_exceptions", "ieee_features",
    # Common external libraries
    "mpi", "mpi_f08", "openacc", "omp_lib",
    "netcdf", "netcdf95", "ioipsl", "nrtype",
})

# Maximum number of files to feed into a single check. Resolving the
# full transitive `use` closure of a deep entry point in a large
# Fortran codebase (e.g. ~353 dependent files) holds enough AST/ASR JSON in
# memory to trigger macOS jetsam SIGKILL on the LSP process. The cap
# trades cross-file coverage for stability: when the workset exceeds
# this, we keep the last N entries in topo order — the active file
# plus its nearest deps. Override via `maxWorksetSize` in
# initializationOptions.
DEFAULT_MAX_WORKSET = 40


class _ServerState:
    """Single owner of every lock + mutable global the LSP handlers share.

    Reassign fields directly (``state.last_result = result``) — because every
    module references the one shared instance, attribute assignment is visible
    everywhere and there is no ``global`` keyword and no stale-reference
    footgun. Always hold the matching lock when touching a guarded field.
    """

    def __init__(self) -> None:
        # --- locks ---
        # Serialises pipeline runs (didOpen / didSave / didChange).
        self.check_lock = threading.Lock()
        # Guards `last_result`.
        self.last_result_lock = threading.Lock()
        # Guards `workspace_index`.
        self.workspace_index_lock = threading.Lock()
        # Serialises tree-sitter traversal across feature handlers.
        self.ts_handler_lock = threading.Lock()
        # Guards `doc_versions`.
        self.doc_versions_lock = threading.Lock()
        # Guards `opened_uris`.
        self.opened_uris_lock = threading.Lock()

        # --- mutable containers (mutated in place under their lock) ---
        # Debounce for `didChange`: per-URI monotonically increasing version.
        # A scheduled re-check compares the version under the lock before
        # running, so a burst of keystrokes only runs the last one.
        self.doc_versions: dict[str, int] = {}
        # Every file the client currently has open, keyed by resolved Path so
        # we can recover the *exact* URI the editor uses (its normalisation
        # may differ from ours — symlinks, case, percent-encoding).
        # Publishing back to the editor's URI is what makes squiggles appear.
        self.opened_uris: dict[Path, str] = {}
        # Workspace folders, captured at initialise time.
        self.workspace_folders: list[Path] = []

        # --- reassignable scalars ---
        # Last successful check result, used for hover.
        self.last_result: WorksetResult | None = None
        # Workspace module index — built once at initialize on a background
        # thread (it can take several seconds on large codebases), updated
        # incrementally on didChange / didSave. ``None`` until the initial
        # scan completes; callers fall back to whole-workspace check while
        # ``None``.
        self.workspace_index: WorkspaceIndex | None = None
        # Resolved project config (``.dimfort.toml``). Loaded once at
        # ``initialize`` time; an LSP restart is required to re-read.
        self.project_config: DimfortConfig = DimfortConfig()
        self.external_modules: frozenset[str] = DEFAULT_EXTERNAL_MODULES
        self.max_workset_size: int = DEFAULT_MAX_WORKSET
        # Opt-in multiplicative-scale checking (Phase 1; see
        # docs/design/scale.md). Off ⇒ dimension-only.
        self.scale_mode: bool = False
        # Content-hash cache (see docs/design/content-hash-cache.md). ``None``
        # means caching is disabled — the workspace check runs as it did
        # before the cache landed.
        self.cache: CacheStore | None = None
        self.cache_mode: str = "off"


state = _ServerState()
