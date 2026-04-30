from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from src.config import Settings
from src.utils.logging import get_logger


LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class CoregistrationDiagnostics:
    method: str
    fallback_reason: str | None = None
    corrected_t1_path: str | None = None
    corrected_t1_valid_mask_path: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoregistrationResult:
    corrected_t1_path: Path
    corrected_t1_valid_mask_path: Path
    diagnostics: CoregistrationDiagnostics


def coregister_t1_to_t2_reprojection_only(
    *,
    reference_image_path: Path,
    target_image_path: Path,
    reference_valid_mask_path: Path,
    target_valid_mask_path: Path,
    output_dir: Path,
    settings: Settings,
) -> CoregistrationResult:
    """Return original T1 paths; downstream raster reprojection handles grid alignment."""
    del reference_image_path, reference_valid_mask_path, output_dir, settings
    LOGGER.info("Using reprojection-only mosaic alignment.")
    return CoregistrationResult(
        corrected_t1_path=target_image_path,
        corrected_t1_valid_mask_path=target_valid_mask_path,
        diagnostics=CoregistrationDiagnostics(
            method="reprojection_only",
            corrected_t1_path=str(target_image_path),
            corrected_t1_valid_mask_path=str(target_valid_mask_path),
        ),
    )
