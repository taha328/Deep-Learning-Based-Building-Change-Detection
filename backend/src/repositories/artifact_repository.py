from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from src.db.models import ArtifactRecord, MilestoneRecord, ProjectRecord, RunRecord
from src.schemas import ArtifactEntry, TemporalArtifactEntry


def _artifact_kind(name: str, media_type: str | None) -> str | None:
    if media_type == "application/zip" or name.endswith("_bundle"):
        return "bundle"
    if media_type == "application/geo+json" or name.endswith(".geojson"):
        return "geojson"
    if media_type and media_type.startswith("image/"):
        return "preview"
    return None


def _file_size(path: str) -> int | None:
    try:
        candidate = Path(path)
        return candidate.stat().st_size if candidate.exists() else None
    except OSError:
        return None


def artifact_record_from_entry(
    entry: ArtifactEntry | TemporalArtifactEntry,
    *,
    project: ProjectRecord | None = None,
    milestone: MilestoneRecord | None = None,
    run: RunRecord | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        project_db_id=project.id if project else None,
        milestone_id=milestone.id if milestone else None,
        run_db_id=run.id if run else None,
        name=entry.name,
        path=entry.path,
        media_type=entry.media_type,
        description=entry.description,
        artifact_kind=_artifact_kind(entry.name, entry.media_type),
        size_bytes=_file_size(entry.path),
    )


def replace_run_artifacts(session: Session, run: RunRecord, artifacts: list[ArtifactEntry]) -> None:
    session.query(ArtifactRecord).filter(ArtifactRecord.run_db_id == run.id).delete()
    for entry in artifacts:
        session.add(artifact_record_from_entry(entry, project=run.project, run=run))

