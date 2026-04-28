from __future__ import annotations

import os
from contextlib import contextmanager
from types import SimpleNamespace
import uuid

import pytest
from alembic import command
from alembic.config import Config
from fastapi import HTTPException
from sqlalchemy import text

from src import core_api
from src.api.errors import raise_api_error
from src.api.routes import health as health_routes
from src.api.routes.health import database_health
from src.config import Settings, get_settings
from src.db.models import GeometryLayerRecord, ProjectRecord
from src.db.session import session_scope
from src.repositories import run_repository
from src.repositories.run_repository import build_detection_run_id, build_temporal_run_id, save_detection_run, save_temporal_run
from src.repositories.temporal_project_repository import get_project, list_projects, save_project
from src.schemas import (
    TemporalArtifactEntry,
    TemporalMilestone,
    TemporalMilestoneMetrics,
    TemporalOverrideRequest,
    TemporalProject,
    TemporalProjectRunResponse,
    RunRequest,
    RunResponse,
    SummaryStats,
)


TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
requires_test_database = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL not set")


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


def test_raise_api_error_uses_flat_detail_shape() -> None:
    with pytest.raises(HTTPException) as exc_info:
        raise_api_error(
            400,
            "invalid_request",
            "project_id is required",
            details={"field": "project_id"},
        )

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert detail["code"] == "invalid_request"
    assert detail["message"] == "project_id is required"
    assert detail["details"] == {"field": "project_id"}


def test_database_health_postgres_failure_uses_flat_error_shape(monkeypatch, tmp_path) -> None:
    @contextmanager
    def failing_session_scope(*_args, **_kwargs):
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(health_routes, "session_scope", failing_session_scope)
    settings = Settings(
        runtime_cache_dir=tmp_path,
        persistence_backend="postgres",
        database_url="postgresql+psycopg://building_change:building_change@localhost:5432/building_change",
    )

    with pytest.raises(HTTPException) as exc_info:
        database_health(settings)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "database_unavailable"
    assert "setup_postgis_db.py --migrate" in exc_info.value.detail["message"]
    assert exc_info.value.detail["details"]["persistence_backend"] == "postgres"


def test_temporal_run_ids_are_unique() -> None:
    run_id_one = build_temporal_run_id("temporal-project")
    run_id_two = build_temporal_run_id("temporal-project")

    assert run_id_one != run_id_two
    assert run_id_one.startswith("temporal-temporal-project-")
    assert run_id_two.startswith("temporal-temporal-project-")


def test_detection_fallback_run_ids_are_uuid_based() -> None:
    run_id_one = build_detection_run_id(None)
    run_id_two = build_detection_run_id(None)

    assert run_id_one != run_id_two
    assert run_id_one.startswith("detection-")
    assert run_id_two.startswith("detection-")
    assert len(run_id_one.removeprefix("detection-")) == 32
    assert len(run_id_two.removeprefix("detection-")) == 32
    assert build_detection_run_id("request-hash-123") == "request-hash-123"


