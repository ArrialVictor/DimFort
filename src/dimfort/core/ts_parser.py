"""Tree-sitter Fortran parser wrapper.

Phase 0 of the tree-sitter migration. Mirrors the API surface of
:mod:`dimfort.core.lfortran` but uses ``tree-sitter`` + ``tree-sitter-
fortran`` instead of an LFortran subprocess.

Why we're migrating to tree-sitter (in one paragraph): LFortran's AST
positions drift on ``&``-continuations, ~16 files in our reference
trial trigger ``Internal Compiler Errors`` we have to allowlist,
parsing a 200 KB
file takes ~100 ms and a 700 KB file far longer. Tree-sitter parses
the same corpus in seconds, recovers from syntax errors with localised
``ERROR`` nodes instead of fatal failures, gives byte-exact positions
on every node including comments, and matches DimFort's
linter-not-compiler philosophy. See ``Homogeneity/scratch/tree-
sitter-eval/RESULTS.md`` for the benchmark numbers behind that claim.

Position convention: tree-sitter exposes 0-based ``(row, column)`` on
every node via ``start_point`` / ``end_point``. DimFort uses 1-based
``line`` and ``column`` throughout (matching LSP, editors, compiler
diagnostics). The :func:`position_for` helper bridges this; callers
should never read ``node.start_point`` directly.

CPP-preprocessed ``.F90`` files: tree-sitter has no built-in CPP, but
its grammar is error-tolerant — raw ``#ifdef`` blocks parse with a
small ERROR node and the surrounding Fortran code is recovered.
Empirically (over ~2400 real-world files, see the spike notes) most ``.F90``
files parse acceptably raw, but continuations interleaved with
``#ifdef`` lines lose argument-list structure. For those cases use
:func:`parse_with_cpp`, which runs the system ``cpp`` first and parses
the expanded text. Position remapping back to the source file is
exposed via :class:`PreprocessedSource`.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_fortran as _tsf
from tree_sitter import Language, Node, Parser, Tree

# ---------------------------------------------------------------------------
# Parser singleton

_LANGUAGE: Language | None = None
_PARSER: Parser | None = None


def _parser() -> Parser:
    """Lazy-create and reuse a single Parser instance.

    Tree-sitter ``Parser`` is cheap to create, but holding one instance
    avoids re-loading the grammar shared object on every call.

    Returns:
        The module-level singleton ``Parser`` instance, created on first
        call.
    """
    global _LANGUAGE, _PARSER
    if _PARSER is None:
        _LANGUAGE = Language(_tsf.language())
        _PARSER = Parser(_LANGUAGE)
    return _PARSER


# ---------------------------------------------------------------------------
# Errors

class TreeSitterError(Exception):
    """Base class for parse and CPP-shim failures in this module."""


class CppNotFoundError(TreeSitterError):
    """Raised when the system ``cpp`` binary cannot be located on ``PATH``."""


class CppFailedError(TreeSitterError):
    """Raised when ``cpp`` exits non-zero.

    Attributes:
        stderr: First line of ``cpp``'s stderr stream, captured for
            display in the diagnostic surface.
    """

    def __init__(self, message: str, stderr: str):
        """Initialise the error.

        Args:
            message: Human-readable summary (typically the failing file
                name and the ``cpp`` return code).
            stderr: First line of ``cpp``'s stderr output.
        """
        super().__init__(message)
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Plain parsing

def parse_text(text: str | bytes) -> Tree:
    """Parse Fortran source text with no preprocessing.

    Args:
        text: Fortran source. ``str`` inputs are UTF-8 encoded under the
            hood; pass ``bytes`` when raw file content is already in
            hand to save a copy.

    Returns:
        The parsed tree-sitter ``Tree``.
    """
    src = text.encode("utf-8") if isinstance(text, str) else text
    return _parser().parse(src)


def parse_file(path: str | Path) -> Tree:
    """Parse a Fortran source file with no preprocessing.

    For ``.F90`` files containing CPP directives, prefer
    :func:`parse_with_cpp`. ``parse_file`` is appropriate for ``.f90``
    or for ``.F90`` known to be directive-free.

    Args:
        path: Filesystem path to the Fortran source file.

    Returns:
        The parsed tree-sitter ``Tree``.
    """
    return parse_text(Path(path).read_bytes())


# ---------------------------------------------------------------------------
# CPP-preprocessed parsing

@dataclass(frozen=True)
class PreprocessedSource:
    """A parsed CPP-expanded source, plus an expanded-to-source line remap.

    Lines from ``#include``-injected content map to ``None`` in
    ``line_map`` (callers should normally not encounter them since
    DimFort doesn't analyse code from .h includes).

    Attributes:
        tree: The parsed tree-sitter ``Tree`` over the expanded source.
        expanded_text: Raw bytes fed to the parser, after stripping
            ``cpp``'s line markers.
        line_map: Indexed by ``expanded_line - 1`` (0-based); each entry
            is the 1-based source line that produced the expanded line,
            or ``None`` for lines synthesised from an ``#include``.
        cpp_closure: Documented inline beside the field.
    """
    tree: Tree
    expanded_text: bytes
    line_map: tuple[int | None, ...]  # index = expanded_line - 1
    cpp_closure: frozenset[str] = frozenset()
    """Absolute paths of every file cpp pulled in via ``#include``.

    Excludes the source file itself and cpp's own ``<built-in>`` /
    ``<command-line>`` pseudo-files. Empty when the source has no
    ``#include`` directives. Used by the content-hash cache to
    invalidate entries when a transitively-included header changes.
    """

    def source_line(self, expanded_line_1based: int) -> int | None:
        """Map an expanded 1-based line number back to the source file.

        Args:
            expanded_line_1based: 1-based line number in the expanded
                (post-cpp, marker-stripped) text.

        Returns:
            The corresponding 1-based source-file line, or ``None`` if
            the line came from an ``#include`` or is out of range.
        """
        idx = expanded_line_1based - 1
        if 0 <= idx < len(self.line_map):
            return self.line_map[idx]
        return None


def _find_cpp() -> str:
    """Locate the system ``cpp`` binary.

    Returns:
        Absolute path to the ``cpp`` executable.

    Raises:
        CppNotFoundError: If ``cpp`` is not on ``PATH``.
    """
    cpp = shutil.which("cpp")
    if cpp is None:
        raise CppNotFoundError("system 'cpp' not found in PATH")
    return cpp


def _build_line_map(expanded_with_markers: bytes, source_path: Path) -> tuple[int | None, ...]:
    """Walk a ``cpp``-output stream with line markers and build an expanded-to-source map.

    ``cpp`` emits ``# linenum "file"`` markers when called without
    ``-P``. Those markers track which lines of the expanded output
    correspond to which line of the original source; lines from other
    files (``#include`` expansions) map to ``None``.

    The marker syntax is::

      # <line> "<file>" [<flags>...]

    Per GCC docs, lines in the output between two markers come from
    the file the first marker names, starting at the line number the
    first marker gives.

    Args:
        expanded_with_markers: Raw ``cpp`` output including line markers.
        source_path: Path to the original source file (used to recognise
            target-file markers).

    Returns:
        Tuple indexed by expanded-line-minus-one; each entry is the
        1-based source line, or ``None`` for lines that originated
        outside ``source_path``.
    """
    target = str(source_path)
    target_realpath = str(source_path.resolve())
    out_map: list[int | None] = []
    current_file: str | None = None
    current_line: int = 1
    for raw in expanded_with_markers.splitlines():
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if line.startswith("# ") and '"' in line:
            try:
                # # 123 "filename" [flags...]
                rest = line[2:]
                num_str, rest = rest.split(" ", 1)
                lineno = int(num_str)
                # Filename is between the first two quotes
                first = rest.index('"') + 1
                second = rest.index('"', first)
                fname = rest[first:second]
                current_file = fname
                current_line = lineno
                continue  # marker line itself is not in the expanded output
            except (ValueError, IndexError):
                pass  # malformed marker, treat as content
        # This output line corresponds to current_file:current_line
        if current_file in (target, target_realpath, source_path.name):
            out_map.append(current_line)
        else:
            out_map.append(None)
        current_line += 1
    return tuple(out_map)


def parse_with_cpp(
    path: str | Path,
    *,
    defines: Sequence[str] = (),
    include_paths: Sequence[str | Path] = (),
) -> PreprocessedSource:
    """Preprocess a Fortran file with system ``cpp``, then parse the expansion.

    On macOS ``cpp`` is a clang wrapper; ``-I`` must be passed as the
    joined form ``-IPATH``, not ``-I PATH`` (the wrapper mis-parses
    the split form). The joined form is used unconditionally — it
    works on every platform DimFort cares about.

    One ``cpp`` invocation, no ``-P``: that keeps the
    ``# <linenum> "file"`` markers in the output so the expanded-line
    to source-line map can be built accurately. The marker lines are
    then stripped to produce parser input, and the map is constructed
    from the same stream so it stays in sync line-for-line with what
    the parser actually sees.

    Earlier versions called ``cpp`` twice (once with ``-P``, once
    without) and built the map from the with-markers stream. That
    produced a line-count mismatch because ``-P`` also suppresses the
    blank lines surrounding suppressed markers — diagnostics on cpp'd
    files consistently appeared 1-2 lines above their real source
    position.

    Args:
        path: Filesystem path to the Fortran source file.
        defines: ``-D`` preprocessor symbols to pass to ``cpp``.
        include_paths: ``-I`` include search paths to pass to ``cpp``.

    Returns:
        A :class:`PreprocessedSource` carrying the parsed tree, the
        marker-stripped expanded bytes, the expanded-to-source line
        map, and the set of files pulled in transitively via
        ``#include`` (the cpp closure).

    Raises:
        CppNotFoundError: If the system ``cpp`` binary is missing.
        CppFailedError: If ``cpp`` exits non-zero on ``path``.
    """
    cpp = _find_cpp()
    p = Path(path)

    cmd = [cpp]  # no -P, we need the markers
    for d in defines:
        cmd.append("-D" + d)
    for inc in include_paths:
        cmd.append("-I" + str(inc))
    cmd.append(str(p))
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise CppFailedError(
            f"cpp failed on {p.name} (rc={r.returncode})",
            err[0] if err else "(no message)",
        )
    raw = r.stdout

    expanded_lines: list[bytes] = []
    line_map: list[int | None] = []
    target_basename_lc = p.name.lower()
    target_strs = {str(p), str(p.resolve())}
    current_file: str | None = None
    current_line: int = 1
    saw_any_target_marker = False
    cpp_closure: set[str] = set()

    def _is_target(marker_file: str) -> bool:
        r"""Return ``True`` if this marker points at the source file passed to cpp.

        Direct string match handles POSIX-style ``cpp`` on macOS/Linux.
        The basename fallback handles Windows path-encoding quirks
        (some ``cpp`` builds emit ``C:/...`` while ``Path.str`` gives
        ``C:\...``; some emit backslashes literally) and the
        case-insensitive Windows filesystem.

        Args:
            marker_file: The filename from a ``# linenum "file"`` cpp marker.

        Returns:
            ``True`` when the marker file matches the source path
            (direct, resolved, or basename-case-insensitive).
        """
        if marker_file in target_strs:
            return True
        # Normalise separators before peeling the basename.
        try:
            marker_name = Path(marker_file.replace("\\", "/")).name.lower()
        except (TypeError, ValueError):
            return False
        return marker_name == target_basename_lc

    for line in raw.splitlines(keepends=True):
        # A line marker looks like ``# 123 "filename" [flags]``. We
        # decode each line as latin-1 (lossless single-byte) just for
        # the marker scan; the line itself stays as raw bytes when
        # we copy it into the parser input.
        is_marker = False
        marker_flags: tuple[str, ...] = ()
        if line.startswith(b"# ") and b'"' in line:
            try:
                head = line.decode("ascii", "ignore")
                rest = head[2:]
                num_str, rest = rest.split(" ", 1)
                lineno = int(num_str)
                first = rest.index('"') + 1
                second = rest.index('"', first)
                current_file = rest[first:second]
                current_line = lineno
                # Trailing flags (per GCC docs):
                #   1 = start of new file, 2 = returning to file,
                #   3 = system header, 4 = treat as extern "C".
                # We skip system headers (3) from the cpp closure —
                # they're toolchain-provided (e.g. Linux's implicit
                # ``/usr/include/stdc-predef.h``) and including them
                # makes the cache key platform-dependent.
                tail = rest[second + 1:].strip()
                marker_flags = tuple(tail.split()) if tail else ()
                is_marker = True
            except (ValueError, IndexError):
                pass
        if is_marker:
            # Record non-target, non-builtin, non-system files as
            # cpp_closure members.
            if (
                current_file is not None
                and not current_file.startswith("<")
                and not _is_target(current_file)
                and "3" not in marker_flags
            ):
                try:
                    cpp_closure.add(str(Path(current_file).resolve()))
                except (OSError, ValueError):
                    cpp_closure.add(current_file)
            continue
        expanded_lines.append(line)
        if current_file is not None and _is_target(current_file):
            line_map.append(current_line)
            saw_any_target_marker = True
        else:
            line_map.append(None)
        current_line += 1

    # Fallback: if no marker ever pointed at our source file (some
    # Windows cpp builds don't emit ``#``-line markers at all when
    # running over a single file with no #includes), assume the
    # output is a 1-to-1 copy of the source. That's correct when
    # cpp didn't add or remove any lines — the common case for
    # files without #ifdef-removed blocks. With #ifdef-active
    # branches we'd be wrong, but no markers means we have no
    # signal to do better, and 1-to-1 beats all-None.
    if not saw_any_target_marker:
        line_map = [i + 1 for i in range(len(line_map))]

    expanded = b"".join(expanded_lines)
    tree = parse_text(expanded)
    return PreprocessedSource(
        tree=tree, expanded_text=expanded, line_map=tuple(line_map),
        cpp_closure=frozenset(cpp_closure),
    )


# ---------------------------------------------------------------------------
# Tree walking

def walk(node: Node) -> Iterator[Node]:
    """Pre-order depth-first walk over a tree-sitter node.

    Yields ``node`` first, then descends into each child. Matches the
    iteration order callers ported from the LFortran API expect.

    Implementation: drives tree-sitter's ``TreeCursor`` instead of
    recursing through ``node.children``. Profiling on a reference
    workset (~2400 files) showed the recursive ``yield from`` form
    accounted for ~65% of wall-clock during the check phase —
    each Python-level ``yield from`` frame plus the ``.children``
    list materialisation costs more than the actual tree traversal.
    Cursor-based iteration walks in C and saves roughly 2x on the
    full pipeline.

    Args:
        node: Root tree-sitter node to walk from.

    Yields:
        Each node in pre-order depth-first sequence, starting with
        ``node`` itself.
    """
    cursor = node.walk()
    # ``cursor.node`` is ``Node | None`` in the API but is non-None at every
    # step of a live walk; guard to satisfy the typed ``Iterator[Node]``.
    if (n := cursor.node) is not None:
        yield n
    while True:
        if cursor.goto_first_child():
            if (n := cursor.node) is not None:
                yield n
            continue
        while True:
            if cursor.goto_next_sibling():
                if (n := cursor.node) is not None:
                    yield n
                break
            if not cursor.goto_parent():
                return


# ---------------------------------------------------------------------------
# Position helpers

@dataclass(frozen=True)
class SourcePosition:
    """1-based ``(line, column)`` matching DimFort's diagnostic convention.

    Tree-sitter exposes 0-based ``(row, column)``. This helper is the
    only place that conversion happens; callers should not read
    ``node.start_point`` directly.

    Attributes:
        line: 1-based line number.
        column: 1-based column number.
    """
    line: int
    column: int


def position_for(node: Node) -> SourcePosition:
    """Return the start position of ``node`` in DimFort's 1-based convention.

    Args:
        node: A tree-sitter node.

    Returns:
        The 1-based start :class:`SourcePosition`.
    """
    row, col = node.start_point
    return SourcePosition(line=row + 1, column=col + 1)


def end_position_for(node: Node) -> SourcePosition:
    """Return the end position (exclusive) of ``node`` in 1-based convention.

    Args:
        node: A tree-sitter node.

    Returns:
        The 1-based exclusive end :class:`SourcePosition`.
    """
    row, col = node.end_point
    return SourcePosition(line=row + 1, column=col + 1)


def node_text(node: Node, source: bytes) -> str:
    """Extract the source text spanned by ``node`` as a Python ``str``.

    Tree-sitter nodes don't carry their own text; the caller must
    provide the original ``bytes``. UTF-8 decoded with ``replace`` so
    non-ASCII comments don't break the wrapper.

    Args:
        node: The tree-sitter node whose byte span to slice.
        source: The raw source bytes the tree was parsed from.

    Returns:
        The decoded source text covered by ``node``.
    """
    return source[node.start_byte:node.end_byte].decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Error inspection

def has_error(tree: Tree) -> bool:
    """Return ``True`` if any node in ``tree`` is ``ERROR`` or missing.

    Args:
        tree: The parsed tree-sitter tree.

    Returns:
        ``True`` when the root node reports parse errors.
    """
    return tree.root_node.has_error


def error_nodes(tree: Tree) -> Iterator[Node]:
    """Yield every ``ERROR`` or missing node in ``tree``.

    Args:
        tree: The parsed tree-sitter tree.

    Yields:
        Each node whose ``type`` is ``"ERROR"`` or whose ``is_missing``
        flag is set, in pre-order.
    """
    for n in walk(tree.root_node):
        if n.type == "ERROR" or n.is_missing:
            yield n
