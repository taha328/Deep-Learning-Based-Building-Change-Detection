from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest

from src.api.routes.health import jobs_health, redis_health
from src.config import Settings
from src.core_api import _temporal_pair_progress_details
from src.db.models import JobRecord, ProjectRecord
from src.jobs import tasks as job_tasks
from src.jobs import service as jobs_service
from src.jobs.exceptions import JobsDisabledError
from src.jobs.service import cancel_job, start_detection_job, start_temporal_project_job
from src.repositories.job_repository import mark_job_completed, mark_job_failed
from src.schemas import (
    ArtifactEntry,
    DiagnosticMetadata,
    RunRequest,
    RunResponse,
    SummaryStats,
    TemporalMilestone,
    TemporalMilestoneMetrics,
    TemporalProject,
    TemporalProjectRunRequest,
    TemporalProjectRunResponse,
)
from src.jobs.tasks import _log_worker_effective_backend


def test_temporal_pair_progress_details_adds_dates_and_preserves_tile_fields() -> None:
    details = _temporal_pair_progress_details(
        details={
            "processed_tiles": 36,
            "total_tiles": 100,
            "processed_tile_count": 12,
            "total_tile_count": 20,
        },
        pair_fraction=0.36,
        pair_stage="Running tiled local BANDON change detection",
        current_pair_index=2,
        total_pair_count=4,
        from_release_identifier="WB_2025_R03",
        to_release_identifier="WB_2026_R05",
        release_dates={
            "WB_2025_R03": "2025-03-27",
            "WB_2026_R05": "2026-05-28",
        },
    )

    assert details["temporal_progress_kind"] == "active_pair"
    assert details["current_pair_index"] == 2
    assert details["total_pair_count"] == 4
    assert details["pair_fraction"] == 0.36
    assert details["from_release_identifier"] == "WB_2025_R03"
    assert details["to_release_identifier"] == "WB_2026_R05"
    assert details["from_release_date"] == "2025-03-27"
    assert details["to_release_date"] == "2026-05-28"
    assert details["processed_tiles"] == 36
    assert details["total_tiles"] == 100
    assert details["processed_tile_count"] == 12
    assert details["total_tile_count"] == 20


class _FakeQuery:
    def __init__(self, value):
        self.value = value

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def one_or_none(self):
        return self.value

    def first(self):
        return self.value

    def all(self):
        if self.value is None:
            return []
        if isinstance(self.value, list):
            return self.value
        return [self.value]


class _FakeJobSession:
    def __init__(self, project=None, job=None):
        self.project = project
        self.job = job
        self.added = []
        self.flush_count = 0

    def query(self, model):
        if model is ProjectRecord:
            return _FakeQuery(self.project)
        if model is JobRecord:
            return _FakeQuery(self.job)
        raise AssertionError(f"Unexpected query model: {model!r}")

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, JobRecord):
            self.job = obj

    def flush(self):
        self.flush_count += 1
        assert self.added, "flush called before a job was added"
        assert getattr(self.added[-1], "status", None) is not None


@contextmanager
def _job_session_scope(session):
    yield session


def _sample_request() -> RunRequest:
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


def test_jobs_health_disabled_when_feature_is_off(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, jobs_enabled=False)

    result = jobs_health(settings)

    assert result["status"] == "disabled"
    assert result["jobs_enabled"] is False


def test_redis_health_disabled_when_feature_is_off(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, jobs_enabled=False)

    result = redis_health(settings)

    assert result["status"] == "disabled"
    assert result["redis"] == "not_configured"


