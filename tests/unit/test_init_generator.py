"""Tests for the ``dimfort init`` template generator."""
from __future__ import annotations

from pathlib import Path

import pytest

from dimfort.cli import (
    _AVAILABLE_TEMPLATES,
    _activate_template_block,
    _extract_derived_section,
    _load_template,
    main,
)
from dimfort.core.unit_config import load_config


def test_all_templates_load_via_resources() -> None:
    """Each declared template is reachable via importlib.resources."""
    for tmpl in _AVAILABLE_TEMPLATES:
        content = _load_template(tmpl)
        assert content.startswith("# DimFort")
        assert "[derived]" in content


def test_activate_strips_comment_marker_on_entry_lines() -> None:
    """Lines that look like commented TOML entries get uncommented."""
    src = (
        "# --- group header ---\n"
        '# foo = { dim = "L" }\n'
        '# bar = { dim = "M", aliases = ["baz"] }\n'
        "# plain comment\n"
    )
    result = _activate_template_block(src)
    lines = result.splitlines()
    assert lines[0] == "# --- group header ---"            # section header preserved
    assert lines[1] == 'foo = { dim = "L" }'                # entry uncommented
    assert lines[2] == 'bar = { dim = "M", aliases = ["baz"] }'
    assert lines[3] == "# plain comment"                   # plain prose preserved


def test_extract_derived_section_drops_header() -> None:
    """The header above ``[derived]`` is stripped from each template."""
    src = "# Header\n# more\n\n[derived]\n\nfoo = { dim = \"L\" }\n"
    body = _extract_derived_section(src)
    assert body.strip() == "foo = { dim = \"L\" }"


def test_init_dry_run_writes_to_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--dry-run`` prints to stdout, doesn't write the file."""
    rc = main(["init", "--dry-run", "-t", "climate"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "DimFort project unit configuration" in captured.out
    assert "CLIMATE template (ACTIVE)" in captured.out


def test_init_writes_to_file(tmp_path: Path) -> None:
    out = tmp_path / "dimfort.toml"
    rc = main(["init", "-t", "climate", "-o", str(out)])
    assert rc == 0
    assert out.exists()
    content = out.read_text()
    assert "CLIMATE template (ACTIVE)" in content
    assert "ASTRONOMY template (commented" in content
    assert "Project-local additions" in content


def test_init_bare_skips_templates(tmp_path: Path) -> None:
    out = tmp_path / "dimfort.toml"
    rc = main(["init", "--bare", "-o", str(out)])
    assert rc == 0
    content = out.read_text()
    assert "No active templates" in content
    assert "CLIMATE template" not in content
    assert "Project-local additions" in content


def test_init_refuses_existing_without_force(tmp_path: Path) -> None:
    out = tmp_path / "dimfort.toml"
    out.write_text("# pre-existing content\n")
    rc = main(["init", "-t", "climate", "-o", str(out)])
    assert rc == 2  # exit code 2 per the documented contract
    assert out.read_text() == "# pre-existing content\n"


def test_init_force_overwrites_existing(tmp_path: Path) -> None:
    out = tmp_path / "dimfort.toml"
    out.write_text("# pre-existing content\n")
    rc = main(["init", "-t", "climate", "--force", "-o", str(out)])
    assert rc == 0
    assert "CLIMATE template (ACTIVE)" in out.read_text()


def test_init_rejects_unknown_template(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out = tmp_path / "dimfort.toml"
    rc = main(["init", "-t", "unicorn", "-o", str(out)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "unknown templates" in captured.err


def test_init_generated_file_loads_through_loader(tmp_path: Path) -> None:
    """The generated dimfort.toml parses cleanly through the unit_config loader."""
    out = tmp_path / "dimfort.toml"
    rc = main(["init", "-t", "climate,astronomy", "-o", str(out)])
    assert rc == 0
    table = load_config(out)
    # Climate template entries
    for k in ("sverdrup", "psu", "DU", "langley", "kayser"):
        assert k in table.derived, f"{k} missing"
    # Astronomy template entries
    for k in ("au", "pc", "M_sun", "Jy"):
        assert k in table.derived, f"{k} missing"
    # Sanity: SI core still there from defaults
    for k in ("Pa", "J", "W", "N"):
        assert k in table.derived


def test_init_legacy_template_loads_when_active(tmp_path: Path) -> None:
    """Edge case: the legacy template (largest, with collision-avoidance fixes)
    activates cleanly without name clashes."""
    out = tmp_path / "dimfort.toml"
    rc = main(["init", "-t", "legacy", "-o", str(out)])
    assert rc == 0
    table = load_config(out)
    for k in ("erg", "dyn", "inch", "foot", "psi", "Btu"):
        assert k in table.derived


def test_init_all_templates_compose(tmp_path: Path) -> None:
    """Activating EVERY template at once must not produce alias collisions."""
    out = tmp_path / "dimfort.toml"
    rc = main([
        "init", "-t", ",".join(_AVAILABLE_TEMPLATES),
        "-o", str(out),
    ])
    assert rc == 0
    table = load_config(out)
    assert "sverdrup" in table.derived   # climate
    assert "au" in table.derived         # astronomy
    assert "darcy" in table.derived      # geosciences
    assert "U" in table.derived          # biology-medicine
    assert "erg" in table.derived        # legacy
