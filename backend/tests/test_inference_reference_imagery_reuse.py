from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_bounds, from_origin

import src.domain.inference_reference_imagery as inference_reference_imagery
from src.config import Settings
from src.domain.inference_reference_imagery import (
    _reference_key_payload,
    _tile_grid_for_bbox,
    get_or_create_inference_reference_imagery,
    validate_canonical_cog_for_inference,
)
from src.domain.mosaic import MosaicResult
from src.domain.reference_imagery_cache import (
    build_reference_imagery_cache_metadata,
    build_reference_imagery_key,
    reference_imagery_cache_cog_path,
    reference_imagery_cache_metadata_path,
    write_reference_imagery_cache_metadata,
)
from src.domain.wayback import WaybackRelease
from src.services.temporal_reference_imagery import ensure_reference_imagery_cog


def _settings(tmp_path: Path) -> Settings:
    return Settings(runtime_cache_dir=tmp_path, materialize_source_imagery_in_requests=False)


def _release(identifier: str = "WB_2026_R04") -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2026, 4, 30),
        label=f"2026-04-30 | {identifier}",
        release_num=4,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://example.com/{TileMatrix}/{TileCol}/{TileRow}",
    )


def _aoi() -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [-0.0001, -0.0001],
                [0.0001, -0.0001],
                [0.0001, 0.0001],
                [-0.0001, 0.0001],
                [-0.0001, -0.0001],
            ]
        ],
    }


def _bbox() -> dict[str, float]:
    return {"west": -0.0001, "south": -0.0001, "east": 0.0001, "north": 0.0001}


def _write_rgb(path: Path, *, bounds_3857: tuple[float, float, float, float], width: int, height: int, crs: str | None = "EPSG:3857") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.zeros((3, height, width), dtype=np.uint8)
    arr[0, :, :] = 10
    arr[1, :, :] = 20
    arr[2, :, :] = 30
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=3,
        dtype="uint8",
        crs=crs,
        transform=from_bounds(*bounds_3857, width=width, height=height),
    ) as dst:
        dst.write(arr)
    return path


def _write_mask(path: Path, *, bounds_3857: tuple[float, float, float, float], width: int, height: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=width,
        height=height,
        count=1,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_bounds(*bounds_3857, width=width, height=height),
    ) as dst:
        dst.write(np.ones((height, width), dtype=np.uint8), 1)
    return path


def _prepared_source(tmp_path: Path, settings: Settings, release: WaybackRelease) -> tuple[Path, Path, tuple[int, int, int, int], tuple[float, float, float, float], int, int]:
    tile_range, bounds_3857, width, height, _tile_count = _tile_grid_for_bbox(_bbox(), settings.zoom)
    source = _write_rgb(tmp_path / "source" / "mosaic.tif", bounds_3857=bounds_3857, width=width, height=height)
    mask = _write_mask(tmp_path / "source" / "valid_mask.tif", bounds_3857=bounds_3857, width=width, height=height)
    return source, mask, tile_range, bounds_3857, width, height


def _scene(path: Path, mask: Path, release: WaybackRelease, settings: Settings) -> MosaicResult:
    tile_range, bounds_3857, _width, _height, tile_count = _tile_grid_for_bbox(_bbox(), settings.zoom)
    return MosaicResult(
        identifier=release.identifier,
        release_date=str(release.release_date),
        zoom=settings.zoom,
        tile_count=tile_count,
        available_tile_count=tile_count,
        missing_tile_count=0,
        tile_range=tile_range,
        bounds_3857=bounds_3857,
        png_path=path,
        geotiff_path=path,
        valid_mask_path=mask,
        shared_cache_dir=path.parent,
        cache_key="wayback-cache-key",
        materialized_in_request_dir=False,
        source_id=release.identifier,
        effective_date=str(release.release_date),
        metadata={"cache_hit": True},
    )


def _expected_key(settings: Settings, release: WaybackRelease) -> tuple[str, dict[str, object]]:
    tile_range, bounds_3857, _width, _height, _tile_count = _tile_grid_for_bbox(_bbox(), settings.zoom)
    payload = _reference_key_payload(
        release=release,
        normalized_aoi=_aoi(),
        settings=settings,
        zoom=settings.zoom,
        tile_range=tile_range,
        bounds_3857=bounds_3857,
    )
    return build_reference_imagery_key(payload), payload


