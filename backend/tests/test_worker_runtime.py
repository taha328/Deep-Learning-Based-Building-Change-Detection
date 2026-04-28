from __future__ import annotations

import os

import pytest

from src.config import Settings
from src.jobs.worker_runtime import build_backend_command, build_worker_command, collect_worker_diagnostics, validate_worker_environment


def test_build_backend_command_uses_scoped_reload_excludes(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)

    command = build_backend_command(settings, python_executable="/usr/bin/python3")

    assert command[:4] == ["/usr/bin/python3", "-m", "uvicorn", "src.api.main:app"]
    assert "--reload" in command
    assert "--reload-dir" in command
    assert "src" in command
    assert "--reload-exclude" in command
    assert ".venv/*" in command


def test_build_backend_command_can_disable_reload(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path)

    command = build_backend_command(settings, reload=False, python_executable="/usr/bin/python3")

    assert "--reload" not in command
    assert "--reload-dir" not in command


def test_build_worker_command_uses_solo_pool_without_concurrency(tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        celery_worker_pool="solo",
        celery_worker_concurrency=4,
    )

    command = build_worker_command(settings, python_executable="/usr/bin/python3")

    assert command[:4] == ["/usr/bin/python3", "-m", "celery", "-A"]
    assert "--pool" in command
    assert "solo" in command
    assert "--concurrency" not in command


def test_build_worker_command_uses_concurrency_for_prefork(tmp_path) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path,
        celery_worker_pool="prefork",
        celery_worker_concurrency=3,
    )

    command = build_worker_command(settings, python_executable="/usr/bin/python3")

    assert "--pool" in command
    assert "prefork" in command
    assert "--concurrency" in command
    assert "3" in command


def test_validate_worker_environment_rejects_user_site_leakage(monkeypatch) -> None:
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)

    with pytest.raises(RuntimeError) as exc_info:
        validate_worker_environment(
            {
                "pythonno_usersite_env": None,
                "python_executable": "/usr/bin/python3",
                "venv_root": "/tmp/backend/.venv",
                "no_user_site_flag": False,
                "site_enable_user_site": True,
                "user_site_in_sys_path": True,
                "module_paths": {
                    "fastapi": "unavailable: missing",
                    "uvicorn": "unavailable: missing",
                    "celery": "unavailable: missing",
                    "redis": "unavailable: missing",
                },
                "registered_tasks": ["unavailable: missing"],
            }
        )

    assert "PYTHONNOUSERSITE" in str(exc_info.value)
    assert "python -m pip install -r requirements.txt" in str(exc_info.value)


def test_collect_worker_diagnostics_returns_basic_metadata() -> None:
    diagnostics = collect_worker_diagnostics()

    assert diagnostics["python_executable"]
    assert diagnostics["python_version"]
    assert "celery_module" in diagnostics
