from __future__ import annotations

from src.config import Settings
from src.repositories.job_repository import update_job_progress


def update_progress(
    job_id: str,
    progress: int,
    stage: str,
    message: str | None = None,
    *,
    settings: Settings | None = None,
) -> None:
    update_job_progress(job_id=job_id, progress=progress, stage=stage, message=message, settings=settings)

