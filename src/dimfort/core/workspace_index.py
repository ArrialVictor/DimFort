"""Workspace-aware Fortran module discovery.

Scans a source tree for ``module M`` declarations and ``use`` references,
building an index that maps module names to the file declaring them.
Used by :func:`check_files` to auto-expand a workset from a few entry
points, and by the LSP to discover cross-file dependencies.

The scanner is regex + string-literal-aware comment stripping — no
LFortran invocation. It's intentionally narrow: we only extract the
information needed for compile-order resolution. Anything semantic
(types, signatures, expressions) still flows through LFortran's
AST/ASR.

External / unresolvable modules:

- ``external_modules`` passed to :func:`resolve_workset` is the user's
  allowlist of modules that exist outside the source tree (e.g.
  ``ioipsl``, ``netcdf``, ``mpi``). These are silently dropped from
  the dep chain — no diagnostic.
- Anything else not in the index becomes a ``Resolution.unresolved``
  entry. Callers (CLI / LSP) decide how to surface that.
"""
from __future__ import annotations

import multiprocessing
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from dimfort.core.annotations import _comment_start

# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------


# `module NAME` declaration. Excludes `module procedure` interface bodies.
_MODULE_DECL_RE = re.compile(
    r"^\s*module\s+(?!procedure\b)([A-Za-z_]\w*)",
    re.IGNORECASE,
)

# `end module` / `end module NAME` — needed to track when a top-level
# SUBROUTINE/FUNCTION declaration falls outside any MODULE block.
_END_MODULE_RE = re.compile(r"^\s*end\s*module\b", re.IGNORECASE)

# `SUBROUTINE foo` / `[type, attrs] FUNCTION foo([args]) [RESULT(x)]`.
# Match the *name* only — we don't care about args / return-type spec at
# this stage. ``type`` includes `real`, `integer(kind=...)`, `type(T)`,
# `class(T)`, `pure`, `elemental`, `recursive`, `module`. We tolerate
# any sequence of those prefixes by allowing a permissive run of word
# / paren / equals / star / comma tokens before the keyword.
_PROCEDURE_DECL_RE = re.compile(
    r"""^\s*
        (?!end\b)                # reject ``end subroutine NAME`` / ``end function``
        (?:[\w()=*,\s]*?\s+)?    # optional type/attr prefix for functions
        (?:subroutine|function)
        \s+
        ([A-Za-z_]\w*)           # the name we want
        \s*
        (?:\(|$|!|\&)            # followed by '(', EOL, comment, or continuation
    """,
    re.IGNORECASE | re.VERBOSE,
)

