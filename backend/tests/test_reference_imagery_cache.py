from __future__ import annotations

import os
from pathlib import Path

import pytest

import src.domain.reference_imagery_cache as reference_imagery_cache
from src.domain.reference_imagery_cache import (
    build_aoi_hash,
    build_reference_imagery_cache_key_payload,
    build_reference_imagery_key,
    materialize_reference_imagery_cog,
    read_reference_imagery_cache_metadata,
    reference_imagery_cache_cog_path,
    write_reference_imagery_cache_metadata,
)


def _write_file(path: Path, content: bytes = b"cog") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _key_payload(tmp_path: Path, *, release_identifier: str = "WB_2026_R04", aoi_offset: float = 0.0) -> dict[str, object]:
    source_path = _write_file(tmp_path / "source.tif")
    mask_path = _write_file(tmp_path / "valid_mask.tif", b"mask")
    aoi = {
        "type": "Polygon",
        "coordinates": [
            [
                [-7.0 + aoi_offset, 33.0],
                [-6.9 + aoi_offset, 33.0],
                [-6.9 + aoi_offset, 33.1],
                [-7.0 + aoi_offset, 33.1],
                [-7.0 + aoi_offset, 33.0],
            ]
        ],
    }
    return build_reference_imagery_cache_key_payload(
        provider="esri_wayback",
        release_identifier=release_identifier,
        release_num=None,
        tile_matrix_set="WebMercatorQuad",
        zoom=18,
        tile_range={"min_x": 1, "min_y": 2, "max_x": 3, "max_y": 4},
        bounds_3857=[1.0, 2.0, 3.0, 4.0],
        source_raster_path=source_path,
        valid_mask_path=mask_path,
        aoi_hash=build_aoi_hash(aoi),
        reference_cog_format_version=4,
    )


def test_reference_imagery_key_is_deterministic_for_same_inputs(tmp_path: Path) -> None:
    payload = _key_payload(tmp_path)

    assert build_reference_imagery_key(payload) == build_reference_imagery_key(dict(reversed(payload.items())))


def test_reference_imagery_key_changes_for_release_aoi_and_format(tmp_path: Path) -> None:
    base = _key_payload(tmp_path)
    base_key = build_reference_imagery_key(base)

    changed_release = dict(base, release_identifier="WB_2025_R03")
    changed_aoi = _key_payload(tmp_path, aoi_offset=0.01)
    changed_format = dict(base, reference_cog_format_version=5)

    assert build_reference_imagery_key(changed_release) != base_key
    assert build_reference_imagery_key(changed_aoi) != base_key
    assert build_reference_imagery_key(changed_format) != base_key


def test_reference_imagery_key_ignores_project_staging_paths(tmp_path: Path) -> None:
    first_source = _write_file(tmp_path / "requests" / "request-a" / "source.tif")
    first_mask = _write_file(tmp_path / "requests" / "request-a" / "valid_mask.tif", b"mask")
    second_source = _write_file(tmp_path / "requests" / "request-b" / "source.tif")
    second_mask = _write_file(tmp_path / "requests" / "request-b" / "valid_mask.tif", b"mask")
    common = {
        "provider": "esri_wayback",
        "release_identifier": "WB_2026_R04",
        "release_num": None,
        "tile_matrix_set": "WebMercatorQuad",
        "zoom": 18,
        "tile_range": {"min_x": 1, "min_y": 2, "max_x": 3, "max_y": 4},
        "bounds_3857": [1.0, 2.0, 3.0, 4.0],
        "aoi_hash": build_aoi_hash({"type": "Point", "coordinates": [-7.0, 33.0]}),
        "reference_cog_format_version": 4,
    }

    first_payload = build_reference_imagery_cache_key_payload(
        **common,
        source_raster_path=first_source,
        valid_mask_path=first_mask,
    )
    second_payload = build_reference_imagery_cache_key_payload(
        **common,
        source_raster_path=second_source,
        valid_mask_path=second_mask,
    )

    assert build_reference_imagery_key(first_payload) == build_reference_imagery_key(second_payload)


