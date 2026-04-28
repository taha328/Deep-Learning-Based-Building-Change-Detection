from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.config import Settings
from src.db.models import JobRecord
from src.repositories.job_repository import mark_stale_jobs_failed


class _FakeStaleQuery:
    def __init__(self, jobs):
        self.jobs = jobs

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self.jobs


class _FakeStaleSession:
    def __init__(self, jobs):
        self.jobs = jobs

    def query(self, model):
        return _FakeStaleQuery(self.jobs)


def test_mark_stale_jobs_failed_marks_queued_and_running_jobs(tmp_path) -> None:
    stale_created_at = datetime.now(UTC) - timedelta(minutes=90)
    jobs = [
        JobRecord(
            job_id="job-queued",
            job_kind="detection",
            status="queued",
            progress=0,
            stage="queued",
            message="Queued for execution.",
            created_at=stale_created_at,
            updated_at=stale_created_at,
        ),
        JobRecord(
            job_id="job-running",
            job_kind="temporal_project",
            status="running",
            progress=15,
            stage="running",
            message="Processing.",
            created_at=stale_created_at,
            updated_at=stale_created_at,
            started_at=stale_created_at,
        ),
    ]

    session = _FakeStaleSession(jobs)
    settings = Settings(runtime_cache_dir=tmp_path, celery_job_stale_after_minutes=60)

    stale_jobs = mark_stale_jobs_failed(stale_after_minutes=settings.celery_job_stale_after_minutes, settings=settings, session=session)  # type: ignore[arg-type]

    assert len(stale_jobs) == 2
    assert all(job.status == "failed" for job in jobs)
    assert all(job.error_code == "worker_stale" for job in jobs)
    assert all(job.completed_at is not None for job in jobs)
