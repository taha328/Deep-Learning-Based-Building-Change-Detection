from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse

from src.api.deps import get_app_settings
from src.config import Settings
from src.domain.exports import create_export_bundle_from_manifest


router = APIRouter()


def is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


@router.get("")
def get_file(path: str = Query(..., min_length=1), settings: Settings = Depends(get_app_settings)) -> FileResponse:
    requested_path = Path(path).expanduser()
    if not requested_path.is_absolute():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path must be absolute")

    resolved_path = requested_path.resolve(strict=False)
    allowed_roots = {
        settings.runtime_cache_dir.resolve(),
        settings.request_cache_dir.resolve(),
        settings.temporal_projects_dir.resolve(),
        *{root.resolve() for root in settings.allowed_file_roots},
    }
    if not any(is_relative_to(resolved_path, root) for root in allowed_roots):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="path is outside approved directories")

    if not resolved_path.exists() or not resolved_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")

    return FileResponse(resolved_path)


@router.post("/runs/{run_id}/export-bundle")
def create_run_export_bundle(run_id: str, settings: Settings = Depends(get_app_settings)) -> dict[str, str]:
    request_dir = settings.request_cache_dir / run_id
    if not request_dir.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    bundle_path = create_export_bundle_from_manifest(request_dir, force=True)
    return {"path": str(bundle_path)}
