from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.domain.wayback import WaybackRelease
from src.schemas import TemporalProject, ValidationRequest, validate_stored_temporal_project
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
        "coordinates": [[[-7.60, 33.40], [-7.59, 33.40], [-7.59, 33.41], [-7.60, 33.41], [-7.60, 33.40]]],
    }


def test_validation_request_rejects_removed_latest_source() -> None:
    with pytest.raises(ValidationError, match="latest_source"):
        ValidationRequest(
            aoi_geojson=_aoi(),
            t1_release="WB_2024_R02",
            t2_release="WB_2026_R03",
            mode="full_run",
            latest_source="mapbox_current",
        )


def test_temporal_project_rejects_removed_latest_source() -> None:
    with pytest.raises(ValidationError, match="latest_source"):
        TemporalProject(
            project_id="removed-latest-source",
            name="Removed latest source",
            aoi_geojson=_aoi(),
            milestones=[],
            latest_source="esri_wayback",
            created_at="2026-06-12T00:00:00Z",
            updated_at="2026-06-12T00:00:00Z",
        )


def test_stored_project_migration_drops_removed_field_and_synthetic_milestone() -> None:
    project = validate_stored_temporal_project(
        {
            "project_id": "legacy-latest-source",
            "name": "Legacy latest source",
            "aoi_geojson": _aoi(),
            "milestones": [
                {"release_identifier": "WB_2024_R02"},
                {"release_identifier": "WB_2026_R03"},
                {"release_identifier": "mapbox.satellite", "release_date": "current_basemap"},
            ],
            "latest_source": "mapbox_current",
            "created_at": "2026-06-12T00:00:00Z",
            "updated_at": "2026-06-12T00:00:00Z",
        }
    )

    assert [milestone.release_identifier for milestone in project.milestones] == ["WB_2024_R02", "WB_2026_R03"]


def test_wayback_only_workflow_still_validates(tmp_path) -> None:
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
    assert all("Mapbox" not in message for message in [*response.warnings, *response.blocking_errors])
