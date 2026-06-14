from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from qgis.PyQt.QtCore import QSettings, QStandardPaths


SETTINGS_PREFIX = "BuildingChangePlugin"
DEFAULT_BACKEND_BASE_URL = "http://127.0.0.1:8000"


@dataclass
class PluginSettings:
    backend_base_url: str = DEFAULT_BACKEND_BASE_URL
    last_project_id: str = ""
    output_dir: str = ""


def default_output_dir() -> str:
    documents = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    if documents:
        return str(Path(documents) / "BuildingChange")
    return str(Path.home() / "BuildingChange")


def load_plugin_settings() -> PluginSettings:
    settings = QSettings()
    return PluginSettings(
        backend_base_url=settings.value(
            f"{SETTINGS_PREFIX}/backendBaseUrl",
            DEFAULT_BACKEND_BASE_URL,
            type=str,
        ).rstrip("/"),
        last_project_id=settings.value(f"{SETTINGS_PREFIX}/lastProjectId", "", type=str),
        output_dir=settings.value(f"{SETTINGS_PREFIX}/outputDir", default_output_dir(), type=str),
    )


def save_plugin_settings(values: PluginSettings) -> None:
    settings = QSettings()
    settings.setValue(f"{SETTINGS_PREFIX}/backendBaseUrl", values.backend_base_url.rstrip("/"))
    settings.setValue(f"{SETTINGS_PREFIX}/lastProjectId", values.last_project_id)
    settings.setValue(f"{SETTINGS_PREFIX}/outputDir", values.output_dir)
