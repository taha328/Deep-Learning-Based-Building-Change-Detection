from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.config import Settings
from src.db.models import GeometryLayerRecord, JobRecord, MilestoneRecord, ProjectRecord, RunRecord
from src.jobs.service import _job_response
from src.repositories.job_repository import mark_job_completed
from src.repositories.payload_storage import (
    build_payload_reference,
    externalize_payload_if_needed,
    payload_storage_path,
    resolve_payload_reference,
)
from src.repositories.run_repository import save_detection_run
from src.repositories.temporal_project_repository import get_project, list_project_summaries, save_project
from src.schemas import RunRequest, RunResponse, SummaryStats, TemporalMilestone, TemporalProject


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def all(self):
        return self._rows

    def one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeQuery:
    def __init__(self, record=None):
        self.record = record

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.record

    def one_or_none(self):
        return self.record

    def delete(self):
        return 0


class _FakeSession:
    def __init__(self, *, project=None, run=None, job=None, summary_rows=None):
        self.project = project
        self.run = run
        self.job = job
        self.summary_rows = summary_rows or []
        self.added = []
        self.executed_mappings = []

    def query(self, model, *_args, **_kwargs):
        if model is ProjectRecord:
            return _FakeQuery(self.project)
        if model is RunRecord:
            return _FakeQuery(self.run)
        if model is JobRecord:
            return _FakeQuery(self.job)
        return _FakeQuery()

    def add(self, record):
        if getattr(record, "id", None) is None:
            record.id = uuid4()
        if isinstance(record, ProjectRecord):
            self.project = record
        if isinstance(record, RunRecord):
            self.run = record
        if isinstance(record, JobRecord):
            self.job = record
        if isinstance(record, MilestoneRecord):
            record.id = getattr(record, "id", None) or uuid4()
        self.added.append(record)

    def flush(self):
        for record in self.added:
            if getattr(record, "id", None) is None:
                record.id = uuid4()

    def execute(self, _statement, mappings=None):
        if mappings is not None:
            self.executed_mappings.extend(mappings)
            return _FakeResult()
        return _FakeResult(self.summary_rows)


class _DetailSession:
    def __init__(self, record):
        self.record = record

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self.record)


def _feature_collection(repetitions: int = 1) -> dict:
    feature = {
        "type": "Feature",
        "properties": {"label": "x" * 128},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-6.999, 33.0], [-6.999, 33.001], [-7.0, 33.001], [-7.0, 33.0]]],
        },
    }
    return {"type": "FeatureCollection", "features": [feature.copy() for _ in range(repetitions)]}


def _project(tmp_path, *, feature_repetitions: int = 1) -> TemporalProject:
    project_root = Path(tmp_path)
    return TemporalProject(
        project_id="temporal-payload-test",
        name="Temporal Payload Test",
        project_dir=str(project_root / "temporal-payload-test"),
        aoi_geojson=None,
        milestones=[
            TemporalMilestone(
                release_identifier="WB_2026_R01",
                release_date="2026-01-01",
                status="complete",
                additions_geojson=_feature_collection(feature_repetitions),
                buffer_layers_geojson={"10m": _feature_collection(feature_repetitions)},
            )
        ],
        created_at="2026-05-20T00:00:00Z",
        updated_at="2026-05-20T00:00:00Z",
    )


def _run_request() -> RunRequest:
    return RunRequest(
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-6.999, 33.0], [-6.999, 33.001], [-7.0, 33.001], [-7.0, 33.0]]],
        },
        t1_release="WB_2024_R01",
        t2_release="WB_2026_R01",
        mode="fast_preview",
    )


def _run_response(*, payload_repetitions: int) -> RunResponse:
    return RunResponse(
        summary=SummaryStats(
            request_hash="request-hash",
            mode="fast_preview",
            estimated_area_m2=1.0,
            tile_count_t1=1,
            tile_count_t2=1,
            total_new_buildings=1,
            total_building_blocks=1,
            total_new_building_area_m2=1.0,
            total_building_block_area_m2=1.0,
        ),
        new_buildings_geojson=_feature_collection(payload_repetitions),
    )


