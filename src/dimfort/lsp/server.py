"""DimFort language server.

Speaks LSP over stdio. Runs the full DimFort pipeline on each opened
or saved Fortran file and publishes the resulting diagnostics. The
pipeline reuses :func:`dimfort.core.multifile.check_files`; we treat
each open file as a one-file workset for v1 (no project-wide scan
yet — TODO once we have workspace-folder traversal wired up).

Started via ``dimfort lsp``. Speaks stdio by default, which is what
all the common LSP clients (VS Code, Neovim, Helix, Emacs) expect.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote, urlparse

from lsprotocol import types as lsp
from pygls.lsp.server import LanguageServer

from dimfort import __version__
from dimfort.core import lfortran as lf
from dimfort.core import unit_config  # noqa: F401  populates DEFAULT_TABLE
from dimfort.core.diagnostics import Diagnostic, Severity
from dimfort.core.multifile import check_files

log = logging.getLogger("dimfort.lsp")

server = LanguageServer("dimfort", __version__)


_SEVERITY_TO_LSP = {
    Severity.ERROR: lsp.DiagnosticSeverity.Error,
    Severity.WARNING: lsp.DiagnosticSeverity.Warning,
    Severity.INFO: lsp.DiagnosticSeverity.Information,
    Severity.HINT: lsp.DiagnosticSeverity.Hint,
}


def _uri_to_path(uri: str) -> Path | None:
    """Decode a file:// URI into a :class:`Path`."""
    if not uri.startswith("file:"):
        return None
    parsed = urlparse(uri)
    return Path(unquote(parsed.path))


def _to_lsp_diagnostic(d: Diagnostic) -> lsp.Diagnostic:
    """Translate one of our :class:`Diagnostic`s into the LSP shape.

    Source positions in DimFort are 1-based (lines and columns); LSP
    positions are 0-based. A line of 0 (used for whole-file diagnostics
    like ``U007``) is mapped to LSP line 0.
    """
    start_line = max(d.start.line - 1, 0)
    start_col = max(d.start.column - 1, 0)
    end_line = max(d.end.line - 1, 0)
    end_col = max(d.end.column - 1, 0)
    # Ensure end ≥ start so VSCode draws a squiggle even on point ranges.
    if (end_line, end_col) <= (start_line, start_col):
        end_col = start_col + 1
    return lsp.Diagnostic(
        range=lsp.Range(
            start=lsp.Position(line=start_line, character=start_col),
            end=lsp.Position(line=end_line, character=end_col),
        ),
        severity=_SEVERITY_TO_LSP.get(d.severity, lsp.DiagnosticSeverity.Error),
        code=d.code,
        source="dimfort",
        message=d.message,
    )


def _publish_for_uri(ls: LanguageServer, uri: str) -> None:
    path = _uri_to_path(uri)
    if path is None or not path.is_file():
        return
    try:
        result = check_files([path])
    except lf.LFortranNotFound as exc:
        log.warning("lfortran not found: %s", exc)
        ls.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[])
        )
        return
    except Exception:
        # The user shouldn't see a crashing language server — log and
        # publish nothing.
        log.exception("dimfort pipeline crashed on %s", path)
        ls.text_document_publish_diagnostics(
            lsp.PublishDiagnosticsParams(uri=uri, diagnostics=[])
        )
        return

    diags = result.diagnostics.get(path.resolve(), [])
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(
            uri=uri,
            diagnostics=[_to_lsp_diagnostic(d) for d in diags],
        )
    )


@server.feature(lsp.TEXT_DOCUMENT_DID_OPEN)
def _did_open(ls: LanguageServer, params: lsp.DidOpenTextDocumentParams) -> None:
    _publish_for_uri(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_SAVE)
def _did_save(ls: LanguageServer, params: lsp.DidSaveTextDocumentParams) -> None:
    _publish_for_uri(ls, params.text_document.uri)


@server.feature(lsp.TEXT_DOCUMENT_DID_CLOSE)
def _did_close(ls: LanguageServer, params: lsp.DidCloseTextDocumentParams) -> None:
    # Clear diagnostics when the file is closed so old squiggles don't linger.
    ls.text_document_publish_diagnostics(
        lsp.PublishDiagnosticsParams(uri=params.text_document.uri, diagnostics=[])
    )


def run_stdio() -> None:
    """Start the language server speaking LSP over stdio."""
    server.start_io()