def test_start_detection_job_enqueues_and_persists(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession()
    settings = Settings(
        runtime_cache_dir=tmp_path,
        jobs_enabled=True,
        redis_url="redis://localhost:6379/0",
    )

    monkeypatch.setattr(jobs_service, "assert_redis_available", lambda settings: None)
    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(jobs_service.celery_app, "send_task", lambda *args, **kwargs: SimpleNamespace(id="celery-task-1"))

    response = start_detection_job(_sample_request(), settings=settings)

    assert response.job_id.startswith("job-")
    assert response.celery_task_id == "celery-task-1"
    assert session.job is not None
    assert session.job.status == "queued"
    assert session.job.celery_task_id == "celery-task-1"


def test_start_temporal_job_requires_existing_project(monkeypatch, tmp_path, caplog) -> None:
    session = _FakeJobSession(project=SimpleNamespace(id=uuid.uuid4(), project_id="temporal-test"))
    settings = Settings(
        runtime_cache_dir=tmp_path,
        jobs_enabled=True,
        redis_url="redis://localhost:6379/0",
    )
    launch_lock = settings.wayback_tile_preflight_cache_dir / "launch.json.lock"
    launch_lock.mkdir()

    monkeypatch.setattr(jobs_service, "assert_redis_available", lambda settings: None)
    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(
        jobs_service,
        "get_temporal_project",
        lambda *_args, **_kwargs: SimpleNamespace(milestones=[SimpleNamespace(release_identifier="WB_2026_R01")]),
    )
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        jobs_service.celery_app,
        "send_task",
        lambda *args, **kwargs: (sent.update(args=args, kwargs=kwargs) or SimpleNamespace(id="celery-task-2")),
    )

    with caplog.at_level("INFO"):
        response = start_temporal_project_job(
            "temporal-test",
            settings=settings,
            run_request=TemporalProjectRunRequest(change_threshold=0.3),
        )

    assert response.job_id.startswith("job-")
    assert response.celery_task_id == "celery-task-2"
    assert session.job is not None
    assert session.job.project_id == "temporal-test"
    assert session.job.project_db_id == session.project.id
    assert session.job.raw_request["change_threshold"] == 0.3
    assert sent["kwargs"]["kwargs"]["run_request_payload"] == {"change_threshold": 0.3}
    assert not launch_lock.exists()
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert f"TEMPORAL_JOB_THRESHOLD_RECEIVED projectId=temporal-test jobId={response.job_id} changeThreshold=0.3" in messages
    assert f"TEMPORAL_JOB_THRESHOLD_ENQUEUED projectId=temporal-test jobId={response.job_id} changeThreshold=0.3" in messages


def test_start_temporal_job_does_not_enqueue_unreloadable_project(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(project=SimpleNamespace(id=uuid.uuid4(), project_id="temporal-missing"))
    settings = Settings(runtime_cache_dir=tmp_path, jobs_enabled=True, redis_url="redis://localhost:6379/0")
    sent: list[object] = []

    monkeypatch.setattr(jobs_service, "assert_redis_available", lambda settings: None)
    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(
        jobs_service,
        "get_temporal_project",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("missing project.json")),
    )
    monkeypatch.setattr(jobs_service.celery_app, "send_task", lambda *args, **kwargs: sent.append((args, kwargs)))

    with pytest.raises(jobs_service.JobNotFoundError, match="not reloadable"):
        start_temporal_project_job("temporal-missing", settings=settings)

    assert sent == []
    assert session.job is None


def test_cancel_job_marks_request_and_revokes(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-1",
            job_kind="detection",
            status="queued",
            celery_task_id="task-1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    settings = Settings(runtime_cache_dir=tmp_path, jobs_enabled=True, redis_url="redis://localhost:6379/0")
    revoked: dict[str, object] = {}

    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(jobs_service.celery_app.control, "revoke", lambda task_id, terminate, signal: revoked.update({"task_id": task_id, "terminate": terminate, "signal": signal}))

    response = cancel_job("job-1", settings=settings)

    assert response.cancel_requested is True
    assert revoked["task_id"] == "task-1"
    assert revoked["terminate"] is False
    assert revoked["signal"] == "SIGTERM"


def test_mark_job_completed_clears_previous_error_fields(tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-failed-then-complete",
            job_kind="temporal_project",
            status="failed",
            progress=100,
            stage="failed",
            error_code="runtime_error",
            error_message="Previous run failed.",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    settings = Settings(runtime_cache_dir=tmp_path)

    record = mark_job_completed(
        job_id="job-failed-then-complete",
        result_run_id="run-1",
        raw_result={"success": True},
        settings=settings,
        session=session,  # type: ignore[arg-type]
    )

    assert record.status == "completed"
    assert record.error_code is None
    assert record.error_message is None


def test_mark_job_failed_with_session_does_not_construct_settings(monkeypatch) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-runtime-failure",
            job_kind="temporal_project",
            status="running",
            progress=25,
            stage="fetching_imagery",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )

    monkeypatch.setattr(
        "src.repositories.job_repository.Settings",
        lambda: pytest.fail("mark_job_failed must not construct Settings during failure persistence"),
    )

    record = mark_job_failed(
        job_id="job-runtime-failure",
        error_code="runtime_error",
        error_message="Original temporal failure",
        session=session,  # type: ignore[arg-type]
    )

    assert record.status == "failed"
    assert record.progress == 100
    assert record.stage == "failed"
    assert record.error_message == "Original temporal failure"
    assert record.completed_at is not None


def test_mark_job_failed_keeps_raw_failure_payload_inline_without_settings(monkeypatch) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-runtime-failure-payload",
            job_kind="temporal_project",
            status="running",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    payload = {"original_error": "imagery request failed"}

    monkeypatch.setattr(
        "src.repositories.job_repository.Settings",
        lambda: pytest.fail("mark_job_failed must not construct Settings during failure persistence"),
    )

    record = mark_job_failed(
        job_id="job-runtime-failure-payload",
        error_code="runtime_error",
        error_message="Original temporal failure",
        raw_result=payload,
        session=session,  # type: ignore[arg-type]
    )

    assert record.raw_result == payload


def test_mark_job_execution_failed_passes_settings_and_preserves_original_error(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-worker-failure",
            job_kind="temporal_project",
            status="running",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    settings = Settings(runtime_cache_dir=tmp_path)
    captured: dict[str, object] = {}

    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))

    def persist_failure(**kwargs):
        captured.update(kwargs)
        return mark_job_failed(**kwargs)

    monkeypatch.setattr(jobs_service, "mark_job_failed", persist_failure)

    persisted = jobs_service.mark_job_execution_failed(
        "job-worker-failure",
        "RuntimeError: original worker error",
        settings=settings,
    )

    assert persisted is True
    assert captured["settings"] is settings
    assert session.job.status == "failed"
    assert session.job.error_message == "RuntimeError: original worker error"


