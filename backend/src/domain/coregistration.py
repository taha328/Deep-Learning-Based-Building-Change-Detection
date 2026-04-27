from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import shutil
import tempfile
from typing import Any

import numpy as np
import rasterio

from src.config import Settings
from src.utils.logging import get_logger


LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class CoregistrationDiagnostics:
    method: str
    used_arosics: bool
    fallback_reason: str | None = None
    corrected_t1_path: str | None = None
    corrected_t1_valid_mask_path: str | None = None
    invalid_mask_ref_path: str | None = None
    invalid_mask_tgt_path: str | None = None
    tie_point_count: int | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CoregistrationResult:
    corrected_t1_path: Path
    corrected_t1_valid_mask_path: Path
    diagnostics: CoregistrationDiagnostics


def _get_arosics_classes():
    from arosics import COREG_LOCAL, DESHIFTER

    return COREG_LOCAL, DESHIFTER


def _invert_valid_mask_to_baddata_mask(valid_mask_path: Path, output_path: Path) -> Path:
    with rasterio.open(valid_mask_path) as src:
        valid_mask = src.read(1) > 0
        profile = src.profile.copy()

    invalid_mask = (~valid_mask).astype(np.uint8)
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    profile.pop("tiled", None)
    profile.update(driver="GTiff", count=1, dtype="uint8", nodata=0, compress="LZW")

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(invalid_mask, 1)

    return output_path


def _extract_tie_point_count(coreg_local: Any) -> int | None:
    coreg_info = getattr(coreg_local, "coreg_info", None)
    if isinstance(coreg_info, dict):
        gcp_list = coreg_info.get("GCPList")
        if gcp_list is not None:
            return len(gcp_list)

    tiepoint_grid = getattr(coreg_local, "tiepoint_grid", None)
    if tiepoint_grid is not None:
        gcp_list = getattr(tiepoint_grid, "GCPList", None)
        if gcp_list is not None:
            return len(gcp_list)

    coreg_points = getattr(coreg_local, "CoRegPoints_table", None)
    if coreg_points is not None:
        try:
            return len(coreg_points)
        except TypeError:
            return None
    return None


def _run_mask_deshift(
    deshifter_cls: Any,
    *,
    source_mask_path: Path,
    coreg_info: dict[str, Any],
    output_path: Path,
    settings: Settings,
) -> Path:
    deshifter = deshifter_cls(
        str(source_mask_path),
        coreg_info,
        path_out=str(output_path),
        fmt_out="GTIFF",
        nodata=0,
        resamp_alg="nearest",
        CPUs=settings.arosics_cpus,
        progress=False,
        q=True,
    )
    deshifter.correct_shifts()
    return output_path


def _materialize_gdal_safe_path(source_path: Path, workspace_dir: Path, alias_name: str) -> Path:
    safe_path = workspace_dir / alias_name
    try:
        if safe_path.exists() or safe_path.is_symlink():
            safe_path.unlink()
        safe_path.symlink_to(source_path)
    except OSError:
        shutil.copy2(source_path, safe_path)
    return safe_path