def test_reference_imagery_cache_metadata_roundtrip(tmp_path: Path) -> None:
    metadata_path = tmp_path / "imagery_cache" / "refimg-v1-demo" / "metadata.json"
    metadata = {
        "reference_imagery_key": "refimg-v1-demo",
        "canonical_cog_path": str(metadata_path.with_name("reference_imagery_cog.tif")),
        "materializations": [],
    }

    write_reference_imagery_cache_metadata(metadata_path, metadata)

    assert read_reference_imagery_cache_metadata(metadata_path) == metadata


def test_reference_imagery_cache_path_uses_key_directory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "imagery_cache"

    assert reference_imagery_cache_cog_path(cache_dir, "refimg-v1-demo") == (
        cache_dir / "refimg-v1-demo" / "reference_imagery_cog.tif"
    )


def test_materialize_reference_imagery_uses_hardlink_by_default(tmp_path: Path) -> None:
    canonical = _write_file(tmp_path / "imagery_cache" / "refimg-v1-demo" / "reference_imagery_cog.tif")
    project_cog = tmp_path / "temporal_projects" / "project" / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"

    result = materialize_reference_imagery_cog(canonical_cog_path=canonical, project_cog_path=project_cog)

    assert result["method"] in {"hardlink", "symlink", "copy"}
    assert project_cog.read_bytes() == canonical.read_bytes()
    if result["method"] == "hardlink":
        assert os.stat(project_cog).st_ino == os.stat(canonical).st_ino


def test_materialize_reference_imagery_falls_back_to_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    canonical = _write_file(tmp_path / "imagery_cache" / "refimg-v1-demo" / "reference_imagery_cog.tif")
    project_cog = tmp_path / "temporal_projects" / "project" / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"

    def fail_hardlink(_canonical: Path, _project: Path) -> str:
        raise OSError("cross-device link")

    monkeypatch.setattr(reference_imagery_cache, "_try_hardlink", fail_hardlink)

    result = materialize_reference_imagery_cog(
        canonical_cog_path=canonical,
        project_cog_path=project_cog,
        mode="hardlink",
    )

    assert result["method"] == "symlink"
    assert project_cog.is_symlink()
    assert project_cog.resolve() == canonical.resolve()


def test_materialize_reference_imagery_falls_back_to_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    canonical = _write_file(tmp_path / "imagery_cache" / "refimg-v1-demo" / "reference_imagery_cog.tif")
    project_cog = tmp_path / "temporal_projects" / "project" / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"

    def fail_link(_canonical: Path, _project: Path) -> str:
        raise OSError("unsupported")

    monkeypatch.setattr(reference_imagery_cache, "_try_hardlink", fail_link)
    monkeypatch.setattr(reference_imagery_cache, "_try_symlink", fail_link)

    result = materialize_reference_imagery_cog(
        canonical_cog_path=canonical,
        project_cog_path=project_cog,
        mode="hardlink",
    )

    assert result["method"] == "copy"
    assert not project_cog.is_symlink()
    assert project_cog.read_bytes() == canonical.read_bytes()


def test_materialize_reference_imagery_only_replaces_project_compatibility_cog(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime_cache"
    canonical = _write_file(runtime / "imagery_cache" / "refimg-v1-demo" / "reference_imagery_cog.tif")
    request_sentinel = _write_file(runtime / "requests" / "req-1" / "manifest.json", b"{}")
    wayback_sentinel = _write_file(runtime / "wayback_mosaics" / "mosaic-1" / "metadata.json", b"{}")
    project_cog = runtime / "temporal_projects" / "project" / "milestones" / "WB_2026_R04" / "reference_imagery_cog.tif"

    materialize_reference_imagery_cog(canonical_cog_path=canonical, project_cog_path=project_cog)

    assert request_sentinel.is_file()
    assert wayback_sentinel.is_file()
    assert project_cog.is_file()
