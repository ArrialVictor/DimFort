"""Tests for the M4 disk-persistent ProjectionCache layer.

Roundtrip: write a populated cache, read it back, assert structural
equality across every field of every persisted dataclass. Failure
modes: corrupt JSON, version mismatch, missing file → all return
``None`` and don't raise.
"""

from __future__ import annotations

import json
from pathlib import Path

from dimfort.core.multifile import check_files
from dimfort.core.multifile_cache import ProjectionCache
from dimfort.core.multifile_cache_persist import (
    _PROJECTION_SCHEMA_VERSION,
    load_persistent_projection_cache,
    save_persistent_projection_cache,
)

SAMPLE = """\
module sample
  implicit none
  real :: x  ! @unit{m}
  real :: y  ! @unit{s}
contains
  subroutine driver
    real :: z  ! @unit{m/s}
    z = x / y
  end subroutine driver
end module sample
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    return p


def test_roundtrip_preserves_cache_contents(tmp_path: Path):
    """Save + load reconstructs identical ScanResult + AttachmentResult."""
    src = _write(tmp_path, "p.f90", SAMPLE)
    cache = ProjectionCache()
    check_files([src], projection_cache=cache)
    assert len(cache) == 1

    save_persistent_projection_cache(cache, tmp_path)
    loaded = load_persistent_projection_cache(tmp_path)
    assert loaded is not None
    assert len(loaded) == len(cache)

    # Compare every entry field-by-field. Both ScanResult and
    # AttachmentResult are dataclasses, so per-field equality covers
    # the whole structure.
    with cache._lock:  # noqa: SLF001 — test reaches in for snapshot
        original = list(cache._entries.items())  # noqa: SLF001
    for key, value in original:
        round_tripped = loaded.get(key)
        assert round_tripped is not None
        assert round_tripped.scan == value.scan
        assert round_tripped.attachment == value.attachment


def test_loaded_cache_serves_check_files(tmp_path: Path):
    """A loaded cache should let ``check_files`` skip the scan + attach work."""
    from unittest import mock

    from dimfort.core import annotations as _ann
    from dimfort.core import attach as _att

    src = _write(tmp_path, "p.f90", SAMPLE)
    populated = ProjectionCache()
    check_files([src], projection_cache=populated)

    save_persistent_projection_cache(populated, tmp_path)
    fresh = load_persistent_projection_cache(tmp_path)
    assert fresh is not None

    # With the loaded cache in place, scan_text + attach must not run.
    with mock.patch.object(
        _ann, "scan_text", side_effect=AssertionError("scan should be skipped"),
    ), mock.patch.object(
        _att, "attach", side_effect=AssertionError("attach should be skipped"),
    ):
        check_files([src], projection_cache=fresh)


def test_missing_file_returns_none(tmp_path: Path):
    assert load_persistent_projection_cache(tmp_path) is None


def test_corrupt_json_returns_none(tmp_path: Path):
    (tmp_path / "projection-cache.json").write_text("{ not valid json")
    assert load_persistent_projection_cache(tmp_path) is None


def test_version_mismatch_returns_none(tmp_path: Path):
    (tmp_path / "projection-cache.json").write_text(
        json.dumps(
            {
                "schema_version": _PROJECTION_SCHEMA_VERSION + 99,
                "entries": [],
            }
        )
    )
    assert load_persistent_projection_cache(tmp_path) is None


def test_empty_payload_returns_empty_cache(tmp_path: Path):
    """A payload with zero entries should load to an empty cache, not None."""
    (tmp_path / "projection-cache.json").write_text(
        json.dumps(
            {
                "schema_version": _PROJECTION_SCHEMA_VERSION,
                "entries": [],
            }
        )
    )
    loaded = load_persistent_projection_cache(tmp_path)
    assert loaded is not None
    assert len(loaded) == 0


def test_save_idempotent(tmp_path: Path):
    """Calling save twice produces a parseable file both times."""
    src = _write(tmp_path, "p.f90", SAMPLE)
    cache = ProjectionCache()
    check_files([src], projection_cache=cache)

    save_persistent_projection_cache(cache, tmp_path)
    save_persistent_projection_cache(cache, tmp_path)
    loaded = load_persistent_projection_cache(tmp_path)
    assert loaded is not None
    assert len(loaded) == len(cache)


def test_save_creates_cache_root(tmp_path: Path):
    """Save should mkdir the cache root if it doesn't exist yet."""
    src = _write(tmp_path, "p.f90", SAMPLE)
    cache = ProjectionCache()
    check_files([src], projection_cache=cache)

    nested = tmp_path / "new" / "nested" / "cache_root"
    assert not nested.exists()
    save_persistent_projection_cache(cache, nested)
    assert (nested / "projection-cache.json").exists()