def coregister_t1_to_t2_with_arosics(
    *,
    reference_image_path: Path,
    target_image_path: Path,
    reference_valid_mask_path: Path,
    target_valid_mask_path: Path,
    output_dir: Path,
    settings: Settings,
) -> CoregistrationResult:
    if not settings.arosics_enabled:
        return CoregistrationResult(
            corrected_t1_path=target_image_path,
            corrected_t1_valid_mask_path=target_valid_mask_path,
            diagnostics=CoregistrationDiagnostics(
                method="reprojection_only",
                used_arosics=False,
                fallback_reason="AROSICS disabled by configuration.",
            ),
        )

    invalid_ref_path = output_dir / "t2_invalid_mask_for_arosics.tif"
    invalid_tgt_path = output_dir / "t1_invalid_mask_for_arosics.tif"
    corrected_t1_path = output_dir / "t1_coregistered_to_t2.tif"
    corrected_t1_valid_mask_path = output_dir / "t1_valid_mask_coregistered_to_t2.tif"

    _invert_valid_mask_to_baddata_mask(reference_valid_mask_path, invalid_ref_path)
    _invert_valid_mask_to_baddata_mask(target_valid_mask_path, invalid_tgt_path)

    try:
        coreg_local_cls, deshifter_cls = _get_arosics_classes()
    except Exception as exc:
        message = f"AROSICS is unavailable in the backend environment: {type(exc).__name__}: {exc}"
        if settings.arosics_fallback_to_reprojection:
            LOGGER.warning("%s Falling back to reprojection-only alignment.", message)
            return CoregistrationResult(
                corrected_t1_path=target_image_path,
                corrected_t1_valid_mask_path=target_valid_mask_path,
                diagnostics=CoregistrationDiagnostics(
                    method="reprojection_fallback",
                    used_arosics=False,
                    fallback_reason=message,
                    invalid_mask_ref_path=str(invalid_ref_path),
                    invalid_mask_tgt_path=str(invalid_tgt_path),
                ),
            )
        raise RuntimeError(message) from exc

    try:
        with tempfile.TemporaryDirectory(prefix="bc_arosics_") as temp_dir_str:
            temp_dir = Path(temp_dir_str)
            safe_reference_path = _materialize_gdal_safe_path(reference_image_path, temp_dir, "reference.tif")
            safe_target_path = _materialize_gdal_safe_path(target_image_path, temp_dir, "target.tif")
            safe_invalid_ref_path = _materialize_gdal_safe_path(invalid_ref_path, temp_dir, "reference_invalid.tif")
            safe_invalid_tgt_path = _materialize_gdal_safe_path(invalid_tgt_path, temp_dir, "target_invalid.tif")
            safe_target_valid_mask_path = _materialize_gdal_safe_path(target_valid_mask_path, temp_dir, "target_valid.tif")
            safe_corrected_t1_path = temp_dir / "t1_coregistered_to_t2.tif"
            safe_corrected_t1_valid_mask_path = temp_dir / "t1_valid_mask_coregistered_to_t2.tif"

            coreg_local = coreg_local_cls(
                str(safe_reference_path),
                str(safe_target_path),
                grid_res=settings.arosics_grid_res,
                window_size=(settings.arosics_window_size, settings.arosics_window_size),
                path_out=str(safe_corrected_t1_path),
                fmt_out="GTIFF",
                max_shift=settings.arosics_max_shift,
                tieP_filter_level=settings.arosics_tieP_filter_level,
                min_reliability=settings.arosics_min_reliability,
                align_grids=settings.arosics_align_grids,
                match_gsd=settings.arosics_match_gsd,
                resamp_alg_calc=settings.arosics_resamp_alg_calc,
                resamp_alg_deshift=settings.arosics_resamp_alg_deshift,
                mask_baddata_ref=str(safe_invalid_ref_path),
                mask_baddata_tgt=str(safe_invalid_tgt_path),
                CPUs=settings.arosics_cpus,
                progress=False,
                q=True,
                ignore_errors=False,
            )
            coreg_local.correct_shifts()
            shutil.copy2(safe_corrected_t1_path, corrected_t1_path)
            warnings: list[str] = []

            try:
                _run_mask_deshift(
                    deshifter_cls,
                    source_mask_path=safe_target_valid_mask_path,
                    coreg_info=coreg_local.coreg_info,
                    output_path=safe_corrected_t1_valid_mask_path,
                    settings=settings,
                )
                shutil.copy2(safe_corrected_t1_valid_mask_path, corrected_t1_valid_mask_path)
            except Exception as mask_exc:
                warnings.append(
                    "AROSICS corrected the T1 RGB mosaic, but the T1 valid-mask deshift failed; "
                    f"falling back to the original T1 valid mask: {type(mask_exc).__name__}: {mask_exc}"
                )
                corrected_t1_valid_mask_path = target_valid_mask_path

        return CoregistrationResult(
            corrected_t1_path=corrected_t1_path,
            corrected_t1_valid_mask_path=corrected_t1_valid_mask_path,
            diagnostics=CoregistrationDiagnostics(
                method="arosics_local",
                used_arosics=True,
                corrected_t1_path=str(corrected_t1_path),
                corrected_t1_valid_mask_path=str(corrected_t1_valid_mask_path),
                invalid_mask_ref_path=str(invalid_ref_path),
                invalid_mask_tgt_path=str(invalid_tgt_path),
                tie_point_count=_extract_tie_point_count(coreg_local),
                warnings=warnings,
            ),
        )
    except Exception as exc:
        message = f"AROSICS local co-registration failed: {type(exc).__name__}: {exc}"
        if settings.arosics_fallback_to_reprojection:
            LOGGER.warning("%s Falling back to reprojection-only alignment.", message)
            return CoregistrationResult(
                corrected_t1_path=target_image_path,
                corrected_t1_valid_mask_path=target_valid_mask_path,
                diagnostics=CoregistrationDiagnostics(
                    method="reprojection_fallback",
                    used_arosics=False,
                    fallback_reason=message,
                    invalid_mask_ref_path=str(invalid_ref_path),
                    invalid_mask_tgt_path=str(invalid_tgt_path),
                ),
            )
        raise RuntimeError(message) from exc
