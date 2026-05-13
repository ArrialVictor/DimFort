from pathlib import Path

from dimfort import cache


def test_clean_nonexistent_returns_zero(tmp_path: Path):
    target = tmp_path / "missing"
    assert cache.clean(target) == 0
    assert not target.exists()


def test_clean_removes_dir(tmp_path: Path):
    target = tmp_path / "cache"
    target.mkdir()
    (target / "a.json").write_text('{"hello": "world"}')
    (target / "sub").mkdir()
    (target / "sub" / "b.json").write_text("{}")

    freed = cache.clean(target)
    assert freed > 0
    assert not target.exists()


def test_info_nonexistent(tmp_path: Path):
    out = cache.info(tmp_path / "missing")
    assert "does not exist" in out


def test_info_existing(tmp_path: Path):
    target = tmp_path / "cache"
    target.mkdir()
    (target / "a.json").write_text('{"x": 1}')
    out = cache.info(target)
    assert "entries: 1" in out
