from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


JobStatus = Literal["queued", "running", "complete", "failed", "cancel_requested", "cancelled"]


class JobStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    celery_task_id: str | None = None
    job_kind: str
    status: JobStatus


class JobResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    celery_task_id: str | None = None
    job_kind: str
    status: JobStatus
    project_id: str | None = None
    request_hash: str | None = None
    progress: int | None = None
    stage: str | None = None
    message: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    result_run_id: str | None = None
    raw_request: dict[str, Any] | None = None
    raw_result: dict[str, Any] | None = None
    cancel_requested: bool = False
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
