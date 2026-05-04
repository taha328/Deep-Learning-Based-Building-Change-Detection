from __future__ import annotations

import io
import json

from PIL import Image
import pytest
import rasterio
import requests

from src.config import Settings
from src.domain.artifact_manifest import build_manifest, iter_exportable_artifacts, register_artifact, write_manifest
from src.domain.mapbox_current import (
    MAPBOX_SOURCE_ID,
    MapboxCurrentImageryError,
    build_mapbox_current_cache_key,
    build_mapbox_satellite_tile_url,
    download_mapbox_current_mosaic,
)


def _tile_bytes(color: tuple[int, int, int] = (40, 80, 120)) -> bytes:
    image = Image.new("RGB", (256, 256), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


class _FakeResponse:
    status_code = 200
    content = _tile_bytes()

    def raise_for_status(self) -> None:
        return None


def _settings(tmp_path, *, token: str | None = "pk.test") -> Settings:
    return Settings(
        runtime_cache_dir=tmp_path / "runtime",
        mapbox_access_token=token,
        mapbox_current_imagery_enabled=True,
        mapbox_current_imagery_default_zoom=19,
        mapbox_current_imagery_max_zoom=19,
        mapbox_current_imagery_max_tiles=4,
        download_workers=1,
    )


def test_mapbox_tile_url_uses_official_v4_shape() -> None:
    url = build_mapbox_satellite_tile_url(
        tileset="mapbox.satellite",
        zoom=19,
        x=1,
        y=2,
        image_format="jpg90",
        access_token="pk.secret",
    )

    assert url == "https://api.mapbox.com/v4/mapbox.satellite/19/1/2.jpg90?access_token=pk.secret"


def test_mapbox_cache_key_is_deterministic_and_changes_with_zoom() -> None:
    bbox = {"west": -7.6, "south": 33.4, "east": -7.59, "north": 33.41}
    first = build_mapbox_current_cache_key(
        bbox=bbox,
        zoom=19,
        tileset="mapbox.satellite",
        image_format="jpg90",
        tile_range=(1, 1, 2, 2),
    )
    second = build_mapbox_current_cache_key(
        bbox=bbox,
        zoom=19,
        tileset="mapbox.satellite",
        image_format="jpg90",
        tile_range=(1, 1, 2, 2),
    )
    changed = build_mapbox_current_cache_key(
        bbox=bbox,
        zoom=18,
        tileset="mapbox.satellite",
        image_format="jpg90",
        tile_range=(1, 1, 2, 2),
    )

    assert first == second
    assert first != changed


def test_mapbox_missing_token_fails_clearly(tmp_path) -> None:
    settings = _settings(tmp_path, token=None)

    with pytest.raises(MapboxCurrentImageryError, match="MAPBOX_ACCESS_TOKEN"):
        download_mapbox_current_mosaic(
            {"west": 0, "south": 0, "east": 1, "north": 1},
            settings=settings,
        )


def test_mapbox_network_error_does_not_leak_token(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path, token="pk.secret")
    monkeypatch.setattr("src.domain.mapbox_current.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))

    def fail_get(url, timeout):
        raise requests.ConnectionError("failed url with access_token=pk.secret")

    monkeypatch.setattr("src.domain.mapbox_current.requests.get", fail_get)

    with pytest.raises(MapboxCurrentImageryError) as exc_info:
        download_mapbox_current_mosaic(
            {"west": 0, "south": 0, "east": 1, "north": 1},
            settings=settings,
        )

    assert "pk.secret" not in str(exc_info.value)
    assert "access_token" not in str(exc_info.value)


def test_mapbox_cache_miss_writes_georeferenced_tif_and_cache_hit_avoids_network(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    calls: list[str] = []

    monkeypatch.setattr("src.domain.mapbox_current.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))
    monkeypatch.setattr("src.domain.mapbox_current.requests.get", lambda url, timeout: calls.append(url) or _FakeResponse())

    first = download_mapbox_current_mosaic(
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
    )
    second = download_mapbox_current_mosaic(
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
    )

    assert first.provider == "mapbox"
    assert first.source_type == "current_basemap"
    assert first.source_id == MAPBOX_SOURCE_ID
    assert first.capture_date_known is False
    assert second.metadata and second.metadata["cache_hit"] is True
    assert len(calls) == 1
    with rasterio.open(first.geotiff_path) as src:
        assert src.crs.to_string() == "EPSG:3857"
        assert src.width == 256
        assert src.height == 256
        assert src.count == 3
        assert src.transform is not None


def test_mapbox_manifest_metadata_does_not_include_token_and_is_non_exportable(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    request_dir = tmp_path / "runtime" / "requests" / "run-1"
    request_dir.mkdir(parents=True)
    monkeypatch.setattr("src.domain.mapbox_current.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))
    monkeypatch.setattr("src.domain.mapbox_current.requests.get", lambda url, timeout: _FakeResponse())

    scene = download_mapbox_current_mosaic(
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
    )
    final_path = request_dir / "building_change_polygons.geojson"
    final_path.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    manifest = build_manifest(
        "run-1",
        request_dir,
        [
            register_artifact(
                path=scene.geotiff_path,
                resolved_path=scene.geotiff_path,
                artifact_type="source",
                purpose="Mapbox source raster",
                format="tif",
                keep_policy="cache",
                include_in_export=False,
                storage="shared_cache",
                request_dir=request_dir,
                run_id="run-1",
                cache_key=scene.cache_key,
                metadata={
                    "provider": "mapbox",
                    "source_type": "current_basemap",
                    "source_id": MAPBOX_SOURCE_ID,
                    "capture_date_known": False,
                    "dominant_src_date": None,
                    "attribution_required": True,
                },
            )
        ],
    )
    manifest_path = write_manifest(request_dir, manifest)

    payload = manifest_path.read_text()
    assert "pk.test" not in payload
    assert str(scene.geotiff_path) in payload
    assert iter_exportable_artifacts(request_dir) == [final_path]
