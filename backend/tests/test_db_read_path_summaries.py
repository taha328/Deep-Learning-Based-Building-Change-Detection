from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from src.config import Settings
from src.jobs import service as jobs_service
from src.jobs.service import list_job_responses
from src.repositories.temporal_project_repository import (
    get_project,
    list_project_artifact_summaries,
    list_project_milestone_summaries,
    list_project_summary_view,
    list_project_summaries,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _ProjectedSession:
    def __init__(self, rows, *, forbidden: tuple[str, ...]):
        self.rows = rows
        self.forbidden = forbidden
        self.statements: list[str] = []

    def execute(self, statement, _params=None):
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        self.statements.append(compiled)
        lowered = compiled.lower()
        for token in self.forbidden:
            assert token not in lowered
        return _FakeResult(self.rows)


class _FakeQuery:
    def __init__(self, record):
        self.record = record

    def filter(self, *_args, **_kwargs):
        return self

    def one_or_none(self):
        return self.record


class _DetailSession:
    def __init__(self, record):
        self.record = record

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self.record)


def _project_row(**overrides):
    now = datetime(2026, 5, 20, tzinfo=UTC)
    values = {
        "id": uuid4(),
        "project_id": "project-1",
        "name": "Project 1",
        "semantics": "expansion_only",
        "project_dir": "/tmp/project-1",
        "created_at": now,
        "updated_at": now,
        "milestone_count": 2,
        "complete_milestone_count": 1,
        "download_bundle_path": "/tmp/project-1/bundle.zip",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_project_list_uses_projection_without_raw_payload() -> None:
    session = _ProjectedSession([_project_row()], forbidden=("raw_payload", "raw_response", "raw_result", "geojson"))

    summaries = list_project_summaries(session=session)

    assert summaries[0].project_id == "project-1"
    assert summaries[0].milestone_count == 2
    assert "project_summary" in session.statements[0].lower()


def test_project_summary_view_uses_lightweight_columns() -> None:
    session = _ProjectedSession([_project_row(run_count=3, artifact_count=4)], forbidden=("raw_payload", "raw_response", "raw_result", "geojson"))

    summaries = list_project_summary_view(session=session)

    assert summaries[0].project_id == "project-1"
    assert summaries[0].complete_milestone_count == 1
    assert "public.project_summary" in session.statements[0].lower()


def test_project_list_returns_public_summary_fields_without_heavy_payloads() -> None:
    session = _ProjectedSession([_project_row()], forbidden=("raw_payload", "raw_response", "raw_result", "geojson"))

    payload = list_project_summaries(session=session)[0].model_dump(mode="json")

    assert payload == {
        "project_id": "project-1",
        "name": "Project 1",
        "project_dir": "/tmp/project-1",
        "project_kind": "temporal",
        "display_name": "Project 1",
        "semantics": "expansion_only",
        "milestone_count": 2,
        "complete_milestone_count": 1,
        "created_at": "2026-05-20T00:00:00Z",
        "updated_at": "2026-05-20T00:00:00Z",
        "download_bundle_path": "/tmp/project-1/bundle.zip",
    }
    assert not {"raw_payload", "raw_response", "raw_result", "geojson"} & payload.keys()


def test_project_detail_still_loads_full_payload() -> None:
    record = SimpleNamespace(
        raw_payload={
            "project_id": "project-1",
            "name": "Project 1",
            "project_dir": "/tmp/project-1",
            "semantics": "expansion_only",
            "aoi_geojson": None,
            "milestones": [],
            "created_at": "2026-05-20T00:00:00Z",
            "updated_at": "2026-05-20T00:00:00Z",
            "execution_config": None,
            "warnings": [],
            "validation_blocking_errors": [],
            "download_bundle_path": None,
            "latest_source": "esri_wayback",
        }
    )

    project = get_project("project-1", session=_DetailSession(record))  # type: ignore[arg-type]

    assert project.project_id == "project-1"


def test_null_raw_payload_does_not_break_summary_listing() -> None:
    session = _ProjectedSession([_project_row(project_id="project-null-payload")], forbidden=("raw_payload",))

    summaries = list_project_summaries(session=session)

    assert summaries[0].project_id == "project-null-payload"


def test_milestone_summary_listing_does_not_select_raw_payload() -> None:
    row = SimpleNamespace(
        id=uuid4(),
        release_identifier="WB_2026_R01",
        release_date=datetime(2026, 1, 1, tzinfo=UTC),
        status="complete",
        source_mode="automated",
        pair_request_hash="hash-1",
        error_message=None,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        updated_at=datetime(2026, 5, 20, tzinfo=UTC),
        added_area_m2=1.0,
        total_area_m2=2.0,
        additions_feature_count=3,
        effective_feature_count=4,
        building_level_available=True,
        added_block_count=5,
        cumulative_block_count=6,
        added_block_area_m2=7.0,
        cumulative_block_area_m2=8.0,
        growth_envelope_area_m2=9.0,
    )
    session = _ProjectedSession([row], forbidden=("raw_payload", "geojson"))

    summaries = list_project_milestone_summaries("project-1", session=session)

    assert summaries[0]["release_identifier"] == "WB_2026_R01"
    assert "raw_payload" not in summaries[0]


def test_artifact_summary_listing_does_not_select_geometry_payloads() -> None:
    row = SimpleNamespace(
        id=uuid4(),
        name="bundle",
        path="/tmp/bundle.zip",
        media_type="application/zip",
        description="Bundle",
        artifact_kind="bundle",
        size_bytes=123,
        checksum="abc",
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        release_identifier=None,
    )
    session = _ProjectedSession([row], forbidden=("raw_payload", "raw_response", "raw_result", "geojson", "geometry_layers"))

    summaries = list_project_artifact_summaries("project-1", session=session)

    assert summaries[0]["name"] == "bundle"
    assert "geojson" not in summaries[0]


def test_public_job_list_response_omits_raw_result(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        jobs_service,
        "list_job_summaries",
        lambda **_kwargs: [
            {
                "job_id": "job-1",
                "celery_task_id": "task-1",
                "job_kind": "detection",
                "status": "completed",
                "created_at": datetime(2026, 5, 20, tzinfo=UTC),
                "updated_at": datetime(2026, 5, 20, tzinfo=UTC),
            }
        ],
    )

    payload = list_job_responses(settings=Settings(runtime_cache_dir=tmp_path))

    assert payload[0]["job_id"] == "job-1"
    assert "raw_result" not in payload[0]
