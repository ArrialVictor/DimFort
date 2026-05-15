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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from dimfort.core.annotations import _comment_start


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------


# `module NAME` declaration. Excludes `module procedure` interface bodies.
_MODULE_DECL_RE = re.compile(
    r"^\s*module\s+(?!procedure\b)([A-Za-z_]\w*)",
    re.IGNORECASE,
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
    """One ``use`` statement found in a source file."""

    module: str                       # lower-cased, normalised
    only: tuple[str, ...] | None      # ``None`` = whole module imported
    renames: tuple[tuple[str, str], ...]  # (local_name, original_name) pairs


@dataclass
class WorkspaceIndex:
    """Module-to-file map plus per-file ``use`` lists."""

    modules: dict[str, Path] = field(default_factory=dict)
    uses_by_file: dict[Path, tuple[UseRef, ...]] = field(default_factory=dict)
    scan_failures: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a workset from an entry-point set."""

    compile_order: tuple[Path, ...]
    unresolved: tuple[tuple[Path, str], ...]  # (consumer_file, missing_module)
    external: frozenset[str]                  # module names that matched the allowlist


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    """Return ``line`` with any trailing comment removed (string-aware)."""
    col = _comment_start(line)
    return line if col is None else line[:col]


def _parse_only_list(tail: str) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Split an ``only:`` tail into ``(plain_names, renames)``."""
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


def extract_modules(text: str) -> tuple[str, ...]:
    """Return every ``module NAME`` declaration in ``text`` (lower-cased)."""
    names: list[str] = []
    for line in text.splitlines():
        code = _strip_comment(line)
        m = _MODULE_DECL_RE.match(code)
        if m:
            names.append(m.group(1).lower())
    return tuple(names)


def extract_uses(text: str) -> tuple[UseRef, ...]:
    """Return every ``use`` statement in ``text``.

    Multi-line ``use`` statements continued with ``&`` are joined
    before matching so the ``only:`` list isn't truncated.
    """
    uses: list[UseRef] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        code = _strip_comment(lines[i]).rstrip()
        # Join continuation lines if present so the only-list isn't split.
        while code.endswith("&") and i + 1 < len(lines):
            code = code[:-1].rstrip()
            i += 1
            cont = _strip_comment(lines[i]).strip()
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
    """Yield every Fortran source file under ``roots`` (recursive)."""
    for root in roots:
        root = Path(root).resolve()
        if root.is_file():
            if root.suffix in include_suffixes and not _excluded(root, exclude_patterns):
                yield root
            continue
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in include_suffixes:
                continue
            if _excluded(p, exclude_patterns):
                continue
            yield p


def _excluded(path: Path, patterns: tuple[str, ...]) -> bool:
    """True if ``path`` matches any glob in ``patterns``."""
    if not patterns:
        return False
    s = str(path)
    return any(
        path.match(pat) or _glob_substring_match(s, pat) for pat in patterns
    )


def _glob_substring_match(s: str, pattern: str) -> bool:
    """Match ``pattern`` against ``s`` even when the pattern is a path
    fragment like ``build/**``. ``Path.match`` requires the pattern to
    align with the right-hand part of the path; this fallback also
    matches a fragment anywhere along the path."""
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


def scan_workspace(
    roots: Iterable[Path],
    *,
    include_suffixes: frozenset[str] = DEFAULT_INCLUDE_SUFFIXES,
    exclude_patterns: tuple[str, ...] = (),
) -> WorkspaceIndex:
    """Walk every root, scan each Fortran source for module/use headers."""
    index = WorkspaceIndex()
    for path in _iter_fortran_files(roots, include_suffixes, exclude_patterns):
        _scan_into_index(index, path)
    return index


def update_index(
    index: WorkspaceIndex,
    changed: Path,
    *,
    new_text: str | None = None,
) -> WorkspaceIndex:
    """Re-scan a single file. Mutates and returns ``index``.

    ``new_text`` lets the LSP pass in unsaved buffer contents; otherwise
    the file is read from disk.
    """
    changed = Path(changed).resolve()
    # Drop previous entries for this file.
    for name, owner in list(index.modules.items()):
        if owner == changed:
            del index.modules[name]
    index.uses_by_file.pop(changed, None)
    index.scan_failures.pop(changed, None)
    _scan_into_index(index, changed, new_text=new_text)
    return index


def _scan_into_index(
    index: WorkspaceIndex, path: Path, *, new_text: str | None = None
) -> None:
    try:
        text = new_text if new_text is not None else path.read_text()
    except OSError as exc:
        index.scan_failures[path] = str(exc)
        return
    for module_name in extract_modules(text):
        index.modules.setdefault(module_name, path)
    index.uses_by_file[path] = extract_uses(text)


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
    """Kahn-style topo sort. Cycle members come out in arbitrary order
    but after their non-cycle predecessors."""
    indeg: dict[Path, int] = {f: 0 for f in files}
    edges: dict[Path, list[Path]] = {f: [] for f in files}
    for f in files:
        for use in index.uses_by_file.get(f, ()):
            if use.module in external:
                continue
            target = index.modules.get(use.module)
            if target is None or target not in files:
                continue
            if target == f:  # `use M` from inside module M is harmless
                continue
            edges[target].append(f)
            indeg[f] += 1

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
