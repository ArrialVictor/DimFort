"""Tests for the unit-table config loader."""
import warnings
from pathlib import Path

import pytest

from dimfort.core.unit_config import DEFAULT_CONFIG_PATH, load_config
from dimfort.core.units import UnitAmbiguityWarning, UnitError, UnknownUnitError, parse


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


# ============================================================================
# Catalog-form schema: `dim`, `quantitykind`, `aliases`
# ============================================================================


def test_dim_form_entry_loads(tmp_path: Path):
    """A derived entry written with ``dim`` (no parser dependency) loads."""
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'foo = { dim = "M*L^-1*T^-2", quantitykind = "Pressure" }\n'
    )
    table = load_config(user)
    assert "foo" in table.derived
    assert table.derived["foo"].dimension == (1, -1, -2, 0, 0, 0, 0)
    assert table.derived["foo"].factor == 1


def test_dim_form_with_factor_and_offset(tmp_path: Path):
    """``dim`` + factor + offset compose correctly."""
    from fractions import Fraction
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'myCels = { dim = "Theta", offset = "273.15", quantitykind = "Temperature" }\n'
        'myBaz = { dim = "M*L^-1*T^-2", factor = 100000, quantitykind = "Pressure" }\n'
    )
    table = load_config(user)
    assert table.derived["myCels"].offset == Fraction("273.15")
    assert table.derived["myCels"].dimension == (0, 0, 0, 1, 0, 0, 0)
    assert table.derived["myBaz"].factor == 100000
    assert table.derived["myBaz"].dimension == (1, -1, -2, 0, 0, 0, 0)


def test_dim_form_dimensionless(tmp_path: Path):
    """``dim = "1"`` denotes the dimensionless unit."""
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'myPpm = { dim = "1", factor = "1/1000000", quantitykind = "Ratio" }\n'
    )
    table = load_config(user)
    assert table.derived["myPpm"].dimension == (0, 0, 0, 0, 0, 0, 0)


def test_dim_form_rejects_unknown_slot(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nfoo = { dim = "X*Y" }\n')
    with pytest.raises(UnitError, match="unknown slot"):
        load_config(user)


def test_dim_form_rejects_duplicate_slot(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nfoo = { dim = "M*M^2" }\n')
    with pytest.raises(UnitError, match="more than once"):
        load_config(user)


def test_dim_form_rejects_non_integer_exponent(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nfoo = { dim = "M^1.5" }\n')
    with pytest.raises(UnitError, match="non-integer exponent"):
        load_config(user)


def test_quantitykind_field_accepted_and_ignored(tmp_path: Path):
    """``quantitykind`` is metadata; load succeeds and value is not exposed."""
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'foo = { dim = "T^-1", quantitykind = "Frequency" }\n'
    )
    table = load_config(user)
    assert "foo" in table.derived


def test_entry_with_both_dim_and_expr_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "M", expr = "kg" }\n'
    )
    with pytest.raises(UnitError, match="cannot specify both 'dim' and 'expr'"):
        load_config(user)


def test_entry_with_neither_dim_nor_expr_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nfoo = { factor = 100 }\n')
    with pytest.raises(UnitError, match="must specify either 'dim' or 'expr'"):
        load_config(user)


def test_unknown_keys_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "M", flavor = "vanilla" }\n'
    )
    with pytest.raises(UnitError, match="unknown keys"):
        load_config(user)


# ============================================================================
# Aliases
# ============================================================================


def test_aliases_register_pointing_at_canonical(tmp_path: Path):
    """A declared alias resolves to the same Unit instance as its canonical."""
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'foo = { dim = "M*L^-1*T^-2", aliases = ["bar_alias", "foo_long"] }\n'
    )
    table = load_config(user)
    assert "foo" in table.derived
    assert "bar_alias" in table.derived
    assert "foo_long" in table.derived
    assert table.derived["foo"] is table.derived["bar_alias"]
    assert table.derived["foo"] is table.derived["foo_long"]


def test_alias_colliding_with_base_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "L", aliases = ["m"] }\n'
    )
    with pytest.raises(UnitError, match="alias collision.*base unit"):
        load_config(user)


def test_alias_colliding_with_existing_derived_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "M*L^-1*T^-2", aliases = ["N"] }\n'
    )
    with pytest.raises(UnitError, match="alias collision.*derived unit"):
        load_config(user)


def test_alias_colliding_with_prefix_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "M", aliases = ["k"] }\n'
    )
    with pytest.raises(UnitError, match="alias collision.*prefix"):
        load_config(user)


def test_alias_colliding_across_two_entries_rejected(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'fooA = { dim = "M", aliases = ["shared"] }\n'
        'fooB = { dim = "L", aliases = ["shared"] }\n'
    )
    with pytest.raises(UnitError, match="alias collision"):
        load_config(user)


def test_aliases_must_be_list(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "M", aliases = "not_a_list" }\n'
    )
    with pytest.raises(UnitError, match="'aliases' must be a list"):
        load_config(user)


def test_aliases_entries_must_be_strings(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\nfoo = { dim = "M", aliases = [42] }\n'
    )
    with pytest.raises(UnitError, match="alias entries must be strings"):
        load_config(user)


# ============================================================================
# Override gate: [base], [prefixes], [derived]
# ============================================================================


def test_project_cannot_redefine_base(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[base]\nm = "Theta"\n')
    with pytest.raises(UnitError, match="cannot redefine base unit"):
        load_config(user)


def test_project_cannot_add_new_base(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[base]\nfoo = "M"\n')
    with pytest.raises(UnitError, match="cannot add new base unit"):
        load_config(user)


def test_project_cannot_redefine_prefix(tmp_path: Path):
    user = tmp_path / "user.toml"
    user.write_text('[prefixes]\nk = 999\n')
    with pytest.raises(UnitError, match="cannot redefine prefix"):
        load_config(user)


def test_project_can_add_new_prefix(tmp_path: Path):
    """Adding a brand-new prefix (e.g. binary Ki) is permitted."""
    user = tmp_path / "user.toml"
    user.write_text('[prefixes]\nKi = 1024\n')
    table = load_config(user)
    assert table.prefixes["Ki"] == 1024


def test_project_can_redefine_derived_with_warning(tmp_path: Path):
    """Overriding a shipped derived unit is allowed but warns."""
    user = tmp_path / "user.toml"
    user.write_text('[derived]\nN = { expr = "kg*m/s^2", factor = 2 }\n')
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        table = load_config(user)
    assert table.derived["N"].factor == 2
    matching = [w for w in caught if issubclass(w.category, UnitAmbiguityWarning)]
    assert matching, f"expected UnitAmbiguityWarning; got {caught}"
    assert "redefines shipped derived units" in str(matching[0].message)
    assert "N" in str(matching[0].message)


# ============================================================================
# Backward-compat: existing expr-style still works
# ============================================================================


def test_expr_form_still_works(tmp_path: Path):
    """Project-local TOML using the legacy expr form continues to load."""
    user = tmp_path / "user.toml"
    user.write_text(
        '[derived]\n'
        'foo = { expr = "Pa", factor = 100 }\n'
    )
    table = load_config(user)
    assert table.derived["foo"].factor == 100
    assert table.derived["foo"].dimension == (1, -1, -2, 0, 0, 0, 0)
