"""CLI ``--backend`` flag dispatches to the correct pipeline.

Smoke-tests both flags by running the in-process ``cli.main`` against
the multifile fixture. Both backends should produce the H001+H004 set
expected by the existing ``test_multifile`` baseline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.cli import main
from dimfort.core import lfortran as lf


def _have_lfortran() -> bool:
    try:
        lf.find_lfortran()
        return True
    except lf.LFortranNotFound:
        return False


pytestmark = pytest.mark.skipif(
    not _have_lfortran(), reason="lfortran binary not available"
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "multifile"


@pytest.mark.parametrize("backend", ["asr", "ast"])
def test_cli_backend_dispatch(backend, capsys):
    """Both backends produce non-zero exit code on the multifile
    fixture (it has deliberate unit errors). Smoke-test that the flag
    is wired through end-to-end."""
    rc = main([
        "check",
        "--backend", backend,
        "--no-cache",
        str(FIXTURES / "geo.f90"),
        str(FIXTURES / "main.f90"),
    ])
    out = capsys.readouterr().out
    assert rc == 1, (
        f"expected exit code 1 (errors present); got {rc}.\n{out}"
    )
    # Both backends should report H001 and H004 from main.f90.
    assert "H001" in out, f"backend={backend}: expected H001 in output\n{out}"
    assert "H004" in out, f"backend={backend}: expected H004 in output\n{out}"


def test_cli_invalid_backend_rejected():
    """argparse should reject an unknown backend."""
    with pytest.raises(SystemExit):
        main([
            "check",
            "--backend", "neural-net",
            str(FIXTURES / "geo.f90"),
        ])
