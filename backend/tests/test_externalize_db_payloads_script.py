from __future__ import annotations

from pathlib import Path

from src.config import Settings
from src.repositories.payload_storage import build_payload_reference
from scripts.externalize_db_payloads import PayloadTarget, process_payload_row


TARGET = PayloadTarget("projects", "raw_payload", "id")


def _large_payload() -> dict:
    return {"payload": "x" * 1024}


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)

    result = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value=_large_payload(),
        size_bytes=1024,
        settings=settings,
        min_bytes=512,
        mode="dry_run",
    )

    assert result.result == "dry_run"
    assert result.path is not None
    assert not result.path.exists()


def test_apply_migrates_large_inline_payload(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    updates: list[dict] = []

    result = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value=_large_payload(),
        size_bytes=1024,
        settings=settings,
        min_bytes=512,
        mode="apply",
        update_reference=updates.append,
    )

    assert result.result == "migrated"
    assert result.path is not None
    assert result.path.exists()
    assert updates[0]["storage"] == "file"
    assert updates[0]["source_table"] == "projects"
    assert updates[0]["source_column"] == "raw_payload"
    assert updates[0]["source_pk"] == "project-1"


def test_already_externalized_reference_is_skipped(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    path = tmp_path / "payload.json"
    path.write_text('{"ok":true}', encoding="utf-8")
    reference = build_payload_reference(path, "unused", 11, "db_payload_externalized_v1")
    reference["sha256"] = __import__("hashlib").sha256(path.read_bytes()).hexdigest()

    result = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value=reference,
        size_bytes=100,
        settings=settings,
        min_bytes=512,
        mode="apply",
    )

    assert result.result == "skipped"
    assert result.reason == "already_externalized"


def test_small_payload_is_skipped(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)

    result = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value={"small": True},
        size_bytes=64,
        settings=settings,
        min_bytes=512,
        mode="apply",
    )

    assert result.result == "skipped"
    assert result.reason == "below_threshold"


def test_script_is_idempotent(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    updates: list[dict] = []
    first = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value=_large_payload(),
        size_bytes=1024,
        settings=settings,
        min_bytes=512,
        mode="apply",
        update_reference=updates.append,
    )

    second = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value=first.reference,
        size_bytes=1024,
        settings=settings,
        min_bytes=512,
        mode="apply",
    )

    assert first.result == "migrated"
    assert second.result == "skipped"
    assert second.reason == "already_externalized"


def test_verify_only_catches_missing_reference_file(tmp_path: Path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)
    reference = build_payload_reference(tmp_path / "missing.json", "sha", 1, "db_payload_externalized_v1")

    result = process_payload_row(
        target=TARGET,
        pk="project-1",
        raw_value=reference,
        size_bytes=100,
        settings=settings,
        min_bytes=512,
        mode="verify_only",
    )

    assert result.result == "failed"
    assert "missing" in result.reason