# `use NAME` reference. Captures the module name and the trailing tail
# (after the module name) so a second pass can extract `only:` lists
# and renames. Handles:
#   use M
#   use, intrinsic :: M
#   use, non_intrinsic :: M
#   use :: M
#   use M, only: a, b => c
_USE_RE = re.compile(
    r"""^\s*
        use
        (?:\s*,\s*(?:intrinsic|non_intrinsic))?
        \s*(?:::\s*)?
        ([A-Za-z_]\w*)
        \s*
        (?:,\s*only\s*:\s*(.+?))?
        \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Inside an `only:` list, split on top-level commas. Each piece is
# either `NAME` (plain import) or `LOCAL => REMOTE` (renamed import).
_RENAME_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*=>\s*([A-Za-z_]\w*)\s*$")
_PLAIN_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*$")

# ``CALL name`` invocation. Captures the callee name only. Permits a
# leading label (numeric F77-style line label, very rare in F90 code
# but tolerated). We deliberately don't try to match function-call
# expressions here — they're harder to disambiguate from array
# indexing without semantic context, and the dominant external-
# procedure pattern in F77-vintage codebases is ``CALL`` to a
# subroutine. Functions can be added later if needed.
_CALL_RE = re.compile(
    r"^\s*(?:\d+\s+)?call\s+([A-Za-z_]\w*)",
    re.IGNORECASE,
)


# Default file extensions worth scanning. Fixed-form (`.f`, `.for`) is
# excluded — DimFort doesn't support it (see PROJECT_LOG decision).
DEFAULT_INCLUDE_SUFFIXES: frozenset[str] = frozenset({
    ".f90", ".F90", ".f95", ".F95", ".f03", ".F03", ".f08", ".F08",
})


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UseRef:
    """One ``use`` statement found in a source file.

    Attributes:
        module: Lower-cased, normalised module name being imported.
        only: Lower-cased names from a ``only:`` list, or ``None`` when
            the whole module is imported.
        renames: ``(local_name, original_name)`` pairs (both lower-cased)
            extracted from ``LOCAL => REMOTE`` clauses inside ``only:``.
    """

    module: str                       # lower-cased, normalised
    only: tuple[str, ...] | None      # ``None`` = whole module imported
    renames: tuple[tuple[str, str], ...]  # (local_name, original_name) pairs


@dataclass
class WorkspaceIndex:
    """Module-to-file map plus per-file ``use`` lists.

    **Bound:** O(workspace files). Grows on first scan; per-file
    edits replace entries via :func:`update_index`.

    **Invalidation:**

    - didChange / didSave: :func:`update_index` re-scans the changed
      file and replaces its entries.
    - Cold start: :func:`scan_workspace` (with optional ``prior_index``
      validated against on-disk file hashes) rebuilds the index.

    **didClose:** entries persist (the index is a workspace property,
    not an open-buffer property). External file deletions are NOT
    pruned today — see the ``workspace/didChangeWatchedFiles``
    follow-up task in ``0_2_6_PLAN.md``.

    Procedures contained inside a module are deliberately excluded from
    ``procedures`` — those are reached through the module's exports
    already. Top-level (file-scope) procedures, by contrast, mirror the
    F77-vintage external-linkage pattern: codebases ``CALL`` them
    without a ``USE`` clause, so without this index the LSP's per-file
    workset can't reach their defining files via ``use``-chain
    resolution.

    Attributes:
        modules: Lower-cased module name to the file declaring it.
        procedures: Lower-cased name of every top-level
            ``SUBROUTINE`` / ``FUNCTION`` declaration (one that is not
            inside a ``MODULE`` block) to the file that declares it.
        uses_by_file: Per-file ordered tuple of every ``use`` statement
            seen in that file.
        calls_by_file: Per-file ordered tuple of lower-cased ``CALL``
            callee names. The workset resolver consults this after the
            ``use``-chain expansion to also pull in files that define
            externally-linked procedures the active file calls.
        scan_failures: Per-file read/scan error message; entries
            present here are excluded from the rest of the index.
        file_hashes: SHA-256 hex of each scanned file's source bytes
            at the time it was indexed. Used by
            :func:`load_persistent_index` to validate cached entries
            against the on-disk file before reusing them on a fresh
            LSP session.
    """

    modules: dict[str, Path] = field(default_factory=dict)
    procedures: dict[str, Path] = field(default_factory=dict)
    uses_by_file: dict[Path, tuple[UseRef, ...]] = field(default_factory=dict)
    calls_by_file: dict[Path, tuple[str, ...]] = field(default_factory=dict)
    scan_failures: dict[Path, str] = field(default_factory=dict)
    file_hashes: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a workset from an entry-point set.

    Attributes:
        compile_order: Files in topological order (each file appears
            after every file it depends on).
        unresolved: ``(consumer_file, missing_module)`` pairs for every
            ``use`` clause whose target module isn't in the index and
            isn't in the external allowlist.
        external: Lower-cased names of every allowlist module that was
            actually referenced by the workset.
    """

    compile_order: tuple[Path, ...]
    unresolved: tuple[tuple[Path, str], ...]  # (consumer_file, missing_module)
    external: frozenset[str]                  # module names that matched the allowlist


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Return ``line`` with any trailing comment removed (string-aware).

    Args:
        line: A single line of source text.

    Returns:
        The same line, truncated before the first comment-introducing
        ``!`` that sits outside a string literal. Unchanged when no
        comment is present.
    """
    col = _comment_start(line)
    return line if col is None else line[:col]


def _prepare_stripped_lines(text: str) -> tuple[str, ...]:
    """Split + strip comments once, share across all four extractors.

    Originally each extractor (``extract_modules``,
    ``extract_top_level_procedures``, ``extract_uses``,
    ``extract_calls``) re-did the splitlines + ``_strip_comment`` pass
    independently. Profiling showed ``_strip_comment`` /
    ``_comment_start`` together cost ~75% of the scan_workspace
    runtime on a 2435-file workset (4 M calls). One shared pass
    cuts that by 3/4.
    """
    return tuple(_strip_comment(line) for line in text.splitlines())


def _parse_only_list(tail: str) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Split an ``only:`` tail into ``(plain_names, renames)``.

    Args:
        tail: Text following ``only:`` (without the ``only:`` keyword
            itself), e.g. ``"a, b => c, d"``.

    Returns:
        Pair ``(plain, renames)`` where ``plain`` lists every imported
        local name (lower-cased) in source order — including the local
        side of rename clauses — and ``renames`` lists
        ``(local, original)`` pairs (both lower-cased) for the renamed
        entries only.
    """
    plain: list[str] = []
    renames: list[tuple[str, str]] = []
    for piece in tail.split(","):
        piece = piece.strip()
        if not piece:
            continue
        m = _RENAME_RE.match(piece)
        if m:
            local, original = m.group(1), m.group(2)
            renames.append((local.lower(), original.lower()))
            plain.append(local.lower())
            continue
        m = _PLAIN_RE.match(piece)
        if m:
            plain.append(m.group(1).lower())
    return tuple(plain), tuple(renames)


def extract_calls(
    text: str, *, stripped_lines: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return every ``CALL name`` callee in ``text``, lower-cased.

    Order-preserving but de-duplicated: the same name called twice
    appears once. Comments are stripped before matching, so a ``CALL``
    inside a string-aware comment is ignored.

    Args:
        text: Full source text of a Fortran file.
        stripped_lines: Optional pre-computed comment-stripped lines
            (from :func:`_prepare_stripped_lines`). When supplied, the
            internal splitlines + strip pass is skipped.

    Returns:
        Tuple of lower-cased callee names in first-seen order.
    """
    lines = (
        stripped_lines if stripped_lines is not None
        else _prepare_stripped_lines(text)
    )
    seen: set[str] = set()
    out: list[str] = []
    for code in lines:
        m = _CALL_RE.match(code)
        if m:
            name = m.group(1).lower()
            if name not in seen:
                seen.add(name)
                out.append(name)
    return tuple(out)


def extract_top_level_procedures(
    text: str, *, stripped_lines: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return the names of every top-level SUBROUTINE/FUNCTION in ``text``.

    "Top-level" = not inside any ``MODULE`` block. ``MODULE`` /
    ``END MODULE`` pairs are tracked to discriminate. Nested procedures
    (the F2008 contained-procedure feature) are NOT captured — only
    ones whose declaration appears at file scope.

    Args:
        text: Full source text of a Fortran file.
        stripped_lines: Optional pre-computed comment-stripped lines
            (from :func:`_prepare_stripped_lines`). When supplied, the
            internal splitlines + strip pass is skipped.

    Returns:
        Lower-cased procedure names in source order.
    """
    lines = (
        stripped_lines if stripped_lines is not None
        else _prepare_stripped_lines(text)
    )
    names: list[str] = []
    module_depth = 0
    for code in lines:
        # MODULE ... — enter a module scope. Excludes the
        # ``module procedure`` interface form (handled by the
        # ``(?!procedure)`` lookahead in ``_MODULE_DECL_RE``).
        if _MODULE_DECL_RE.match(code):
            module_depth += 1
            continue
        if _END_MODULE_RE.match(code):
            if module_depth > 0:
                module_depth -= 1
            continue
        if module_depth > 0:
            # Inside a module — anything matching the procedure
            # regex here is a *contained* procedure and isn't a
            # top-level external. Skip.
            continue
        m = _PROCEDURE_DECL_RE.match(code)
        if m:
            names.append(m.group(1).lower())
    return tuple(names)


def extract_modules(
    text: str, *, stripped_lines: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """Return every ``module NAME`` declaration in ``text`` (lower-cased).

    Args:
        text: Full source text of a Fortran file.
        stripped_lines: Optional pre-computed comment-stripped lines
            (from :func:`_prepare_stripped_lines`). When supplied, the
            internal splitlines + strip pass is skipped.

    Returns:
        Lower-cased module names in source order.
    """
    lines = (
        stripped_lines if stripped_lines is not None
        else _prepare_stripped_lines(text)
    )
    names: list[str] = []
    for code in lines:
        m = _MODULE_DECL_RE.match(code)
        if m:
            names.append(m.group(1).lower())
    return tuple(names)


def extract_uses(
    text: str, *, stripped_lines: tuple[str, ...] | None = None,
) -> tuple[UseRef, ...]:
    """Return every ``use`` statement in ``text``.

    Multi-line ``use`` statements continued with ``&`` are joined
    before matching so the ``only:`` list isn't truncated.

    Args:
        text: Full source text of a Fortran file.
        stripped_lines: Optional pre-computed comment-stripped lines
            (from :func:`_prepare_stripped_lines`). When supplied, the
            internal splitlines + strip pass is skipped.

    Returns:
        Tuple of :class:`UseRef` records in source order.
    """
    uses: list[UseRef] = []
    lines = (
        stripped_lines if stripped_lines is not None
        else _prepare_stripped_lines(text)
    )
    i = 0
    while i < len(lines):
        code = lines[i].rstrip()
        # Join continuation lines if present so the only-list isn't split.
        while code.endswith("&") and i + 1 < len(lines):
            code = code[:-1].rstrip()
            i += 1
            cont = lines[i].strip()
            if cont.startswith("&"):
                cont = cont[1:].lstrip()
            code = f"{code} {cont}".rstrip()
        m = _USE_RE.match(code)
        if m:
            name = m.group(1).lower()
            only_tail = m.group(2)
            if only_tail is None:
                uses.append(UseRef(module=name, only=None, renames=()))
            else:
                plain, renames = _parse_only_list(only_tail)
                uses.append(UseRef(module=name, only=plain, renames=renames))
        i += 1
    return tuple(uses)


# ---------------------------------------------------------------------------
# Filesystem walk
# ---------------------------------------------------------------------------


def _iter_fortran_files(
    roots: Iterable[Path],
    include_suffixes: frozenset[str],
    exclude_patterns: tuple[str, ...],
) -> Iterator[Path]:
    """Yield every Fortran source file under ``roots`` (recursive, sorted).

    ``Path.rglob`` returns entries in filesystem order, which differs
    between macOS, Linux, and CI runners. First-wins ``setdefault`` on
    duplicate procedure / module names — and ``_topo_sort`` ordering of
    the workset — both inherit that instability. Sorting at the source
    pins the workspace's effective composition across OSes.

    Args:
        roots: Directories (recursed into) or individual files.
        include_suffixes: File-extension allowlist (case-sensitive).
        exclude_patterns: Globs filtered out of the walk; see
            :func:`_excluded` for matching semantics.

    Yields:
        Resolved paths to every surviving Fortran source file, in
        sorted order.
    """
    for root in roots:
        root = Path(root).resolve()
        if root.is_file():
            if root.suffix in include_suffixes and not _excluded(root, exclude_patterns):
                yield root
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix not in include_suffixes:
                continue
            if _excluded(p, exclude_patterns):
                continue
            yield p


def _excluded(path: Path, patterns: tuple[str, ...]) -> bool:
    """Return ``True`` if ``path`` matches any glob in ``patterns``.

    Args:
        path: Candidate path.
        patterns: Glob patterns; matched both by :meth:`Path.match`
            (right-anchored) and by :func:`_glob_substring_match`
            (anywhere along the path).

    Returns:
        ``True`` when at least one pattern matches; ``False`` for an
        empty pattern tuple.
    """
    if not patterns:
        return False
    s = str(path)
    return any(
        path.match(pat) or _glob_substring_match(s, pat) for pat in patterns
    )


def _glob_substring_match(s: str, pattern: str) -> bool:
    """Match ``pattern`` against any path-suffix substring of ``s``.

    :meth:`Path.match` requires the pattern to align with the
    right-hand part of the path; this fallback also matches a fragment
    anywhere along the path, so patterns like ``build/**`` catch a
    ``build`` directory at any depth.

    Args:
        s: Stringified candidate path.
        pattern: Glob pattern (fnmatch syntax).

    Returns:
        ``True`` if any contiguous tail of ``s``'s path components
        matches ``pattern``.
    """
    from fnmatch import fnmatch
    parts = Path(s).parts
    for i in range(len(parts)):
        sub = "/".join(parts[i:])
        if fnmatch(sub, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Index build + update
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FileScanResult:
    """Per-file scan output produced by the parallel scan workers.

    Holds the four extracted artefacts in immutable form so the main
    thread can merge them into the workspace index in input order
    (preserving the deterministic "first-found wins" contract for
    duplicate module / procedure names).

    ``error`` is populated when the file couldn't be read; the other
    fields are empty in that case.
    """

    path: Path
    error: str | None
    modules: tuple[str, ...]
    procedures: tuple[str, ...]
    uses: tuple[UseRef, ...]
    calls: tuple[str, ...]
    # SHA-256 hex of the source bytes scanned. Used by the persistent
    # index cache to validate whether a stored entry is still current.
    # Empty when the read failed.
    content_hash: str = ""


def _scan_one_file(
    path: Path, new_text: str | None = None,
) -> _FileScanResult:
    """Read and scan one file. Pure: no shared state mutation."""
    import hashlib

    from dimfort.core._source_io import read_text
    try:
        text = new_text if new_text is not None else read_text(path)
    except OSError as exc:
        return _FileScanResult(path, str(exc), (), (), (), ())
    # Strip comments once and feed the shared line tuple to all four
    # extractors; saves ~75% of the scan's CPU cost (profile showed
    # _strip_comment + _comment_start dominating).
    stripped = _prepare_stripped_lines(text)
    return _FileScanResult(
        path=path,
        error=None,
        modules=tuple(extract_modules(text, stripped_lines=stripped)),
        procedures=tuple(extract_top_level_procedures(text, stripped_lines=stripped)),
        uses=extract_uses(text, stripped_lines=stripped),
        calls=extract_calls(text, stripped_lines=stripped),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def _merge_scan_result(index: WorkspaceIndex, result: _FileScanResult) -> None:
    """Apply one ``_FileScanResult`` to ``index`` (sequential merge step)."""
    if result.error is not None:
        index.scan_failures[result.path] = result.error
        return
    for module_name in result.modules:
        # First-found wins on duplicates; the parallel scan preserves
        # this contract by merging in input (file-list) order on the
        # main thread.
        index.modules.setdefault(module_name, result.path)
    for proc_name in result.procedures:
        index.procedures.setdefault(proc_name, result.path)
    index.uses_by_file[result.path] = result.uses
    index.calls_by_file[result.path] = result.calls
    if result.content_hash:
        index.file_hashes[result.path] = result.content_hash


def scan_workspace(
    roots: Iterable[Path],
    *,
    include_suffixes: frozenset[str] = DEFAULT_INCLUDE_SUFFIXES,
    exclude_patterns: tuple[str, ...] = (),
    progress_cb: Callable[[int, int, Path], None] | None = None,
    max_workers: int | None = None,
    prior_index: WorkspaceIndex | None = None,
) -> WorkspaceIndex:
    """Walk every root, scan each Fortran source for module/use headers.

    Per-file scans run in parallel via a thread pool (the regex
    scanners release the GIL during ``read_text``'s I/O; the regex
    work itself is short enough that pool overhead is the dominant
    factor for tiny worksets). The merge step is sequential and
    walks results in input-file order so the "first-found wins"
    contract for duplicate module / procedure names is independent
    of thread scheduling.

    Args:
        roots: Directories (recursed into) or individual files.
        include_suffixes: File-extension allowlist (case-sensitive).
        exclude_patterns: Glob patterns filtered out of the walk.
        progress_cb: Optional callback invoked after each file is
            scanned as ``progress_cb(scanned, total, path)``. The
            ``path`` and ``scanned`` index reflect *completion* order
            (a path completes before its index slot does), not the
            input order — fine for a progress bar that only displays
            the count.
        max_workers: Override for the worker pool size; default is
            one less than the CPU count.
        prior_index: Optional previously-built :class:`WorkspaceIndex`
            (typically loaded from disk via
            :func:`load_persistent_index`). Files whose current
            ``content_hash`` matches the stored value skip the scan
            entirely; their stored entries are reused. Files
            missing from ``prior_index`` or whose hash differs run
            a fresh scan.

    Returns:
        A populated :class:`WorkspaceIndex` covering every scanned
        file.
    """
    import hashlib

    from dimfort.core._source_io import read_text

    files = list(_iter_fortran_files(roots, include_suffixes, exclude_patterns))
    index = WorkspaceIndex()
    if not files:
        return index
    total = len(files)
    workers = (
        max_workers
        if max_workers is not None
        else max(1, (multiprocessing.cpu_count() or 4) - 1)
    )

    # Partition files: reuse_paths get their entries copied from
    # prior_index; scan_paths run through the parallel worker pool.
    reuse_paths: list[Path] = []
    scan_paths: list[Path] = []
    if prior_index is not None and prior_index.file_hashes:
        for path in files:
            stored_hash = prior_index.file_hashes.get(path)
            if stored_hash is None:
                scan_paths.append(path)
                continue
            try:
                current_hash = hashlib.sha256(
                    read_text(path).encode("utf-8"),
                ).hexdigest()
            except OSError:
                # Unreadable now; let the scan path record the failure.
                scan_paths.append(path)
                continue
            if current_hash == stored_hash:
                reuse_paths.append(path)
            else:
                scan_paths.append(path)
    else:
        scan_paths = list(files)

    # Reuse: copy entries from prior_index. No scan work needed.
    for p in reuse_paths:
        if p in prior_index.uses_by_file:  # type: ignore[union-attr]
            index.uses_by_file[p] = prior_index.uses_by_file[p]  # type: ignore[union-attr]
        if p in prior_index.calls_by_file:  # type: ignore[union-attr]
            index.calls_by_file[p] = prior_index.calls_by_file[p]  # type: ignore[union-attr]
        index.file_hashes[p] = prior_index.file_hashes[p]  # type: ignore[union-attr]
    # Reuse module + procedure first-found-wins entries in input order,
    # but only when the owning file is in reuse_paths (so a re-scanned
    # file's exports don't get pre-empted by stale entries).
    if prior_index is not None:
        reuse_set = set(reuse_paths)
        for path in files:
            if path not in reuse_set:
                continue
            for name, owner in prior_index.modules.items():
                if owner == path:
                    index.modules.setdefault(name, path)
            for name, owner in prior_index.procedures.items():
                if owner == path:
                    index.procedures.setdefault(name, path)

    # Scan: run the worker pool over scan_paths only.
    if not scan_paths:
        return index
    scan_slots: list[_FileScanResult | None] = [None] * len(scan_paths)
    progress_lock = threading.Lock()
    progress_counter = [len(reuse_paths)]  # reused files count toward progress

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_scan_one_file, path): i
            for i, path in enumerate(scan_paths)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            scan_slots[i] = fut.result()
            if progress_cb is not None:
                with progress_lock:
                    progress_counter[0] += 1
                    n = progress_counter[0]
                progress_cb(n, total, scan_paths[i])

    # Sequential merge in input order. Cheap (dict ops on already-
    # scanned data) and what makes the parallel scan deterministic.
    for slot in scan_slots:
        if slot is not None:
            _merge_scan_result(index, slot)
    return index


def update_index(
    index: WorkspaceIndex,
    changed: Path,
    *,
    new_text: str | None = None,
) -> WorkspaceIndex:
    """Re-scan a single file. Mutates and returns ``index``.

    Args:
        index: Workspace index to update in place.
        changed: Path of the file that changed.
        new_text: Optional source override; lets the LSP pass in
            unsaved buffer contents instead of reading from disk.

    Returns:
        The same ``index`` object, with all previous entries for
        ``changed`` dropped and replaced by a fresh scan.
    """
    changed = Path(changed).resolve()
    # Drop previous entries for this file.
    for name, owner in list(index.modules.items()):
        if owner == changed:
            del index.modules[name]
    for name, owner in list(index.procedures.items()):
        if owner == changed:
            del index.procedures[name]
    index.uses_by_file.pop(changed, None)
    index.calls_by_file.pop(changed, None)
    index.scan_failures.pop(changed, None)
    index.file_hashes.pop(changed, None)
    _scan_into_index(index, changed, new_text=new_text)
    return index


# ---------------------------------------------------------------------------
# Disk persistence
# ---------------------------------------------------------------------------

# Bump when the on-disk JSON schema changes in a way that can't be
# back-compat read. A version mismatch causes ``load_persistent_index``
# to drop the cache silently and trigger a full rescan.
_INDEX_SCHEMA_VERSION = 1


def save_persistent_index(index: WorkspaceIndex, cache_root: Path) -> None:
    """Serialise ``index`` to ``<cache_root>/workspace-index.json``.

    Best-effort: any OSError (read-only FS, parent missing, etc.) is
    swallowed so a workspace scan never aborts because the cache
    couldn't be written.

    Args:
        index: The workspace index to persist.
        cache_root: Directory the on-disk cache lives under
            (typically ``.dimfort-cache``).
    """
    import json

    payload = {
        "schema_version": _INDEX_SCHEMA_VERSION,
        "modules": {n: str(p) for n, p in index.modules.items()},
        "procedures": {n: str(p) for n, p in index.procedures.items()},
        "uses_by_file": {
            str(p): [_dump_useref(u) for u in uses]
            for p, uses in index.uses_by_file.items()
        },
        "calls_by_file": {
            str(p): list(calls) for p, calls in index.calls_by_file.items()
        },
        "scan_failures": {str(p): err for p, err in index.scan_failures.items()},
        "file_hashes": {str(p): h for p, h in index.file_hashes.items()},
    }
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        out_path = cache_root / "workspace-index.json"
        # Write to a sibling tempfile and rename for atomicity; partial
        # writes on crash never produce a corrupted file the next
        # session would try to parse.
        tmp = out_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        tmp.replace(out_path)
    except OSError:
        return


def load_persistent_index(cache_root: Path) -> WorkspaceIndex | None:
    """Load + parse ``<cache_root>/workspace-index.json`` if present.

    Returns ``None`` on any failure (missing file, version mismatch,
    JSON parse error, OSError). Caller treats ``None`` as "no prior
    index" and runs a full scan.

    Args:
        cache_root: Directory holding the on-disk cache.

    Returns:
        A populated :class:`WorkspaceIndex` whose entries the caller
        can pass as ``prior_index=`` to :func:`scan_workspace` for
        hash-validated reuse.
    """
    import json

    in_path = cache_root / "workspace-index.json"
    try:
        text = in_path.read_text()
    except OSError:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != _INDEX_SCHEMA_VERSION:
        return None

    index = WorkspaceIndex()
    try:
        index.modules = {
            n: Path(p) for n, p in payload.get("modules", {}).items()
        }
        index.procedures = {
            n: Path(p) for n, p in payload.get("procedures", {}).items()
        }
        index.uses_by_file = {
            Path(p): tuple(_load_useref(u) for u in uses)
            for p, uses in payload.get("uses_by_file", {}).items()
        }
        index.calls_by_file = {
            Path(p): tuple(calls)
            for p, calls in payload.get("calls_by_file", {}).items()
        }
        index.scan_failures = {
            Path(p): err for p, err in payload.get("scan_failures", {}).items()
        }
        index.file_hashes = {
            Path(p): h for p, h in payload.get("file_hashes", {}).items()
        }
    except (TypeError, ValueError, KeyError):
        return None
    return index


def _dump_useref(u: UseRef) -> dict[str, object]:
    """Serialise one ``UseRef`` to a JSON-friendly dict."""
    out: dict[str, object] = {"module": u.module}
    if u.only is not None:
        out["only"] = list(u.only)
    if u.renames:
        out["renames"] = [list(r) for r in u.renames]
    return out


def _load_useref(payload: dict[str, object]) -> UseRef:
    """Reconstruct a ``UseRef`` from the on-disk dict form."""
    module = str(payload.get("module", ""))
    only_raw = payload.get("only")
    only: tuple[str, ...] | None = (
        tuple(str(s) for s in only_raw)
        if isinstance(only_raw, list)
        else None
    )
    renames_raw = payload.get("renames", [])
    renames: tuple[tuple[str, str], ...] = (
        tuple((str(r[0]), str(r[1])) for r in renames_raw)
        if isinstance(renames_raw, list)
        else ()
    )
    return UseRef(module=module, only=only, renames=renames)


def _scan_into_index(
    index: WorkspaceIndex, path: Path, *, new_text: str | None = None
) -> None:
    """Scan one file's headers and merge them into ``index``.

    First-found wins on duplicate module / top-level procedure names —
    matches the link-time symbol-resolution that F77-vintage projects
    rely on, and avoids re-introducing ordering instability.

    Single-file entry point: ``update_index`` calls this on
    didChange / didSave. The bulk-scan path
    (:func:`scan_workspace`) bypasses this wrapper and calls
    :func:`_scan_one_file` directly so the parallel pool can run
    workers without contending on ``index``.

    Args:
        index: Workspace index to update in place.
        path: File to scan.
        new_text: Optional source override; when ``None`` the file is
            read from disk. OS-level read failures are recorded in
            ``index.scan_failures`` and the file is otherwise skipped.
    """
    _merge_scan_result(index, _scan_one_file(path, new_text))


# ---------------------------------------------------------------------------
# Workset resolution
# ---------------------------------------------------------------------------


def resolve_workset(
    index: WorkspaceIndex,
    entry_files: Iterable[Path],
    *,
    external_modules: frozenset[str] = frozenset(),
) -> Resolution:
    """Follow ``use`` statements transitively, return files in compile order.

    ``compile_order`` is a topological sort: every file appears after
    every file it depends on. Cycles are tolerated — files in a cycle
    appear in arbitrary order relative to each other, after their
    out-of-cycle dependencies.

    Args:
        index: Pre-built workspace index.
        entry_files: Files to seed the BFS / DFS expansion from.
        external_modules: Allowlist of module names known to live
            outside the source tree; matched entries are recorded in
            :attr:`Resolution.external` rather than reported as
            unresolved.

    Returns:
        A :class:`Resolution` carrying the compile-order tuple,
        per-consumer unresolved imports, and the set of allowlist
        modules that were actually referenced.
    """
    ext_lower = frozenset(m.lower() for m in external_modules)
    entries = [Path(p).resolve() for p in entry_files]

    # Step 1: BFS / DFS expansion from entry files.
    visited: set[Path] = set()
    unresolved: list[tuple[Path, str]] = []
    seen_external: set[str] = set()

    stack: list[Path] = list(entries)
    while stack:
        f = stack.pop()
        if f in visited:
            continue
        visited.add(f)
        for use in index.uses_by_file.get(f, ()):
            mod = use.module
            if mod in ext_lower:
                seen_external.add(mod)
                continue
            target = index.modules.get(mod)
            if target is None:
                unresolved.append((f, mod))
                continue
            if target not in visited:
                stack.append(target)
        # External-procedure resolution. For every ``CALL name``
        # invocation that names a top-level procedure declared
        # somewhere in the workspace, pull its defining file into
        # the workset too. This is the F77-style linkage path
        # that ``use``-chain expansion can't reach. Procedures
        # declared *inside* a module are not in ``index.procedures``,
        # so this path doesn't double-pull files already reached via
        # ``use``.
        for callee in index.calls_by_file.get(f, ()):
            target = index.procedures.get(callee)
            if target is None or target in visited:
                continue
            stack.append(target)

    # Step 2: topological sort within the visited set.
    order = _topo_sort(visited, index, ext_lower)

    return Resolution(
        compile_order=tuple(order),
        unresolved=tuple(unresolved),
        external=frozenset(seen_external),
    )


def _topo_sort(
    files: set[Path],
    index: WorkspaceIndex,
    external: frozenset[str],
) -> list[Path]:
    """Kahn-style topological sort over ``files``.

    Cycle members come out in arbitrary order but after their
    non-cycle predecessors. Edges come from two sources, both flowing
    dep → user:

    1. ``use`` clauses (module-style imports).
    2. ``CALL`` invocations of top-level external procedures.

    Including call edges matters for the LSP cap: the LSP truncates a
    workset to its last N topo entries (closest to the active file).
    Without call edges, an external callee — which the active file
    directly needs — ends up scattered through the middle and gets
    dropped by the cap.

    Args:
        files: Set of files to sort (already expanded by
            :func:`resolve_workset`).
        index: Workspace index providing the use / call edges.
        external: Lower-cased module names treated as out-of-workset
            (their ``use`` edges are dropped).

    Returns:
        A list of paths in dependency order, with any cycle members
        appended at the end in lexicographic order.
    """
    indeg: dict[Path, int] = {f: 0 for f in files}
    edges: dict[Path, list[Path]] = {f: [] for f in files}
    seen_edges: set[tuple[Path, Path]] = set()

    def _add_edge(target: Path, consumer: Path) -> None:
        """Add a dep edge ``target → consumer``, de-duplicating.

        Self-edges are silently dropped. Repeats are ignored so
        in-degree counts stay accurate when two dependency sources
        agree on the same edge.
        """
        if target == consumer:
            return  # self-reference is harmless
        key = (target, consumer)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges[target].append(consumer)
        indeg[consumer] += 1

    for f in files:
        for use in index.uses_by_file.get(f, ()):
            if use.module in external:
                continue
            target = index.modules.get(use.module)
            if target is None or target not in files:
                continue
            _add_edge(target, f)
        for callee in index.calls_by_file.get(f, ()):
            target = index.procedures.get(callee)
            if target is None or target not in files:
                continue
            _add_edge(target, f)

    ready = [f for f, d in indeg.items() if d == 0]
    ready.sort()  # deterministic output
    out: list[Path] = []
    while ready:
        f = ready.pop(0)
        out.append(f)
        for consumer in edges[f]:
            indeg[consumer] -= 1
            if indeg[consumer] == 0:
                ready.append(consumer)
        ready.sort()
    # Any file with remaining indegree > 0 sits in a cycle. Append it
    # after the resolved set in deterministic (lexicographic) order.
    cycle = sorted(f for f, d in indeg.items() if d > 0)
    out.extend(cycle)
    return out
