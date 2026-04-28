#!/usr/bin/env python3
from __future__ import annotations

import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO


BACKEND_ENV = {
    "PYTHONNOUSERSITE": "1",
    "DATABASE_URL": "postgresql+psycopg://building_change:building_change@localhost:5432/building_change",
    "PERSISTENCE_BACKEND": "postgres",
    "REDIS_URL": "redis://localhost:6379/0",
}
FRONTEND_ENV = {
    "VITE_FASTAPI_BACKEND_URL": "http://127.0.0.1:8000",
}


@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[str]


def find_repo_root() -> Path:
    current = Path(__file__).resolve()
    for candidate in (current.parent, *current.parents):
        if (candidate / "backend").is_dir() and (candidate / "frontend").is_dir():
            return candidate
    raise RuntimeError("Could not find repository root containing backend/ and frontend/.")


def verify_layout(repo_root: Path) -> tuple[Path, Path]:
    backend_python = repo_root / "backend" / ".venv" / "bin" / "python"
    frontend_package = repo_root / "frontend" / "package.json"
    if not backend_python.exists():
        raise RuntimeError(f"Missing backend virtualenv Python: {backend_python}")
    if not frontend_package.exists():
        raise RuntimeError(f"Missing frontend package.json: {frontend_package}")
    return backend_python, frontend_package


def run_quiet(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def redis_is_ready() -> bool:
    if shutil.which("redis-cli") is None:
        return False
    result = run_quiet(["redis-cli", "ping"])
    return result.returncode == 0 and result.stdout.strip() == "PONG"


def start_redis_if_needed() -> bool:
    if redis_is_ready():
        print("[redis] already running", flush=True)
        return False

    if shutil.which("brew") is None:
        raise RuntimeError("Redis is not running and Homebrew was not found. Start Redis manually, then rerun this script.")

    print("[redis] starting Homebrew Redis service", flush=True)
    result = run_quiet(["brew", "services", "start", "redis"])
    if result.stdout.strip():
        for line in result.stdout.splitlines():
            print(f"[redis] {line}", flush=True)
    if result.stderr.strip():
        for line in result.stderr.splitlines():
            print(f"[redis] {line}", flush=True)

    for _ in range(30):
        if redis_is_ready():
            print("[redis] ready", flush=True)
            return True
        time.sleep(0.5)

    raise RuntimeError("Redis did not respond to redis-cli ping after starting Homebrew service.")


def enqueue_output(prefix: str, stream: TextIO, output_queue: queue.Queue[str]) -> None:
    try:
        for line in iter(stream.readline, ""):
            output_queue.put(f"[{prefix}] {line.rstrip()}")
    finally:
        stream.close()


def start_process(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    output_queue: queue.Queue[str],
) -> ManagedProcess:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        start_new_session=True,
    )
    assert process.stdout is not None
    threading.Thread(target=enqueue_output, args=(name, process.stdout, output_queue), daemon=True).start()
    print(f"[{name}] started pid={process.pid}", flush=True)
    return ManagedProcess(name=name, process=process)


def terminate_process(managed: ManagedProcess, *, timeout: float = 10.0) -> None:
    process = managed.process
    if process.poll() is not None:
        return

    print(f"[{managed.name}] stopping pid={process.pid}", flush=True)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"[{managed.name}] did not stop after SIGTERM; sending SIGKILL", flush=True)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        process.wait(timeout=5)


def stop_all(processes: list[ManagedProcess]) -> None:
    for managed in reversed(processes):
        terminate_process(managed)


def build_env(extra: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(extra)
    return env


def main() -> int:
    repo_root = find_repo_root()
    backend_python, _frontend_package = verify_layout(repo_root)
    output_queue: queue.Queue[str] = queue.Queue()
    processes: list[ManagedProcess] = []

    print(f"[dev] repo root: {repo_root}", flush=True)
    start_redis_if_needed()

    backend_dir = repo_root / "backend"
    frontend_dir = repo_root / "frontend"
    backend_env = build_env(BACKEND_ENV)
    frontend_env = build_env(FRONTEND_ENV)

    try:
        processes.append(
            start_process(
                name="backend",
                command=[str(backend_python), "scripts/start_backend.py"],
                cwd=backend_dir,
                env=backend_env,
                output_queue=output_queue,
            )
        )
        processes.append(
            start_process(
                name="celery",
                command=[str(backend_python), "scripts/start_celery_worker.py"],
                cwd=backend_dir,
                env=backend_env,
                output_queue=output_queue,
            )
        )
        processes.append(
            start_process(
                name="frontend",
                command=["npm", "run", "dev"],
                cwd=frontend_dir,
                env=frontend_env,
                output_queue=output_queue,
            )
        )

        while True:
            for managed in processes:
                return_code = managed.process.poll()
                if return_code is not None:
                    print(f"[dev] {managed.name} exited unexpectedly with code {return_code}; stopping stack", flush=True)
                    stop_all(processes)
                    return return_code or 1

            try:
                line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            print(line, flush=True)
    except KeyboardInterrupt:
        print("\n[dev] CTRL+C received; stopping stack", flush=True)
        stop_all(processes)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[dev] error: {exc}", file=sys.stderr, flush=True)
        stop_all(processes)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
