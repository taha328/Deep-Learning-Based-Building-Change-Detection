from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.setup_postgis_db import (
    DEFAULT_DATABASE_URL,
    EXPECTED_TABLES,
    maintenance_url,
    parse_target_url,
    redact_database_url,
    resolve_database_url,
)


def test_parse_target_url_and_maintenance_url() -> None:
    database_url = "postgresql+psycopg://building_change:building_change@localhost:5432/building_change"

    target = parse_target_url(database_url)
    maintenance = maintenance_url(target)

    assert target.username == "building_change"
    assert target.database == "building_change"
    assert maintenance.database == "postgres"
    assert maintenance.username == "building_change"


def test_resolve_database_url_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert resolve_database_url(None) == DEFAULT_DATABASE_URL

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://env_user:env_pass@localhost:5432/env_db")
    assert resolve_database_url(None) == "postgresql+psycopg://env_user:env_pass@localhost:5432/env_db"
    assert resolve_database_url("postgresql+psycopg://cli_user:cli_pass@localhost:5432/cli_db") == (
        "postgresql+psycopg://cli_user:cli_pass@localhost:5432/cli_db"
    )


def test_redact_database_url_hides_password() -> None:
    raw_url = "postgresql+psycopg://building_change:super_secret@localhost:5432/building_change"
    redacted = redact_database_url(raw_url)

    assert "super_secret" not in redacted
    assert "***" in redacted
    assert "building_change:***@localhost:5432/building_change" in redacted


def test_expected_tables_include_required_postgis_schema() -> None:
    required = {
        "projects",
        "milestones",
        "milestone_metrics",
        "runs",
        "artifacts",
        "geometry_layers",
        "alembic_version",
    }
    assert required.issubset(set(EXPECTED_TABLES))


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_setup_script_runs_migrate_and_verify_for_test_database() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    setup_script = backend_root / "scripts" / "setup_postgis_db.py"
    database_url = os.environ["TEST_DATABASE_URL"]
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url

    subprocess.run(
        [sys.executable, str(setup_script), "--database-url", database_url, "--migrate", "--verify"],
        cwd=backend_root,
        env=env,
        check=True,
    )
