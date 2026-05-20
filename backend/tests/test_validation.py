from datetime import date

from src.config import Settings
from src.domain.wayback import WaybackRelease
from src.schemas import ValidationRequest
from src.services.validation import validate_request


def _sample_releases(settings: Settings) -> list[WaybackRelease]:
    return [
        WaybackRelease(
            identifier="WB_2022_R01",
            release_date=date(2022, 1, 1),
            label="2022-01-01 | WB_2022_R01",
            release_num=1,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.png",
        ),
        WaybackRelease(
            identifier="WB_2023_R01",
            release_date=date(2023, 1, 1),
            label="2023-01-01 | WB_2023_R01",
            release_num=1,
            tile_matrix_sets=(settings.tile_matrix_set,),
            resource_url_template="https://example.com/{TileMatrixSet}/{TileMatrix}/{TileRow}/{TileCol}.png",
        ),
    ]


def test_validation_rejects_reversed_releases() -> None:
    settings = Settings()
    releases = _sample_releases(settings)
    request = ValidationRequest(
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-7.0, 33.001], [-6.999, 33.001], [-6.999, 33.0], [-7.0, 33.0]]],
        },
        t1_release="WB_2023_R01",
        t2_release="WB_2022_R01",
        mode="fast_preview",
    )
    response, _ = validate_request(request, releases=releases, settings=settings)
    assert response.valid is False
    assert any("chronologically earlier" in message for message in response.blocking_errors)


def test_validation_warns_when_aoi_exceeds_inference_patch_guidance() -> None:
    settings = Settings(
        preview_limits={
            "name": "fast_preview",
            "label": "Fast Preview",
            "max_area_m2": 400_000.0,
            "max_scene_tiles": 64,
            "max_inference_patches_per_scene": 1,
        }
    )
    releases = _sample_releases(settings)
    request = ValidationRequest(
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-7.0, 33.005], [-6.995, 33.005], [-6.995, 33.0], [-7.0, 33.0]]],
        },
        t1_release="WB_2022_R01",
        t2_release="WB_2023_R01",
        mode="fast_preview",
    )
    response, _ = validate_request(request, releases=releases, settings=settings)
    response, prepared = validate_request(request, releases=releases, settings=settings, remote_patch_budget_enabled=True)
    assert response.valid is True
    assert prepared is not None
    assert any("inference patches per date" in message for message in response.warnings)


def test_validation_rejects_negative_old_building_mask_dilation() -> None:
    settings = Settings()
    releases = _sample_releases(settings)
    request = ValidationRequest(
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-7.0, 33.001], [-6.999, 33.001], [-6.999, 33.0], [-7.0, 33.0]]],
        },
        t1_release="WB_2022_R01",
        t2_release="WB_2023_R01",
        mode="fast_preview",
        old_building_mask_dilation_pixels=-1,
    )
    response, _ = validate_request(request, releases=releases, settings=settings)
    assert response.valid is False
    assert any("old_building_mask_dilation_pixels" in message for message in response.blocking_errors)


def test_validation_allows_large_aoi() -> None:
    settings = Settings()
    releases = _sample_releases(settings)
    request = ValidationRequest(
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-7.0, 33.05], [-6.95, 33.05], [-6.95, 33.0], [-7.0, 33.0]]],
        },
        t1_release="WB_2022_R01",
        t2_release="WB_2023_R01",
        mode="full_run",
    )

    response, prepared = validate_request(
        request,
        releases=releases,
        settings=settings,
        remote_patch_budget_enabled=False,
    )

    assert response.valid is True
    assert prepared is not None
    assert response.estimated_area_m2 > settings.full_limits.max_area_m2
    assert response.estimated_tile_count_t1 > settings.full_limits.max_scene_tiles
    assert any("AOI area" in message and "remains allowed" in message for message in response.warnings)
    assert any("tiles per date" in message and "remains allowed" in message for message in response.warnings)
