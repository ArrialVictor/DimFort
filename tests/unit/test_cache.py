from pathlib import Path

import pytest

from dimfort import cache


@pytest.fixture(autouse=True)
def _clear_lf_version_cache():
    """Reset the memoised lfortran version between tests so a stubbed
    binary path on one test doesn't leak into the next."""
    from dimfort.core import lfortran as lf
    lf._VERSION_BY_BINARY.clear()
    yield
    lf._VERSION_BY_BINARY.clear()


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


# ---------------------------------------------------------------------------
# Tree cache
# ---------------------------------------------------------------------------


class _FakeLF:
    """Stand-in for ``dimfort.core.lfortran`` that records each call.

    Real LFortran is gated to integration tests; the cache logic itself
    is pure Python and worth testing without it.
    """

    LFortranError = RuntimeError

    def __init__(self, version: str = "0.63.0"):
        self.calls: list[tuple[str, str]] = []  # (load_path, mode)
        self._version = version

    def find_lfortran(self, _explicit=None) -> Path:
        return Path("/fake/lfortran")

    def version(self, _explicit=None) -> str:
        return self._version

    def load_trees(self, load_path, *, lfortran=None, cwd=None, implicit_interface=False):
        self.calls.append((str(load_path), "load"))
        return ({"ast": True}, {"asr": True})


def _patch_lf(monkeypatch, fake: _FakeLF) -> None:
    import dimfort.core.lfortran as real_lf
    monkeypatch.setattr(real_lf, "find_lfortran", fake.find_lfortran)
    monkeypatch.setattr(real_lf, "version", fake.version)
    monkeypatch.setattr(real_lf, "load_trees", fake.load_trees)


def test_cache_miss_invokes_lfortran(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    ast, asr = cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    assert ast == {"ast": True}
    assert asr == {"asr": True}
    assert len(fake.calls) == 1
    # Entry written.
    entries = list(cache_dir.glob("*.json"))
    assert len(entries) == 1


def test_cache_hit_skips_lfortran(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    # Cold call writes the entry.
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    # Warm call must not invoke LFortran again.
    ast, asr = cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    assert ast == {"ast": True}
    assert asr == {"asr": True}
    assert len(fake.calls) == 1  # still one


def test_content_change_invalidates_cache(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    src.write_text("real :: y")  # different content → different sha256
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    assert len(fake.calls) == 2


def test_lfortran_version_change_invalidates_cache(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF(version="0.63.0")
    _patch_lf(monkeypatch, fake)

    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    from dimfort.core import lfortran as lf
    lf._VERSION_BY_BINARY.clear()
    fake._version = "0.64.0"
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    assert len(fake.calls) == 2


def test_override_content_bypasses_cache(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    # Even after a cold call writes an entry, an override invocation
    # must re-run LFortran (the buffer content might differ from disk).
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
        content=b"real :: edited",
    )
    assert len(fake.calls) == 2


def test_cache_disabled(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=None,
    )
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=None,
    )
    assert len(fake.calls) == 2  # always invokes


def test_mods_save_and_load_roundtrip(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("module m\nend module m\n")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    cache.save_mods_cached(
        src,
        {"m": b"\x00FAKEMOD\x01"},
        cache_dir=cache_dir,
    )
    out = cache.load_mods_cached(src, cache_dir=cache_dir)
    assert out == {"m": b"\x00FAKEMOD\x01"}


def test_mods_content_change_invalidates(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("module m\nend module m\n")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    cache.save_mods_cached(src, {"m": b"AAA"}, cache_dir=cache_dir)
    src.write_text("module m\n  integer :: x\nend module m\n")
    assert cache.load_mods_cached(src, cache_dir=cache_dir) is None


def test_mods_version_change_invalidates(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("module m\nend module m\n")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF(version="0.63.0")
    _patch_lf(monkeypatch, fake)

    cache.save_mods_cached(src, {"m": b"AAA"}, cache_dir=cache_dir)
    from dimfort.core import lfortran as lf
    lf._VERSION_BY_BINARY.clear()
    fake._version = "0.64.0"
    assert cache.load_mods_cached(src, cache_dir=cache_dir) is None


def test_mods_disabled_cache(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("module m\nend module m\n")
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    cache.save_mods_cached(src, {"m": b"AAA"}, cache_dir=None)
    assert cache.load_mods_cached(src, cache_dir=None) is None


def test_mods_empty_dict_is_noop(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("module m\nend module m\n")
    cache_dir = tmp_path / "cache"
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    cache.save_mods_cached(src, {}, cache_dir=cache_dir)
    # No entry file should have been written.
    assert not list(cache_dir.glob("*.mods.json")) if cache_dir.exists() else True


def test_corrupt_entry_is_overwritten(tmp_path, monkeypatch):
    src = tmp_path / "src.f90"
    src.write_text("real :: x")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    fake = _FakeLF()
    _patch_lf(monkeypatch, fake)

    # Pre-seed every possible entry path with garbage. The cache must
    # treat any non-matching/unreadable entry as a miss.
    bogus = cache_dir / "deadbeef.json"
    bogus.write_text("not valid json {{{")
    cache.load_trees_cached(
        src.name, source_path=src, cwd=tmp_path, cache_dir=cache_dir,
    )
    assert len(fake.calls) == 1
