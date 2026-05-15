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
import urllib.error
import urllib.request
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
BACKEND_HEALTH_URL = "http://127.0.0.1:8000/api/health"
BACKEND_DOCS_URL = "http://127.0.0.1:8000/docs"
FRONTEND_URL = "http://127.0.0.1:5173/"


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


def drain_output(output_queue: queue.Queue[str]) -> None:
    while True:
        try:
            print(output_queue.get_nowait(), flush=True)
        except queue.Empty:
            return


def url_is_ready(url: str) -> bool:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def curl_url_verbose(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    """Test URL with curl and return (success, output)."""
    result = run_quiet(["curl", "-fsS", "-m", str(int(timeout)), url])
    return result.returncode == 0, result.stdout + result.stderr


def get_port_listeners(port: int) -> str:
    """Get lsof output for a port."""
    result = run_quiet(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"])
    return result.stdout.strip() if result.returncode == 0 else ""


def wait_for_url(
    *,
    label: str,
    url: str,
    output_queue: queue.Queue[str],
    processes: list[ManagedProcess],
    timeout: float = 60.0,
    fallback_url: str | None = None,
    fallback_port: int | None = None,
    is_frontend: bool = False,
) -> None:
    print(f"[dev] waiting for {label}: {url}", flush=True)
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        drain_output(output_queue)
        for managed in processes:
            return_code = managed.process.poll()
            if return_code is not None:
                raise RuntimeError(f"{managed.name} exited before {label} became ready: code {return_code}")
        if url_is_ready(url):
            print(f"[dev] {label} ready", flush=True)
            return
        if fallback_url and url_is_ready(fallback_url):
            print(f"[dev] {label} ready", flush=True)
            return
        if fallback_port is not None and not is_frontend and port_is_listening(fallback_port):
            print(f"[dev] {label} ready", flush=True)
            return
        
        # Store last error for frontend debugging
        if is_frontend and fallback_port is not None:
            success, output = curl_url_verbose(url, timeout=1.0)
            if not success:
                last_error = output
        
        time.sleep(0.5)
    
    error_msg = f"Timed out waiting for {label}: {url}"
    if is_frontend and last_error:
        port_info = get_port_listeners(fallback_port or 5173)
        error_msg += f"\n[dev] frontend is not reachable\n[dev] lsof output:\n"
        if port_info:
            for line in port_info.splitlines():
                error_msg += f"[dev]   {line}\n"
        else:
            error_msg += "[dev]   (port not listening)\n"
        error_msg += f"[dev] last curl error:\n[dev]   {last_error.strip()}"
    raise RuntimeError(error_msg)


def port_is_listening(port: int) -> bool:
    result = run_quiet(["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"])
    return result.returncode == 0 and bool(result.stdout.strip())


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


def run_stop_script(repo_root: Path) -> None:
    stop_script = repo_root / "scripts" / "dev_stop_all.sh"
    if not stop_script.exists():
        return
    result = run_quiet(["bash", str(stop_script)], cwd=repo_root)
    for line in result.stdout.splitlines():
        print(line, flush=True)
    for line in result.stderr.splitlines():
        print(line, flush=True)


def build_env(extra: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(extra)
    return env


def main() -> int:
    app_dev_open_browser = os.environ.get("APP_DEV_OPEN_BROWSER", "false").lower() in ("true", "1", "yes")
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
        wait_for_url(
            label="backend",
            url=BACKEND_HEALTH_URL,
            fallback_url=BACKEND_DOCS_URL,
            output_queue=output_queue,
            processes=processes,
            timeout=180.0,
        )
        processes.append(
            start_process(
                name="celery",
                command=[
                    str(backend_python),
                    "-m",
                    "celery",
                    "-A",
                    "src.jobs.celery_app.celery_app",
                    "worker",
                    "--loglevel=INFO",
                    "--queues",
                    os.environ.get("CELERY_TASK_DEFAULT_QUEUE", "building_change"),
                    "--pool",
                    os.environ.get("CELERY_WORKER_POOL", "solo"),
                    "--hostname",
                    "building_change_worker@%h",
                ],
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
        wait_for_url(
            label="frontend",
            url=FRONTEND_URL,
            output_queue=output_queue,
            processes=processes,
            timeout=120.0,
            fallback_port=5173,
            is_frontend=True,
        )

        print("[dev] ready", flush=True)
        print("[dev] frontend: http://127.0.0.1:5173/", flush=True)
        print("[dev] backend:  http://127.0.0.1:8000/", flush=True)
        print("[dev] open this exact URL:", flush=True)
        print(f"[dev]   {FRONTEND_URL}", flush=True)
        print("[dev] do not omit http://", flush=True)
        print("[dev] keep this terminal open; CTRL+C stops the stack", flush=True)
        print("[dev]", flush=True)
        print("[dev] if browser shows blank page, try:", flush=True)
        print("[dev]   - hard refresh: CMD+SHIFT+R (macOS) or CTRL+SHIFT+R (Linux/Windows)", flush=True)
        print("[dev]   - check backend ready at http://127.0.0.1:8000/docs", flush=True)
        
        if app_dev_open_browser:
            import subprocess as sp
            try:
                sp.run(["open", FRONTEND_URL], check=False)
                print("[dev] opening browser...", flush=True)
            except Exception:
                pass

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
        run_stop_script(repo_root)
        print("[dev] stack stopped", flush=True)
        print("[dev] localhost:5173 will refuse connection until you run ./scripts/dev_start_all.sh again", flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[dev] error: {exc}", file=sys.stderr, flush=True)
        stop_all(processes)
        run_stop_script(repo_root)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