def test_mark_job_execution_failed_logs_persistence_error_without_raising(monkeypatch, tmp_path, caplog) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)

    @contextmanager
    def failing_session_scope(*_args, **_kwargs):
        raise RuntimeError("database unavailable")
        yield

    monkeypatch.setattr(jobs_service, "session_scope", failing_session_scope)

    with caplog.at_level("ERROR"):
        persisted = jobs_service.mark_job_execution_failed(
            "job-worker-failure",
            "ValueError: original worker error",
            settings=settings,
        )

    assert persisted is False
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "JOB_FAILURE_PERSISTENCE_FAILED" in messages
    assert "ValueError: original worker error" in messages


def test_temporal_worker_persists_original_exception_before_reraising(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    worker_lock = settings.wayback_tile_preflight_cache_dir / "worker.json.lock"
    worker_lock.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setattr(job_tasks, "_resolve_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(job_tasks, "_log_worker_effective_backend", lambda **_kwargs: None)
    monkeypatch.setattr(
        job_tasks,
        "_prepare_job_for_execution",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("original temporal failure")),
    )
    monkeypatch.setattr(
        job_tasks,
        "mark_job_execution_failed",
        lambda job_id, message, *, settings: captured.update(
            job_id=job_id,
            message=message,
            settings=settings,
        ),
    )

    with pytest.raises(RuntimeError, match="original temporal failure"):
        job_tasks.run_temporal_project_job.run("job-temporal-failure", "project-1")

    assert captured == {
        "job_id": "job-temporal-failure",
        "message": "RuntimeError: original temporal failure",
        "settings": settings,
    }
    assert not worker_lock.exists()


def test_temporal_worker_fails_success_response_without_published_artifacts(monkeypatch, tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-temporal-incomplete-finalization",
            job_kind="temporal_project",
            status="queued",
            project_id="project-1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    project = TemporalProject(
        project_id="project-1",
        name="Project 1",
        aoi_geojson=None,
        milestones=[
            TemporalMilestone(release_identifier="WB_2024_R01", status="complete", metrics=TemporalMilestoneMetrics()),
            TemporalMilestone(
                release_identifier="WB_2025_R01",
                status="complete",
                pair_request_hash="request-1",
                populated_request_hash="request-1",
                metrics=TemporalMilestoneMetrics(additions_feature_count=1, effective_feature_count=1),
                artifacts=[],
            ),
        ],
        created_at="2026-06-27T00:00:00Z",
        updated_at="2026-06-27T00:00:00Z",
    )

    monkeypatch.setattr(job_tasks, "_resolve_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(job_tasks, "_log_worker_effective_backend", lambda **_kwargs: None)
    monkeypatch.setattr(job_tasks, "cleanup_wayback_preflight_locks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(job_tasks, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(job_tasks, "update_progress", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(job_tasks, "_latest_temporal_run_id", lambda *_args, **_kwargs: "temporal-run-1")
    monkeypatch.setattr(
        job_tasks,
        "run_temporal_project_api",
        lambda *_args, **_kwargs: TemporalProjectRunResponse(success=True, project=project),
    )

    result = job_tasks.run_temporal_project_job.run("job-temporal-incomplete-finalization", "project-1")

    assert result["status"] == "failed"
    assert session.job.status == "failed"
    assert session.job.error_code == "temporal_finalization_incomplete"
    assert "no registered project artifacts" in session.job.error_message


def test_get_job_response_normalizes_legacy_complete(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-legacy-complete",
            job_kind="detection",
            status="complete",
            progress=100,
            stage="completed",
            cancel_requested=False,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    settings = Settings(runtime_cache_dir=tmp_path)

    monkeypatch.setattr(jobs_service, "get_job", lambda *_args, **_kwargs: session.job)

    response = jobs_service.get_job_response("job-legacy-complete", settings=settings)

    assert response.status == "completed"


def test_worker_skips_terminal_job_before_running(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-terminal",
            job_kind="temporal_project",
            status="failed",
            progress=100,
            stage="failed",
            error_code="runtime_error",
            error_message="Previous run failed.",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    settings = Settings(runtime_cache_dir=tmp_path)

    monkeypatch.setattr(job_tasks, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))

    result = job_tasks._prepare_job_for_execution("job-terminal", settings)

    assert result == {"job_id": "job-terminal", "status": "failed", "skipped": True}
    assert session.job.status == "failed"
    assert session.job.started_at is None


def test_compact_detection_result_omits_heavy_geojson() -> None:
    response = RunResponse(
        success=True,
        summary=SummaryStats(
            request_hash="hash-1",
            mode="fast_preview",
            model_backend="bandon_mps",
            estimated_area_m2=100.0,
            tile_count_t1=1,
            tile_count_t2=1,
            total_new_buildings=2,
            total_building_blocks=1,
            total_new_building_area_m2=50.0,
            total_building_block_area_m2=75.0,
        ),
        change_polygons_geojson={"type": "FeatureCollection", "features": [{"large": True}]},
        buffer_layers_geojson={"buffer_10m": {"type": "FeatureCollection", "features": [{"large": True}]}},
        artifacts=[
            ArtifactEntry(
                name="change_polygons",
                path="/tmp/change.geojson",
                media_type="application/geo+json",
                description="Change polygons",
            )
        ],
        diagnostics=DiagnosticMetadata(cache_hit=False, stage_seconds={"inference": 1.2}),
    )

    compact = job_tasks._compact_detection_result(response, result_run_id="run-1", stage_timings={"total": 2.0})

    assert compact["success"] is True
    assert compact["request_hash"] == "hash-1"
    assert compact["result_run_id"] == "run-1"
    assert "change_polygons_geojson" not in compact
    assert "buffer_layers_geojson" not in compact
    assert compact["artifacts"] == [
        {
            "name": "change_polygons",
            "path": "/tmp/change.geojson",
            "media_type": "application/geo+json",
            "description": "Change polygons",
        }
    ]


def test_start_detection_job_raises_when_jobs_disabled(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession()
    settings = Settings(runtime_cache_dir=tmp_path, jobs_enabled=False, redis_url="redis://localhost:6379/0")

    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(jobs_service, "assert_redis_available", jobs_service.assert_redis_available)

    with pytest.raises(JobsDisabledError) as exc_info:
        start_detection_job(_sample_request(), settings=settings)

    assert exc_info.value.code == "jobs_disabled"


def test_worker_effective_runtime_log_uses_selected_checkpoint_and_canonical_thresholds(
    tmp_path,
    caplog,
) -> None:
    bandon_checkpoint = tmp_path / "bandon.pth"
    bandon_checkpoint.write_bytes(b"bandon")
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        inference_backend="bandon_mps",
        bandon_checkpoint_path=bandon_checkpoint,
        change_threshold=0.37,
        semantic_threshold=0.42,
    )

    with caplog.at_level("INFO"):
        _log_worker_effective_backend(job_id="job-1", job_kind="temporal_project", settings=settings)

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "backend=bandon_mps" in messages
    assert "checkpointEnvVar=APP_BANDON_CHECKPOINT_PATH" in messages
    assert f"checkpointPath={bandon_checkpoint.resolve()}" in messages
    assert "thresholdsSource=backend_settings_env semantic=0.42 change=0.37" in messages


def test_worker_effective_runtime_log_uses_temporal_request_threshold_override(tmp_path, caplog) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        change_threshold=0.5,
        semantic_threshold=0.42,
    )

    with caplog.at_level("INFO"):
        _log_worker_effective_backend(
            job_id="job-1",
            job_kind="temporal_project",
            settings=settings,
            change_threshold_override=0.3,
            threshold_source="request_override",
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "CELERY_EFFECTIVE_THRESHOLD=0.3 jobId=job-1 jobKind=temporal_project" in messages
    assert "CELERY_EFFECTIVE_THRESHOLDS source=request_override semantic=0.42 change=0.3" in messages
    assert "thresholdsSource=request_override semantic=0.42 change=0.3" in messages
