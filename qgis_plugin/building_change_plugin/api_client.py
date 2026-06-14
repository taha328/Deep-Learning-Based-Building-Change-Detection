from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from .errors import BackendApiError


class BackendClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def absolute_url(self, url_or_path: str) -> str:
        return urljoin(self.base_url, url_or_path.lstrip("/"))

    def health(self) -> Dict[str, Any]:
        return self._request_json("GET", "/api/health")

    def list_releases(self) -> List[Dict[str, Any]]:
        payload = self._request_json("GET", "/api/releases")
        releases = payload.get("releases") if isinstance(payload, dict) else None
        return releases if isinstance(releases, list) else []

    def list_temporal_projects(self) -> List[Dict[str, Any]]:
        payload = self._request_json("GET", "/api/temporal-projects?include_cached_runs=true")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            projects = payload.get("projects") or payload.get("items") or payload.get("data")
            return projects if isinstance(projects, list) else []
        return []

    def get_temporal_project(self, project_id: str) -> Dict[str, Any]:
        return self._request_json("GET", f"/api/temporal-projects/{project_id}")

    def save_temporal_project(self, project: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json("POST", "/api/temporal-projects", {"project": project})

    def validate_temporal_project(self, project: Dict[str, Any]) -> Dict[str, Any]:
        return self._request_json("POST", "/api/temporal-projects/validate", {"project": project})

    def start_temporal_project_job(self, project_id: str) -> Dict[str, Any]:
        return self._request_json("POST", f"/api/jobs/temporal-projects/{project_id}")

    def get_job(self, job_id: str) -> Dict[str, Any]:
        return self._request_json("GET", f"/api/jobs/{job_id}")

    def download_export(self, project_id: str, export_name: str, target_path: Path) -> Path:
        if export_name not in {"results.xlsx", "results.kml"}:
            raise BackendApiError(f"Unsupported export: {export_name}")
        payload = self._request_bytes("GET", f"/api/temporal-projects/{project_id}/exports/{export_name}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(payload)
        return target_path

    def download_artifact(self, url_or_path: str, target_path: Path) -> Path:
        payload = self._request_bytes("GET", url_or_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(payload)
        return target_path

    def _request_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(self.absolute_url(path), data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                data = response.read()
        except HTTPError as exc:
            raise BackendApiError(_http_error_message(exc), status_code=exc.code) from exc
        except URLError as exc:
            raise BackendApiError(f"Backend unavailable: {exc.reason}") from exc
        except OSError as exc:
            raise BackendApiError(f"Backend connection failed: {exc}") from exc
        if not data:
            return {}
        try:
            return json.loads(data.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise BackendApiError("Backend returned invalid JSON.") from exc

    def _request_bytes(self, method: str, path: str) -> bytes:
        request = Request(self.absolute_url(path), headers={"Accept": "*/*"}, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except HTTPError as exc:
            raise BackendApiError(_http_error_message(exc), status_code=exc.code) from exc
        except URLError as exc:
            raise BackendApiError(f"Backend unavailable: {exc.reason}") from exc
        except OSError as exc:
            raise BackendApiError(f"Backend connection failed: {exc}") from exc


def _http_error_message(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except Exception:
        return f"Backend HTTP {exc.code}: {exc.reason}"
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("code")
            if message:
                return str(message)
        if isinstance(detail, str):
            return detail
    return f"Backend HTTP {exc.code}: {exc.reason}"
