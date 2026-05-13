"""Tests for the unit-table config loader."""
from pathlib import Path

import pytest

from dimfort.core.unit_config import DEFAULT_CONFIG_PATH, load_config
from dimfort.core.units import UnitError, UnknownUnitError, parse


def test_default_config_loads():
    table = load_config()
    assert table.base["m"].dimension == (0, 1, 0, 0, 0, 0, 0)
    assert table.derived["N"].dimension == (1, 1, -2, 0, 0, 0, 0)
    assert table.prefixes["k"] == 1000
    assert "m" in table.prefixable


def test_default_config_path_exists():
    assert DEFAULT_CONFIG_PATH.exists()


def test_user_config_can_add_prefixable_derived(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\neV = { expr = "J", prefixable = true }\n')
    table = load_config(user)
    assert "eV" in table.derived
    assert "eV" in table.prefixable
    keV = parse("keV", table)
    assert keV.dimension == table.derived["J"].dimension
    assert keV.factor == 1000


def test_user_config_collision_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nkm = { expr = "m" }\n')
    with pytest.raises(UnitError, match="collision"):
        load_config(user)


def test_user_config_unknown_reference_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nfoo = { expr = "widget*s" }\n')
    with pytest.raises(UnitError, match="could not be resolved"):
        load_config(user)


def test_non_prefixable_derived_rejects_prefix():
    with pytest.raises(UnknownUnitError):
        parse("MN")
