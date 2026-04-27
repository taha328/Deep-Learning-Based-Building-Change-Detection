from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from src.api.routes.health import database_health
from src.config import Settings, get_settings
from src.db.session import session_scope
from src.repositories.temporal_project_repository import get_project, list_projects, save_project
from src.schemas import TemporalArtifactEntry, TemporalMilestone, TemporalMilestoneMetrics, TemporalProject


def _sample_project(tmp_path) -> TemporalProject:
    return TemporalProject(
        project_id="temporal-db-test",
        name="Temporal DB Test",
        project_dir=str(tmp_path / "temporal-db-test"),
        semantics="expansion_only",
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[
                [-7.0, 33.0],
                [-6.999, 33.0],
                [-6.999, 33.001],
                [-7.0, 33.001],
                [-7.0, 33.0],
            ]],
        },
        milestones=[
            TemporalMilestone(
                release_identifier="WB_2024_R01",
                release_date="2024-01-01",
                status="complete",
                source_mode="manual_override",
                manual_override_geojson={
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "properties": {},
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[
                                    [-7.0, 33.0],
                                    [-6.9995, 33.0],
                                    [-6.9995, 33.0005],
                                    [-7.0, 33.0005],
                                    [-7.0, 33.0],
                                ]],
                            },
                        }
                    ],
                },
                metrics=TemporalMilestoneMetrics(
                    added_area_m2=12.5,
                    total_area_m2=25.0,
                    additions_feature_count=1,
                    effective_feature_count=1,
                    building_level_available=True,
                    added_block_count=1,
                    cumulative_block_count=1,
                    added_block_area_m2=12.5,
                    cumulative_block_area_m2=25.0,
                    growth_envelope_area_m2=30.0,
                ),
                artifacts=[
                    TemporalArtifactEntry(
                        name="WB_2024_R01_manual_override",
                        path=str(tmp_path / "manual_override.geojson"),
                        media_type="application/geo+json",
                        description="Manual milestone override",
                    )
                ],
            )
        ],
        created_at="2026-04-27T00:00:00Z",
        updated_at="2026-04-27T00:00:00Z",
    )


def test_database_health_disabled_for_filesystem_mode(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, persistence_backend="filesystem")

    result = database_health(settings)

    assert result["status"] == "disabled"
    assert result["persistence_backend"] == "filesystem"


_MIGRATED = False


def _postgres_settings(tmp_path) -> Settings:
    global _MIGRATED
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set")
    if not _MIGRATED:
        os.environ["DATABASE_URL"] = database_url
        get_settings.cache_clear()
        command.upgrade(Config("alembic.ini"), "head")
        _MIGRATED = True
    return Settings(
        runtime_cache_dir=tmp_path,
        persistence_backend="postgres",
        database_url=database_url,
    )


def test_database_health_postgres_when_test_database_is_available(tmp_path) -> None:
    settings = _postgres_settings(tmp_path)

    result = database_health(settings)

    assert result["status"] == "ok"
    assert result["database"] == "connected"
    assert result["postgis"] == "available"


def test_save_load_and_list_temporal_project_in_postgres_mode(tmp_path) -> None:
    settings = _postgres_settings(tmp_path)
    project = _sample_project(tmp_path)

    with session_scope(settings) as session:
        session.execute(text("DELETE FROM projects WHERE project_id = :project_id"), {"project_id": project.project_id})
        save_project(project, settings=settings, session=session)

    loaded = get_project(project.project_id, settings=settings)
    summaries = list_projects(settings=settings)

    assert loaded.project_id == project.project_id
    assert loaded.milestones[0].metrics is not None
    assert any(summary.project_id == project.project_id for summary in summaries)
