from __future__ import annotations

from typing import Any


class JobServiceError(RuntimeError):
    code = "job_service_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class JobsDisabledError(JobServiceError):
    code = "jobs_disabled"


class RedisUnavailableError(JobServiceError):
    code = "redis_unavailable"


class JobNotFoundError(JobServiceError):
    code = "not_found"


class CeleryEnqueueError(JobServiceError):
    code = "celery_unavailable"
