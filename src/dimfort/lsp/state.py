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
from dimfort.core.multifile_cache import (
    ModuleExportsCache,
    ProjectionCache,
    TreeCache,
)
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

    The class is instantiated exactly once at import time; the resulting
    ``state`` module-level object is the only handle every LSP feature
    module imports.

    Attributes:
        check_lock: Serialises pipeline runs across ``didOpen`` /
            ``didSave`` / ``didChange`` so a tab-restore burst can't fork
            N concurrent LFortran subprocesses and trip macOS jetsam.
        last_result_lock: Guards reads and writes of :attr:`last_result`.
        workspace_index_lock: Guards reads and writes of
            :attr:`workspace_index`.
        ts_handler_lock: Serialises tree-sitter tree traversal across
            feature handlers (hover, definition, inlay, …). The Python
            bindings call into a C library that is not thread-safe for
            concurrent traversal of the same ``Tree``; without this lock
            two near-simultaneous Cmd-hover requests can crash the
            native layer with no Python traceback. Every tree-walking
            handler MUST hold this lock.
        doc_versions_lock: Guards :attr:`doc_versions`.
        opened_uris_lock: Guards :attr:`opened_uris`.
        doc_versions: Per-URI monotonically increasing version counter
            used to debounce ``didChange``. A scheduled re-check
            compares the version under the lock before running, so a
            burst of keystrokes only re-runs the last one.
        opened_uris: Every file the client currently has open, keyed by
            resolved :class:`~pathlib.Path` so we can recover the exact
            URI the editor uses (its normalisation may differ from
            ours — symlinks, case, percent-encoding). Publishing back to
            the editor's URI is what makes squiggles appear.
        workspace_folders: Workspace folder roots captured at
            ``initialize`` time.
        last_result: Last successful workset check result, consulted by
            every read-side feature (hover, panel, inlay). ``None``
            until the first check completes.
        workspace_index: Module index built once at ``initialize`` on a
            background thread (it can take several seconds on large
            codebases) and updated incrementally on ``didChange`` /
            ``didSave``. ``None`` until the initial scan completes;
            callers fall back to a whole-workspace check while ``None``.
        project_config: Resolved ``dimfort.toml`` configuration.
            Loaded once at ``initialize`` time; an LSP restart is
            required to re-read.
        external_modules: Lower-cased set of modules the workspace
            treats as known-external (silently dropped from the
            dep-chain rather than producing a missing-module
            diagnostic). Initialised from
            :data:`DEFAULT_EXTERNAL_MODULES`.
        max_workset_size: Cap on how many files a single check may
            include (defaults to :data:`DEFAULT_MAX_WORKSET`). Overrides
            come from ``maxWorksetSize`` in ``initializationOptions``.
        scale_mode: Opt-in scale checking. When ``True``, S001
            (multiplicative) and S002 (affine) fire; when ``False``,
            dimension-only checking is performed. See
            ``docs/design/scale.md``.
        cache: Content-hash cache used to skip re-checking files whose
            text hasn't changed. ``None`` means caching is disabled and
            the workspace check runs as it did before the cache landed.
            See ``docs/design/content-hash-cache.md``.
        cache_mode: One of ``"off"`` / ``"read-only"`` /
            ``"read-write"`` matching the CLI flag, surfaced for
            diagnostics and tests.
        tree_cache: Session-scoped tree-sitter parse cache. Threaded
            through every internal ``check_files`` call so the load
            phase collapses on unchanged files. See
            ``docs/design/future/multifile-cache.md``.
        exports_cache: Session-scoped module-exports + signatures
            cache. Pairs with :attr:`tree_cache`; same invalidation
            model.

    Note:
        :attr:`project_config`, :attr:`external_modules`,
        :attr:`max_workset_size`, :attr:`scale_mode`, :attr:`cache`,
        and :attr:`cache_mode` are written once inside
        ``server._initialize`` before any ``textDocument/*`` request can
        arrive, so the write happens-before every worker-thread read
        and needs no lock. Do not add code paths that read them before
        ``initialize`` returns.
    """

    def __init__(self) -> None:
        """Initialise every lock, mutable container, and reassignable scalar.

        Called exactly once at import time. All fields start in their
        empty / disabled / ``None`` state; the LSP ``initialize``
        handler fills in :attr:`project_config`, :attr:`workspace_index`,
        and the cache fields before the first ``textDocument/*`` request
        is allowed to run.
        """
        # --- locks ---
        # Serialises pipeline runs (didOpen / didSave / didChange).
        self.check_lock = threading.Lock()
        # Guards `last_result`.
        self.last_result_lock = threading.Lock()
        # Guards the workspace-check single-in-flight flag below. Held
        # only across the flag check + set; the actual workspace check
        # runs on a daemon thread with this lock released.
        self.workspace_check_lock = threading.Lock()
        # True while a daemon-thread workspace check is running. Used
        # to coalesce duplicate ``dimfort/checkWorkspace`` triggers
        # (typing on the command twice, panel auto-refresh racing the
        # user's manual press, etc.). The active thread clears it
        # in its ``finally``.
        self.workspace_check_in_progress = False
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
        # Resolved project config (``dimfort.toml``). Loaded once at
        # ``initialize`` time; an LSP restart is required to re-read.
        self.project_config: DimfortConfig = DimfortConfig()
        self.external_modules: frozenset[str] = DEFAULT_EXTERNAL_MODULES
        self.max_workset_size: int = DEFAULT_MAX_WORKSET
        # Opt-in scale checking (see docs/design/scale.md). When on,
        # S001 (multiplicative) and S002 (affine) fire. Off ⇒
        # dimension-only.
        self.scale_mode: bool = False
        # Content-hash cache (see docs/design/content-hash-cache.md). ``None``
        # means caching is disabled — the workspace check runs as it did
        # before the cache landed.
        self.cache: CacheStore | None = None
        self.cache_mode: str = "off"
        # In-memory tree + exports caches (see
        # docs/design/future/multifile-cache.md). Instantiated eagerly:
        # both are cheap empty dicts behind a Lock; the LSP routes every
        # internal check_files call through them so the load + index
        # phases collapse on unchanged files. Lifetime = LSP session;
        # not persisted. Either field is ``None`` when the matching
        # ``dimfort lsp --no-{tree,exports}-cache`` flag is set.
        self.tree_cache: TreeCache | None = TreeCache()
        self.exports_cache: ModuleExportsCache | None = ModuleExportsCache()
        # Per-file scan + attach output cache (M1). Skips both walks on
        # a content-hash + patterns-fingerprint hit. Lifetime = LSP
        # session.
        self.projection_cache: ProjectionCache | None = ProjectionCache()

        # Highest ``len(result.trees)`` observed since server start.
        # Drives the adaptive cache cap (``max(observed × 4, 4096)``);
        # see ``server._apply_cache_max_entries``. Sticky high-water
        # mark — never shrinks — so opening a single-file scratch after
        # a 2435-file workspace doesn't trigger eviction inside one check.
        self.observed_max_workset_size = 0


state = _ServerState()
