from __future__ import annotations

from datetime import date

from src.config import Settings
from src.domain.wayback import WaybackRelease
from src.schemas import ValidationRequest
from src.services.validation import validate_request


def _release(identifier: str, release_date: date, release_num: int) -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=release_date,
        label=identifier,
        release_num=release_num,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://example.com/{TileMatrix}/{TileRow}/{TileCol}.png",
    )


def _aoi() -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [-7.60, 33.40],
                [-7.59, 33.40],
                [-7.59, 33.41],
                [-7.60, 33.41],
                [-7.60, 33.40],
            ]
        ],
    }


def test_mapbox_current_warns_for_latest_milestone(tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="pk.test",
    )
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2026_R03", date(2026, 3, 25), 2),
    ]

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
            latest_source="mapbox_current",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is True
    assert prepared is not None
    assert prepared.latest_source == "mapbox_current"
    assert any("Exact capture date is not guaranteed" in warning for warning in response.warnings)


def test_mapbox_current_requires_token_when_enabled(tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        mapbox_current_imagery_enabled=True,
        mapbox_access_token=None,
    )
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2026_R03", date(2026, 3, 25), 2),
    ]

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
            latest_source="mapbox_current",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is False
    assert prepared is None
    assert any("MAPBOX_ACCESS_TOKEN" in message for message in response.blocking_errors)


def test_mapbox_current_rejected_for_non_latest_milestone(tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="pk.test",
    )
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2025_R03", date(2025, 3, 27), 2),
        _release("WB_2026_R03", date(2026, 3, 25), 3),
    ]

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2025_R03",
            mode="full_run",
            latest_source="mapbox_current",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is False
    assert prepared is None
    assert any("newest/latest milestone" in message for message in response.blocking_errors)


def test_existing_esri_only_workflow_still_validates(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2026_R03", date(2026, 3, 25), 2),
    ]

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is True
    assert prepared is not None
    assert prepared.latest_source == "esri_wayback"


def test_mapbox_546_tiles_allowed_with_configured_limit(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="pk.test",
        mapbox_max_tiles_per_request=1024,
    )
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2026_R03", date(2026, 3, 25), 2),
    ]
    monkeypatch.setattr("src.services.validation.scene_tile_count", lambda bbox, zoom: 546)
    monkeypatch.setattr(
        "src.services.validation.intersecting_tiles_for_aoi",
        lambda aoi, *, bbox, zoom: (frozenset((index, 0) for index in range(546)), 546),
    )

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
            latest_source="mapbox_current",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is True
    assert prepared is not None
    assert any("AOI will download 546 Mapbox tiles at z=19" in warning for warning in response.warnings)


def test_mapbox_546_tiles_blocked_with_limit_256(monkeypatch, tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        mapbox_current_imagery_enabled=True,
        mapbox_access_token="pk.test",
        mapbox_max_tiles_per_request=256,
    )
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2026_R03", date(2026, 3, 25), 2),
    ]
    monkeypatch.setattr("src.services.validation.scene_tile_count", lambda bbox, zoom: 546)
    monkeypatch.setattr(
        "src.services.validation.intersecting_tiles_for_aoi",
        lambda aoi, *, bbox, zoom: (frozenset((index, 0) for index in range(546)), 546),
    )

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
            latest_source="mapbox_current",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is False
    assert prepared is None
    assert any("AOI would download 546 Mapbox tiles at z=19, exceeding the limit of 256." in message for message in response.blocking_errors)


def test_wayback_validation_unchanged_even_with_large_tile_count(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", mapbox_max_tiles_per_request=256)
    releases = [
        _release("WB_2024_R02", date(2024, 3, 7), 1),
        _release("WB_2026_R03", date(2026, 3, 25), 2),
    ]
    monkeypatch.setattr("src.services.validation.scene_tile_count", lambda bbox, zoom: 546)

    response, prepared = validate_request(
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
            latest_source="esri_wayback",
        ),
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is True
    assert prepared is not None
    assert all("Mapbox tiles" not in message for message in response.blocking_errors)
