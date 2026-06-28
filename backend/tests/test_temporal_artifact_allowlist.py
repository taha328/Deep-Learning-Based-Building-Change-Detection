from __future__ import annotations

import json

import pytest

from src.config import Settings
from src.schemas import TemporalArtifactEntry, TemporalMilestone, TemporalProject
from src.services.temporal_projects import (
    TEMPORAL_ALLOWED_ARTIFACT_KEYS,
    get_temporal_project,
    resolve_temporal_project_artifact_path,
    save_temporal_project,
)


APPROVED_KEYS = {
    "automated_building_blocks",
    "additions",
    "building_change_buffer_10m",
    "building_change_buffer_15m",
    "building_change_buffer_20m",
    "cumulative_building_change_buffer_10m",
    "cumulative_building_change_buffer_15m",
    "cumulative_building_change_buffer_20m",
}

DEPRECATED_KEYS = {
    "automated_additions",
    "automated_candidate_footprint",
    "effective_footprint",
    "cumulative_union",
    "cumulative_growth_blocks",
    "cumulative_growth_envelope",
}


def _feature_collection() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]],
                },
            }
        ],
    }


def _artifact(key: str, path: str) -> TemporalArtifactEntry:
    return TemporalArtifactEntry(
        name=key,
        path=path,
        media_type="application/geo+json",
        description=key,
        key=key,
    )


def _project(project_id: str, artifacts: list[TemporalArtifactEntry] | None = None) -> TemporalProject:
    return TemporalProject(
        project_id=project_id,
        name="Artifact allowlist",
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
        milestones=[
            TemporalMilestone(
                release_identifier="WB_2026_R01",
                additions_geojson=_feature_collection(),
                automated_building_blocks_geojson=_feature_collection(),
                automated_additions_geojson=_feature_collection(),
                automated_candidate_footprint_geojson=_feature_collection(),
                effective_footprint_geojson=_feature_collection(),
                cumulative_union_geojson=_feature_collection(),
                cumulative_growth_blocks_geojson=_feature_collection(),
                cumulative_growth_envelope_geojson=_feature_collection(),
                artifacts=artifacts or [],
            )
        ],
    )


def test_temporal_allowed_artifact_keys_are_exact() -> None:
    assert TEMPORAL_ALLOWED_ARTIFACT_KEYS == APPROVED_KEYS
    assert "automated_building_blocks" in TEMPORAL_ALLOWED_ARTIFACT_KEYS


def test_deprecated_metadata_and_writes_are_filtered(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    milestone_dir = settings.temporal_projects_dir / "allowlist" / "milestones" / "WB_2026_R01"
    artifacts = [
        _artifact("automated_building_blocks", str(milestone_dir / "automated_building_blocks.geojson")),
        *[_artifact(key, str(milestone_dir / f"{key}.geojson")) for key in sorted(DEPRECATED_KEYS)],
    ]

    save_temporal_project(_project("allowlist", artifacts), settings)
    reloaded = get_temporal_project("allowlist", settings)

    assert {artifact.key for artifact in reloaded.milestones[0].artifacts} <= APPROVED_KEYS
    assert "automated_building_blocks" in {artifact.key for artifact in reloaded.milestones[0].artifacts}
    for key in DEPRECATED_KEYS:
        assert not (milestone_dir / f"{key}.geojson").exists()


def test_direct_artifact_resolution_blocks_deprecated_cached_files(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    milestone_dir = settings.temporal_projects_dir / "direct" / "milestones" / "WB_2026_R01"
    milestone_dir.mkdir(parents=True)
    payload = json.dumps(_feature_collection())
    (milestone_dir / "additions.geojson").write_text(payload)
    (milestone_dir / "automated_building_blocks.geojson").write_text(payload)
    (milestone_dir / "cumulative_union.geojson").write_text(payload)

    allowed_path, media_type = resolve_temporal_project_artifact_path(
        project_id="direct",
        release_identifier="WB_2026_R01",
        artifact_key="additions",
        settings=settings,
    )
    assert allowed_path.name == "additions.geojson"
    assert media_type == "application/geo+json"
    automated_path, _ = resolve_temporal_project_artifact_path(
        project_id="direct",
        release_identifier="WB_2026_R01",
        artifact_key="automated_building_blocks",
        settings=settings,
    )
    assert automated_path.name == "automated_building_blocks.geojson"

    with pytest.raises(FileNotFoundError, match="Unknown temporal artifact key"):
        resolve_temporal_project_artifact_path(
            project_id="direct",
            release_identifier="WB_2026_R01",
            artifact_key="cumulative_union",
            settings=settings,
        )
