from __future__ import annotations

import json
import os
import site
import sys
from importlib import import_module
from pathlib import Path
from typing import Any

from src.config import Settings


BACKEND_ROOT = Path(__file__).resolve().parents[2]
VENV_ROOT = BACKEND_ROOT / ".venv"
REQUIRED_WORKER_MODULES = ("fastapi", "uvicorn", "celery", "redis")
REQUIRED_CELERY_TASKS = ("building_change.run_temporal_project", "building_change.run_detection")


def build_backend_command(
    settings: Settings,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = True,
    python_executable: str | None = None,
) -> list[str]:
    python = python_executable or sys.executable
    command = [
        python,
        "-m",
        "uvicorn",
        "src.api.main:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if reload:
        command.extend(
            [
                "--reload",
                "--reload-dir",
                "src",
                "--reload-exclude",
                ".venv/*",
                "--reload-exclude",
                "__pycache__/*",
                "--reload-exclude",
                ".pytest_cache/*",
                "--reload-exclude",
                "runtime_cache/*",
                "--reload-exclude",
                "runtime_cache_bandon_integration/*",
                "--reload-exclude",
                "*.pyc",
                "--reload-exclude",
                "*.zip",
            ]
        )
    return command


def build_worker_command(settings: Settings, *, python_executable: str | None = None) -> list[str]:
    python = python_executable or sys.executable
    command = [
        python,
        "-m",
        "celery",
        "-A",
        "src.jobs.celery_app.celery_app",
        "worker",
        "--loglevel=INFO",
        "--queues",
        settings.celery_task_default_queue,
        "--pool",
        settings.celery_worker_pool,
    ]
    if settings.celery_worker_pool != "solo":
        command.extend(["--concurrency", str(max(1, settings.celery_worker_concurrency))])
    return command


def _module_path(module_name: str) -> str:
    try:
        module = import_module(module_name)
        return str(getattr(module, "__file__", "built-in"))
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {type(exc).__name__}: {exc}"


def _registered_tasks() -> list[str]:
    try:
        from src.jobs.celery_app import celery_app

        celery_app.loader.import_default_modules()
        return sorted(celery_app.tasks.keys())
    except Exception as exc:  # noqa: BLE001
        return [f"unavailable: {type(exc).__name__}: {exc}"]


def collect_worker_diagnostics() -> dict[str, Any]:
    try:
        user_site = site.getusersitepackages()
    except Exception:  # noqa: BLE001
        user_site = None

    module_paths = {module_name: _module_path(module_name) for module_name in REQUIRED_WORKER_MODULES}

    return {
        "backend_root": str(BACKEND_ROOT),
        "venv_root": str(VENV_ROOT),
        "python_executable": sys.executable,
        "python_version": sys.version,
        "pythonno_usersite_env": os.getenv("PYTHONNOUSERSITE"),
        "no_user_site_flag": bool(getattr(sys.flags, "no_user_site", 0)),
        "site_enable_user_site": getattr(site, "ENABLE_USER_SITE", None),
        "user_site": user_site,
        "user_base": site.getuserbase(),
        "user_site_in_sys_path": isinstance(user_site, str) and user_site in sys.path,
        "module_paths": module_paths,
        "celery_module": module_paths.get("celery"),
        "registered_tasks": _registered_tasks(),
    }


def validate_worker_environment(diagnostics: dict[str, Any]) -> None:
    issues: list[str] = []
    python_executable = str(diagnostics.get("python_executable") or "")
    venv_root = str(diagnostics.get("venv_root") or VENV_ROOT)
    if not python_executable.startswith(venv_root):
        issues.append(f"Python executable must be inside backend/.venv. Current executable: {python_executable}")
    env_flag = str(diagnostics.get("pythonno_usersite_env") or "").strip().lower()
    if env_flag not in {"1", "true", "yes", "on"}:
        issues.append("PYTHONNOUSERSITE must be set to 1 for the local worker.")
    if not diagnostics.get("no_user_site_flag"):
        issues.append("The interpreter is not running with the no-user-site flag enabled.")
    if diagnostics.get("site_enable_user_site") is True:
        issues.append("Python user-site packages are enabled.")
    if diagnostics.get("user_site_in_sys_path"):
        issues.append("The Python user site directory is present on sys.path.")

    module_paths = diagnostics.get("module_paths") or {}
    if isinstance(module_paths, dict):
        for module_name in REQUIRED_WORKER_MODULES:
            module_path = str(module_paths.get(module_name) or "")
            if module_path.startswith("unavailable:"):
                issues.append(f"{module_name} is missing: {module_path}")
            elif venv_root not in module_path:
                issues.append(f"{module_name} must import from backend/.venv. Current path: {module_path}")
    else:
        issues.append("Module import diagnostics were unavailable.")

    registered_tasks = diagnostics.get("registered_tasks") or []
    if isinstance(registered_tasks, list) and registered_tasks and str(registered_tasks[0]).startswith("unavailable:"):
        issues.append(str(registered_tasks[0]))
    elif isinstance(registered_tasks, list):
        missing_tasks = [task for task in REQUIRED_CELERY_TASKS if task not in registered_tasks]
        if missing_tasks:
            issues.append(f"Celery app is missing required tasks: {', '.join(missing_tasks)}")
    else:
        issues.append("Celery task diagnostics were unavailable.")

    if issues:
        message = " | ".join(issues)
        if any("missing:" in issue or "unavailable:" in issue for issue in issues):
            message = f"{message} | Install backend dependencies with: python -m pip install -r requirements.txt"
        raise RuntimeError(message)


def format_worker_diagnostics(diagnostics: dict[str, Any]) -> str:
    return json.dumps(diagnostics, indent=2, sort_keys=True, default=str)
