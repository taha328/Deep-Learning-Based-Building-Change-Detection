from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import QgsTask

from .api_client import BackendClient
from .errors import BackendApiError


class ApiTaskSignals(QObject):
    resultReady = pyqtSignal(object)
    errorRaised = pyqtSignal(str)
    progressChanged = pyqtSignal(float, str)


class ApiTask(QgsTask):
    def __init__(self, description: str, client: BackendClient, operation: str, **kwargs: Any) -> None:
        super().__init__(description, QgsTask.CanCancel)
        self.client = client
        self.operation = operation
        self.kwargs = kwargs
        self.signals = ApiTaskSignals()
        self._result: Any = None
        self._error: Optional[str] = None

    def run(self) -> bool:
        try:
            self._result = self._execute()
            return True
        except Exception as exc:  # noqa: BLE001 - converted to QGIS message on the UI thread
            self._error = str(exc)
            return False

    def finished(self, result: bool) -> None:
        if result:
            self.signals.resultReady.emit(self._result)
            return
        self.signals.errorRaised.emit(self._error or "Background task failed.")

    def _execute(self) -> Any:
        if self.operation == "health":
            return self.client.health()
        if self.operation == "releases":
            return self.client.list_releases()
        if self.operation == "projects":
            return self.client.list_temporal_projects()
        if self.operation == "get_project":
            return self.client.get_temporal_project(str(self.kwargs["project_id"]))
        if self.operation == "save_project":
            return self.client.save_temporal_project(dict(self.kwargs["project"]))
        if self.operation == "validate_project":
            return self.client.validate_temporal_project(dict(self.kwargs["project"]))
        if self.operation == "run_project":
            return self._run_project_and_poll(str(self.kwargs["project_id"]))
        if self.operation == "download_export":
            return self.client.download_export(
                str(self.kwargs["project_id"]),
                str(self.kwargs["export_name"]),
                Path(str(self.kwargs["target_path"])),
            )
        raise BackendApiError(f"Unknown plugin task operation: {self.operation}")

    def _run_project_and_poll(self, project_id: str) -> Dict[str, Any]:
        start = self.client.start_temporal_project_job(project_id)
        job_id = str(start.get("job_id") or "")
        if not job_id:
            raise BackendApiError("Backend did not return a job id.")
        while not self.isCanceled():
            job = self.client.get_job(job_id)
            progress = float(job.get("progress") or 0.0)
            message = str(job.get("message") or job.get("stage") or "Traitement en cours")
            self.setProgress(progress)
            self.signals.progressChanged.emit(progress / 100.0, message)
            status = job.get("status")
            if status == "completed":
                return self.client.get_temporal_project(project_id)
            if status in {"failed", "cancelled"}:
                raise BackendApiError(str(job.get("error_message") or f"Job {status}."))
            time.sleep(2.0)
        raise BackendApiError("Job cancelled.")