class _FakeQuery:
    def __init__(self, value):
        self.value = value

    def filter(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return self.value


class _FakeRunSession:
    def __init__(self, project=None, existing_run=None):
        self.project = project
        self.existing_run = existing_run
        self.added = []
        self.flush_count = 0

    def query(self, model):
        if model is ProjectRecord:
            return _FakeQuery(self.project)
        if model is run_repository.RunRecord:
            return _FakeQuery(self.existing_run)
        raise AssertionError(f"Unexpected query model: {model!r}")

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flush_count += 1
        assert self.added, "flush called before run was added"
        assert getattr(self.added[-1], "status", None) is not None, "run.status must be set before flush"


def _sample_run_request() -> RunRequest:
    return RunRequest(
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
        t1_release="WB_2018_R01",
        t2_release="WB_2020_R01",
        mode="fast_preview",
    )


def _sample_run_response() -> RunResponse:
    return RunResponse(success=True, summary=None)


def _sample_temporal_project(project_id: str = "temporal-run-test") -> TemporalProject:
    return TemporalProject(
        project_id=project_id,
        name="Temporal Run Test",
        project_dir=None,
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
        milestones=[],
        created_at="2026-04-27T00:00:00Z",
        updated_at="2026-04-27T00:00:00Z",
    )


def _sample_temporal_response(project: TemporalProject) -> TemporalProjectRunResponse:
    return TemporalProjectRunResponse(success=True, project=project)


def test_detection_run_creation_sets_status_before_flush(monkeypatch) -> None:
    session = _FakeRunSession()
    monkeypatch.setattr(run_repository, "replace_run_artifacts", lambda *args, **kwargs: None)

    response = save_detection_run(
        request=_sample_run_request(),
        response=_sample_run_response(),
        session=session,
    )

    assert response.success is True
    assert session.flush_count == 1
    assert session.added[0].run_kind == "detection"
    assert session.added[0].status is not None


def test_detection_fallback_run_ids_are_uuid_based_for_saved_runs(monkeypatch) -> None:
    monkeypatch.setattr(run_repository, "replace_run_artifacts", lambda *args, **kwargs: None)
    run_ids: list[str] = []

    for _ in range(2):
        session = _FakeRunSession()
        save_detection_run(
            request=_sample_run_request(),
            response=_sample_run_response(),
            session=session,
        )
        run_ids.append(session.added[0].run_id)

    assert run_ids[0] != run_ids[1]
    assert run_ids[0].startswith("detection-")
    assert run_ids[1].startswith("detection-")


def test_temporal_run_creation_sets_status_before_flush() -> None:
    project = _sample_temporal_project()
    session = _FakeRunSession(project=SimpleNamespace(id=uuid.uuid4(), project_id=project.project_id))

    response = save_temporal_run(
        project_id=project.project_id,
        response=_sample_temporal_response(project),
        session=session,
    )

    assert response.success is True
    assert session.flush_count == 1
    assert session.added[0].run_kind == "temporal_project"
    assert session.added[0].status is not None


def test_temporal_run_ids_are_unique_for_saved_runs() -> None:
    project = _sample_temporal_project()
    run_ids: list[str] = []

    for _ in range(2):
        session = _FakeRunSession(project=SimpleNamespace(id=uuid.uuid4(), project_id=project.project_id))
        save_temporal_run(
            project_id=project.project_id,
            response=_sample_temporal_response(project),
            session=session,
        )
        run_ids.append(session.added[0].run_id)

    assert run_ids[0] != run_ids[1]
    assert run_ids[0].startswith("temporal-")
    assert run_ids[1].startswith("temporal-")


def test_import_temporal_override_api_mirrors_project_in_postgres_mode(monkeypatch, tmp_path) -> None:
    project = _sample_project(tmp_path)
    response = TemporalProjectRunResponse(success=True, project=project)
    settings = Settings(runtime_cache_dir=tmp_path, persistence_backend="postgres")
    saved: dict[str, object] = {}

    def fake_import_override(request: TemporalOverrideRequest, *, settings: Settings) -> TemporalProjectRunResponse:
        assert request.project_id == project.project_id
        assert settings.persistence_backend == "postgres"
        return response

    def fake_save_project(project_arg: TemporalProject, *, settings: Settings, session=None) -> TemporalProject:
        saved["project"] = project_arg
        saved["settings"] = settings
        return project_arg

    monkeypatch.setattr(core_api, "import_temporal_override", fake_import_override)
    monkeypatch.setattr("src.repositories.temporal_project_repository.save_project", fake_save_project)

    result = core_api.import_temporal_override_api(
        TemporalOverrideRequest(
            project_id=project.project_id,
            release_identifier=project.milestones[0].release_identifier,
            override_geojson=project.milestones[0].manual_override_geojson or {"type": "FeatureCollection", "features": []},
        ),
        settings=settings,
    )

    assert result.success is True
    assert saved["project"] == project
    assert saved["settings"] == settings


@requires_test_database
def test_import_temporal_override_api_persists_project_payload_to_postgres(monkeypatch, tmp_path) -> None:
    settings = _postgres_settings(tmp_path)
    project = _sample_project(tmp_path)
    with session_scope(settings) as session:
        session.execute(text("DELETE FROM projects WHERE project_id = :project_id"), {"project_id": project.project_id})
        save_project(project, settings=settings, session=session)

    updated_project = project.model_copy(deep=True)
    updated_project.milestones[0].manual_override_geojson = {
        "type": "FeatureCollection",
        "features": [],
    }
    response = TemporalProjectRunResponse(success=True, project=updated_project)

    def fake_import_override(request: TemporalOverrideRequest, *, settings: Settings) -> TemporalProjectRunResponse:
        assert request.project_id == project.project_id
        assert settings.persistence_backend == "postgres"
        return response

    monkeypatch.setattr(core_api, "import_temporal_override", fake_import_override)

    result = core_api.import_temporal_override_api(
        TemporalOverrideRequest(
            project_id=project.project_id,
            release_identifier=project.milestones[0].release_identifier,
            override_geojson={"type": "FeatureCollection", "features": []},
        ),
        settings=settings,
    )

    persisted = get_project(project.project_id, settings=settings)
    assert result.success is True
    assert persisted.milestones[0].manual_override_geojson == {"type": "FeatureCollection", "features": []}


_MIGRATED = False


def _postgres_settings(tmp_path) -> Settings:
    global _MIGRATED
    database_url = TEST_DATABASE_URL
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


@requires_test_database
def test_database_health_postgres_when_test_database_is_available(tmp_path) -> None:
    settings = _postgres_settings(tmp_path)

    result = database_health(settings)

    assert result["status"] == "ok"
    assert result["database"] == "connected"
    assert result["postgis"] == "available"


@requires_test_database
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


@requires_test_database
def test_geometry_layers_persist_for_saved_project_in_postgres_mode(tmp_path) -> None:
    settings = _postgres_settings(tmp_path)
    project = _sample_project(tmp_path)

    with session_scope(settings) as session:
        session.execute(text("DELETE FROM projects WHERE project_id = :project_id"), {"project_id": project.project_id})
        save_project(project, settings=settings, session=session)
        project_record = session.query(ProjectRecord).filter(ProjectRecord.project_id == project.project_id).one()
        layers = (
            session.query(GeometryLayerRecord.layer_kind)
            .filter(GeometryLayerRecord.project_db_id == project_record.id)
            .all()
        )

    layer_kinds = {layer_kind for (layer_kind,) in layers}
    assert "aoi" in layer_kinds
    assert "manual_override" in layer_kinds
