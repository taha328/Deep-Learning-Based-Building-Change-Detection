from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from src.api.routes.health import jobs_health, redis_health
from src.config import Settings
from src.db.models import JobRecord, ProjectRecord
from src.jobs import tasks as job_tasks
from src.jobs import service as jobs_service
from src.jobs.service import cancel_job, start_detection_job, start_temporal_project_job
from src.repositories.job_repository import mark_job_completed
from src.schemas import RunRequest


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


def test_start_temporal_job_requires_existing_project(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(project=SimpleNamespace(id=uuid.uuid4(), project_id="temporal-test"))
    settings = Settings(
        runtime_cache_dir=tmp_path,
        jobs_enabled=True,
        redis_url="redis://localhost:6379/0",
    )

    monkeypatch.setattr(jobs_service, "assert_redis_available", lambda settings: None)
    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(jobs_service.celery_app, "send_task", lambda *args, **kwargs: SimpleNamespace(id="celery-task-2"))

    response = start_temporal_project_job("temporal-test", settings=settings)

    assert response.job_id.startswith("job-")
    assert response.celery_task_id == "celery-task-2"
    assert session.job is not None
    assert session.job.project_id == "temporal-test"
    assert session.job.project_db_id == session.project.id


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
    assert revoked["terminate"] is True
    assert revoked["signal"] == "SIGTERM"


def test_mark_job_completed_clears_previous_error_fields(tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-stale-then-complete",
            job_kind="temporal_project",
            status="failed",
            progress=100,
            stage="failed",
            error_code="worker_stale",
            error_message="Job exceeded the stale timeout.",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    settings = Settings(runtime_cache_dir=tmp_path)

    record = mark_job_completed(
        job_id="job-stale-then-complete",
        result_run_id="run-1",
        raw_result={"success": True},
        settings=settings,
        session=session,  # type: ignore[arg-type]
    )

    assert record.status == "complete"
    assert record.error_code is None
    assert record.error_message is None


def test_worker_skips_terminal_job_before_running(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession(
        job=JobRecord(
            job_id="job-terminal",
            job_kind="temporal_project",
            status="failed",
            progress=100,
            stage="failed",
            error_code="worker_stale",
            error_message="Job exceeded the stale timeout.",
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


def test_start_detection_job_raises_when_jobs_disabled(monkeypatch, tmp_path) -> None:
    session = _FakeJobSession()
    settings = Settings(runtime_cache_dir=tmp_path, jobs_enabled=False, redis_url="redis://localhost:6379/0")

    monkeypatch.setattr(jobs_service, "session_scope", lambda *_args, **_kwargs: _job_session_scope(session))
    monkeypatch.setattr(jobs_service, "assert_redis_available", jobs_service.assert_redis_available)

    with pytest.raises(HTTPException) as exc_info:
        start_detection_job(_sample_request(), settings=settings)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "jobs_disabled"
