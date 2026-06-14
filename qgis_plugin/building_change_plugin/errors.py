from __future__ import annotations

from typing import Optional


class BuildingChangePluginError(RuntimeError):
    """Base class for plugin-facing errors."""


class BackendApiError(BuildingChangePluginError):
    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class LayerLoadError(BuildingChangePluginError):
    pass