def test_missing_canonical_cog_falls_back_and_promotes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    source, mask, *_ = _prepared_source(tmp_path, settings, release)
    calls = {"count": 0}

    def fake_download(*_args, **_kwargs) -> MosaicResult:
        calls["count"] += 1
        return _scene(source, mask, release, settings)

    monkeypatch.setattr(inference_reference_imagery, "download_wayback_mosaic", fake_download)

    result = get_or_create_inference_reference_imagery(
        release=release,
        normalized_aoi=_aoi(),
        bbox=_bbox(),
        settings=settings,
        zoom=settings.zoom,
        available_tiles=None,
        source_role="t1",
        out_dir=tmp_path / "request",
    )

    reference_key, _payload = _expected_key(settings, release)
    assert calls["count"] == 1
    assert result.metadata is not None
    assert result.metadata["imagery_source_mode"] == "wayback_mosaic_fallback"
    assert result.metadata["reference_imagery_key"] == reference_key
    assert result.metadata["canonical_cog_promoted"] is True
    assert reference_imagery_cache_cog_path(settings.reference_imagery_cache_dir, reference_key).is_file()
    assert (reference_imagery_cache_cog_path(settings.reference_imagery_cache_dir, reference_key).parent / "valid_mask.tif").is_file()


def test_existing_valid_canonical_cog_is_selected_for_inference(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    source, mask, *_ = _prepared_source(tmp_path, settings, release)
    reference_key, payload = _expected_key(settings, release)
    canonical = reference_imagery_cache_cog_path(settings.reference_imagery_cache_dir, reference_key)
    metadata_path = reference_imagery_cache_metadata_path(settings.reference_imagery_cache_dir, reference_key)
    ensure_reference_imagery_cog(source, canonical, valid_mask_path=mask, aoi_geojson=_aoi(), release_identifier=release.identifier)
    write_reference_imagery_cache_metadata(
        metadata_path,
        build_reference_imagery_cache_metadata(reference_imagery_key=reference_key, key_payload=payload, canonical_cog_path=canonical),
    )

    def fail_download(*_args, **_kwargs) -> MosaicResult:
        raise AssertionError("download_wayback_mosaic should not be called for a valid canonical COG")

    monkeypatch.setattr(inference_reference_imagery, "download_wayback_mosaic", fail_download)

    result = get_or_create_inference_reference_imagery(
        release=release,
        normalized_aoi=_aoi(),
        bbox=_bbox(),
        settings=settings,
        zoom=settings.zoom,
        available_tiles=None,
        source_role="t2",
        out_dir=tmp_path / "request",
    )

    assert result.geotiff_path == canonical
    assert result.metadata is not None
    assert result.metadata["imagery_source_mode"] == "canonical_cog"
    assert result.metadata["valid_mask_source"] in {"alpha_band", "internal_mask", "valid_mask_tif"}


def test_invalid_canonical_cog_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    source, mask, *_ = _prepared_source(tmp_path, settings, release)
    reference_key, payload = _expected_key(settings, release)
    canonical = reference_imagery_cache_cog_path(settings.reference_imagery_cache_dir, reference_key)
    metadata_path = reference_imagery_cache_metadata_path(settings.reference_imagery_cache_dir, reference_key)
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_bytes(b"not a tif")
    write_reference_imagery_cache_metadata(
        metadata_path,
        build_reference_imagery_cache_metadata(reference_imagery_key=reference_key, key_payload=payload, canonical_cog_path=source),
    )
    calls = {"count": 0}

    def fake_download(*_args, **_kwargs) -> MosaicResult:
        calls["count"] += 1
        return _scene(source, mask, release, settings)

    monkeypatch.setattr(inference_reference_imagery, "download_wayback_mosaic", fake_download)

    result = get_or_create_inference_reference_imagery(
        release=release,
        normalized_aoi=_aoi(),
        bbox=_bbox(),
        settings=settings,
        zoom=settings.zoom,
        available_tiles=None,
        source_role="t1",
        out_dir=tmp_path / "request",
    )

    assert calls["count"] == 1
    assert result.metadata is not None
    assert result.metadata["imagery_source_mode"] == "wayback_mosaic_fallback"
    assert result.metadata["fallback_reason"] == "read_failed"


def test_cog_validation_rejects_missing_crs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    tile_range, bounds_3857, width, height, _tile_count = _tile_grid_for_bbox(_bbox(), settings.zoom)
    source = _write_rgb(tmp_path / "missing-crs.tif", bounds_3857=bounds_3857, width=width, height=height, crs=None)
    reference_key, payload = _expected_key(settings, release)
    metadata_path = tmp_path / "metadata.json"
    write_reference_imagery_cache_metadata(
        metadata_path,
        build_reference_imagery_cache_metadata(reference_imagery_key=reference_key, key_payload=payload, canonical_cog_path=source),
    )

    result = validate_canonical_cog_for_inference(
        canonical_cog_path=source,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=payload,
        normalized_aoi=_aoi(),
    )

    assert result.valid is False
    assert result.reason in {"missing_crs", "read_failed"}


def test_cog_validation_rejects_identity_transform(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    path = tmp_path / "identity.tif"
    with rasterio.open(path, "w", driver="GTiff", width=256, height=256, count=3, dtype="uint8", crs="EPSG:3857", transform=from_origin(0, 0, 1, -1)) as dst:
        dst.write(np.zeros((3, 256, 256), dtype=np.uint8))
    reference_key, payload = _expected_key(settings, release)
    metadata_path = tmp_path / "metadata.json"
    write_reference_imagery_cache_metadata(
        metadata_path,
        build_reference_imagery_cache_metadata(reference_imagery_key=reference_key, key_payload=payload, canonical_cog_path=path),
    )

    result = validate_canonical_cog_for_inference(
        canonical_cog_path=path,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=payload,
        normalized_aoi=_aoi(),
    )

    assert result.valid is False
    assert result.reason in {"identity_transform", "grid_bounds_mismatch"}


def test_cog_validation_rejects_wrong_release_metadata(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    source, mask, *_ = _prepared_source(tmp_path, settings, release)
    reference_key, payload = _expected_key(settings, release)
    canonical = tmp_path / "reference_imagery_cog.tif"
    ensure_reference_imagery_cog(source, canonical, valid_mask_path=mask, aoi_geojson=_aoi(), release_identifier=release.identifier)
    wrong_payload = dict(payload, release_identifier="WB_2025_R03")
    metadata_path = tmp_path / "metadata.json"
    write_reference_imagery_cache_metadata(
        metadata_path,
        build_reference_imagery_cache_metadata(reference_imagery_key=reference_key, key_payload=wrong_payload, canonical_cog_path=canonical),
    )

    result = validate_canonical_cog_for_inference(
        canonical_cog_path=canonical,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=payload,
        normalized_aoi=_aoi(),
    )

    assert result.valid is False
    assert result.reason == "release_identifier_mismatch"


def test_valid_mask_is_loaded_from_existing_sidecar(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    source, mask, *_ = _prepared_source(tmp_path, settings, release)
    reference_key, payload = _expected_key(settings, release)
    canonical = tmp_path / "reference_imagery_cog.tif"
    ensure_reference_imagery_cog(source, canonical, valid_mask_path=mask, aoi_geojson=_aoi(), release_identifier=release.identifier)
    _tile_range, bounds_3857, width, height, _tile_count = _tile_grid_for_bbox(_bbox(), settings.zoom)
    sidecar = _write_mask(canonical.with_name("valid_mask.tif"), bounds_3857=bounds_3857, width=width, height=height)
    metadata_path = tmp_path / "metadata.json"
    write_reference_imagery_cache_metadata(
        metadata_path,
        build_reference_imagery_cache_metadata(reference_imagery_key=reference_key, key_payload=payload, canonical_cog_path=canonical),
    )

    result = validate_canonical_cog_for_inference(
        canonical_cog_path=canonical,
        metadata_path=metadata_path,
        expected_reference_imagery_key=reference_key,
        expected_key_payload=payload,
        normalized_aoi=_aoi(),
    )

    assert result.valid is True
    assert result.valid_mask_path == sidecar
    assert result.valid_mask_source == "valid_mask_tif"


def test_mosaic_result_contains_required_diagnostics(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    release = _release()
    source, mask, *_ = _prepared_source(tmp_path, settings, release)
    monkeypatch.setattr(inference_reference_imagery, "download_wayback_mosaic", lambda *_args, **_kwargs: _scene(source, mask, release, settings))

    result = get_or_create_inference_reference_imagery(
        release=release,
        normalized_aoi=_aoi(),
        bbox=_bbox(),
        settings=settings,
        zoom=settings.zoom,
        available_tiles=None,
        source_role="t1",
        out_dir=tmp_path / "request",
    )

    assert result.geotiff_path.is_file()
    assert result.valid_mask_path.is_file()
    assert result.metadata is not None
    assert result.metadata["imagery_source_mode"] in {"canonical_cog", "wayback_mosaic_fallback"}
    assert result.metadata["reference_imagery_key"]
    assert "canonical_cog_validation" in result.metadata