def test_large_project_payload_is_externalized(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, db_inline_json_max_bytes=512)
    session = _FakeSession()

    save_project(_project(tmp_path, feature_repetitions=20), settings=settings, session=session)  # type: ignore[arg-type]

    assert session.project.raw_payload["storage"] == "file"
    assert session.project.raw_payload["schema"] == "temporal_project_payload_v1"
    assert session.project.raw_payload["size_bytes"] > 512
    assert "additions_geojson" not in str(session.added[1].raw_payload)


def test_small_payload_can_remain_inline_under_threshold(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, db_inline_json_max_bytes=10_000)
    payload = {"small": True}

    stored = externalize_payload_if_needed(
        payload,
        settings=settings,
        table="runs",
        column="raw_response",
        schema="test_v1",
        target_path=payload_storage_path(settings, table="runs", column="raw_response", key="small"),
    )

    assert stored == payload


def test_old_inline_raw_payload_rows_still_load() -> None:
    payload = _project("/tmp").model_dump(mode="json")
    record = SimpleNamespace(raw_payload=payload)

    project = get_project("temporal-payload-test", session=_DetailSession(record))  # type: ignore[arg-type]

    assert project.project_id == "temporal-payload-test"


def test_new_reference_raw_payload_rows_resolve_to_full_payload(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    payload = _project(tmp_path).model_dump(mode="json")
    target_path = payload_storage_path(settings, table="projects", column="raw_payload", key="project")
    stored = externalize_payload_if_needed(
        payload,
        settings=settings,
        table="projects",
        column="raw_payload",
        schema="temporal_project_payload_v1",
        target_path=target_path,
        force_externalize=True,
    )

    project = get_project("temporal-payload-test", session=_DetailSession(SimpleNamespace(raw_payload=stored)))  # type: ignore[arg-type]

    assert project.project_id == "temporal-payload-test"


def test_large_run_raw_response_is_externalized(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, db_inline_json_max_bytes=512)
    session = _FakeSession()

    save_detection_run(
        request=_run_request(),
        response=_run_response(payload_repetitions=20),
        settings=settings,
        session=session,  # type: ignore[arg-type]
    )

    assert session.run.raw_response["storage"] == "file"
    assert session.run.raw_response["schema"] == "detection_run_response_v1"


def test_large_job_raw_result_is_externalized_and_detail_resolves(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path, db_inline_json_max_bytes=512)
    now = datetime(2026, 5, 20, tzinfo=UTC)
    job = JobRecord(
        job_id="job-payload-test",
        job_kind="detection",
        status="running",
        cancel_requested=False,
        created_at=now,
        updated_at=now,
    )
    session = _FakeSession(job=job)

    mark_job_completed(
        job_id="job-payload-test",
        raw_result={"features": ["x" * 1024]},
        settings=settings,
        session=session,  # type: ignore[arg-type]
    )

    assert job.raw_result["storage"] == "file"
    assert _job_response(job).raw_result == {"features": ["x" * 1024]}


def test_missing_referenced_file_returns_clear_error(tmp_path) -> None:
    reference = build_payload_reference(tmp_path / "missing.json", "sha", 1, "test_v1")

    with pytest.raises(FileNotFoundError, match="Referenced DB payload file is missing"):
        resolve_payload_reference(reference, table="projects", column="raw_payload")


def test_list_summaries_do_not_resolve_payload_files(monkeypatch, tmp_path) -> None:
    def fail_resolve(*_args, **_kwargs):
        raise AssertionError("summary listing should not resolve payload references")

    monkeypatch.setattr("src.repositories.payload_storage.resolve_payload_reference", fail_resolve)
    row = SimpleNamespace(
        id=uuid4(),
        project_id="project-summary",
        name="Summary",
        semantics="expansion_only",
        project_dir=str(tmp_path),
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        updated_at=datetime(2026, 5, 20, tzinfo=UTC),
        milestone_count=1,
        complete_milestone_count=1,
        download_bundle_path=None,
    )
    session = _FakeSession(summary_rows=[row])

    summaries = list_project_summaries(session=session)  # type: ignore[arg-type]

    assert summaries[0].project_id == "project-summary"
