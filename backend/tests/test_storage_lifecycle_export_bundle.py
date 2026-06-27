from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException

from src.api.routes.files import create_run_export_bundle
from src.config import Settings
from src.schemas import RunResponse
from src.services import processing


def test_tiled_export_bundle_auto_generation_disabled_by_default(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    result_dir = settings.request_cache_dir / "run-no-auto-zip"
    result_dir.mkdir(parents=True)
    response = RunResponse(success=True)

    def _unexpected_auto_export(_result_dir: Path) -> Path:
        raise AssertionError("create_export_bundle_from_manifest must not be called by default")

    monkeypatch.setattr(processing, "create_export_bundle_from_manifest", _unexpected_auto_export)

    returned = processing._maybe_create_tiled_export_bundle(
        response=response,
        settings=settings,
        result_dir=result_dir,
        request_hash="run-no-auto-zip",
    )

    assert returned.downloadable_zip_path is None
    assert not (result_dir / "export_bundle.zip").exists()


def test_tiled_export_bundle_auto_generation_can_be_opted_in(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        auto_generate_tiled_export_bundle=True,
    )
    result_dir = settings.request_cache_dir / "run-opt-in-zip"
    result_dir.mkdir(parents=True)
    calls: list[Path] = []

    def _fake_export(request_dir: Path) -> Path:
        calls.append(request_dir)
        bundle = request_dir / "export_bundle.zip"
        bundle.write_bytes(b"zip")
        return bundle

    monkeypatch.setattr(processing, "create_export_bundle_from_manifest", _fake_export)

    returned = processing._maybe_create_tiled_export_bundle(
        response=RunResponse(success=True),
        settings=settings,
        result_dir=result_dir,
        request_hash="run-opt-in-zip",
    )

    assert calls == [result_dir]
    assert returned.downloadable_zip_path == str(result_dir / "export_bundle.zip")
    saved = json.loads((result_dir / "run_response.json").read_text(encoding="utf-8"))
    assert saved["downloadable_zip_path"] == str(result_dir / "export_bundle.zip")


def test_explicit_run_export_bundle_endpoint_creates_zip_when_artifacts_exist(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request_dir = settings.request_cache_dir / "run-explicit-export"
    request_dir.mkdir(parents=True)
    (request_dir / "building_change_blocks.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}',
        encoding="utf-8",
    )

    response = create_run_export_bundle("run-explicit-export", settings=settings)

    bundle_path = Path(response["path"])
    assert bundle_path == request_dir / "export_bundle.zip"
    assert bundle_path.is_file()


def test_explicit_run_export_bundle_endpoint_returns_clear_error_after_compaction(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime")
    request_dir = settings.request_cache_dir / "run-compacted"
    request_dir.mkdir(parents=True)
    (request_dir / "run_response.json").write_text('{"success": true}', encoding="utf-8")
    (request_dir / "manifest.json").write_text(json.dumps({"artifacts": []}), encoding="utf-8")

    try:
        create_run_export_bundle("run-compacted", settings=settings)
    except HTTPException as exc:
        assert exc.status_code == 422
        assert isinstance(exc.detail, dict)
        assert exc.detail["code"] == "export_bundle_unavailable"
        assert "No exportable final artifacts" in exc.detail["message"]
    else:
        raise AssertionError("expected explicit export to fail clearly for compacted request")
