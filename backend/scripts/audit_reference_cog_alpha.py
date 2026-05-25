from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402
from src.services.temporal_reference_imagery import (  # noqa: E402
    clear_reference_tile_cache,
    clear_reference_tilejson_cache,
    ensure_reference_imagery_cog,
    reference_imagery_version_token,
    resolve_temporal_reference_cog,
)
from src.schemas import TemporalReferenceImagery  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("reference_cog_alpha_audit")


def _audit_cog(project_id: str, release_identifier: str, cog_path: Path, sample_size: int) -> dict[str, object]:
    started_at = time.perf_counter()
    logger.info("REFERENCE_COG_AUDIT_START project_id=%s release_identifier=%s cog_path=%s", project_id, release_identifier, cog_path)
    with rasterio.open(cog_path) as src:
        colorinterp = [item.name for item in src.colorinterp]
        has_alpha = "alpha" in colorinterp
        has_internal_mask = any("per_dataset" in [flag.name for flag in flags] for flags in src.mask_flag_enums)
        h = min(src.height, sample_size)
        w = min(src.width, sample_size)
        rgb = src.read([1, 2, 3], out_shape=(3, h, w), resampling=Resampling.nearest)
        mask = src.dataset_mask(out_shape=(h, w), resampling=Resampling.nearest)
        alpha = src.read(4, out_shape=(h, w), resampling=Resampling.nearest) if src.count >= 4 and has_alpha else mask
        black = np.all(rgb == 0, axis=0)
        black_valid_pixel_count = int(np.logical_and(black, alpha > 0).sum())
        alpha_zero_count = int((alpha == 0).sum())
        alpha_255_count = int((alpha == 255).sum())
        result = {
            "project_id": project_id,
            "release_identifier": release_identifier,
            "cog_path": str(cog_path),
            "file_size": cog_path.stat().st_size,
            "band_count": src.count,
            "colorinterp": colorinterp,
            "has_alpha": has_alpha,
            "has_internal_mask": has_internal_mask,
            "nodata": src.nodata,
            "width": src.width,
            "height": src.height,
            "crs": str(src.crs),
            "bounds": tuple(float(v) for v in src.bounds),
            "mask_min": int(mask.min()),
            "mask_max": int(mask.max()),
            "black_valid_pixel_count": black_valid_pixel_count,
            "alpha_zero_count": alpha_zero_count,
            "alpha_255_count": alpha_255_count,
            "pass": bool(has_alpha and alpha_zero_count > 0 and alpha_255_count > 0 and black_valid_pixel_count == 0),
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }
    logger.info(
        "REFERENCE_COG_AUDIT_RESULT project_id=%s release_identifier=%s cog_path=%s band_count=%s has_alpha=%s has_internal_mask=%s nodata=%s black_valid_pixel_count=%s alpha_zero_count=%s alpha_255_count=%s duration_ms=%s",
        project_id,
        release_identifier,
        cog_path,
        result["band_count"],
        result["has_alpha"],
        result["has_internal_mask"],
        result["nodata"],
        result["black_valid_pixel_count"],
        result["alpha_zero_count"],
        result["alpha_255_count"],
        result["duration_ms"],
    )
    if black_valid_pixel_count:
        logger.info(
            "REFERENCE_COG_BLACK_VALID_PIXELS_FOUND project_id=%s release_identifier=%s cog_path=%s black_valid_pixel_count=%s",
            project_id,
            release_identifier,
            cog_path,
            black_valid_pixel_count,
        )
    if not has_alpha:
        logger.info("REFERENCE_COG_ALPHA_MISSING project_id=%s release_identifier=%s cog_path=%s band_count=%s", project_id, release_identifier, cog_path, result["band_count"])
    if not has_internal_mask:
        logger.info("REFERENCE_COG_INTERNAL_MASK_MISSING project_id=%s release_identifier=%s cog_path=%s", project_id, release_identifier, cog_path)
    return result


def _repair_cog(project_id: str, release_identifier: str, cog_path: Path, backup: bool) -> None:
    logger.info("REFERENCE_COG_REPAIR_START project_id=%s release_identifier=%s cog_path=%s", project_id, release_identifier, cog_path)
    started_at = time.perf_counter()
    if backup:
        backup_path = cog_path.with_suffix(".pre_alpha_repair.bak.tif")
        if not backup_path.exists():
            shutil.copy2(cog_path, backup_path)
    ensure_reference_imagery_cog(
        cog_path,
        cog_path,
        project_id=project_id,
        release_identifier=release_identifier,
    )
    clear_reference_tile_cache()
    clear_reference_tilejson_cache()
    reference = TemporalReferenceImagery(cog_path=str(cog_path), storage_strategy="raster_tiles")
    info = resolve_temporal_reference_cog(reference)
    version = reference_imagery_version_token(info) if info else ""
    logger.info("REFERENCE_COG_REPAIR_CACHE_INVALIDATED project_id=%s release_identifier=%s cog_path=%s", project_id, release_identifier, cog_path)
    logger.info("REFERENCE_TILE_VERSION_UPDATED project_id=%s release_identifier=%s cog_path=%s version=%s", project_id, release_identifier, cog_path, version)
    logger.info(
        "REFERENCE_COG_REPAIR_DONE project_id=%s release_identifier=%s cog_path=%s duration_ms=%s",
        project_id,
        release_identifier,
        cog_path,
        round((time.perf_counter() - started_at) * 1000, 2),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit and optionally repair temporal reference COG alpha transparency.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--runtime-cache-dir", default=None)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--sample-size", type=int, default=1024)
    args = parser.parse_args()

    settings = Settings(runtime_cache_dir=Path(args.runtime_cache_dir)) if args.runtime_cache_dir else Settings()
    project_dir = settings.temporal_projects_dir / args.project_id
    project_json = project_dir / "project.json"
    payload = json.loads(project_json.read_text())
    releases = [
        str(milestone.get("release_identifier"))
        for milestone in payload.get("milestones", [])
        if isinstance(milestone, dict) and milestone.get("release_identifier")
    ]
    results = []
    for release in releases:
        cog_path = project_dir / "milestones" / release / "reference_imagery_cog.tif"
        if not cog_path.is_file():
            logger.info("REFERENCE_COG_AUDIT_RESULT project_id=%s release_identifier=%s cog_path=%s pass=false reason=missing_cog", args.project_id, release, cog_path)
            continue
        before = _audit_cog(args.project_id, release, cog_path, args.sample_size)
        if args.repair and not before["pass"]:
            try:
                _repair_cog(args.project_id, release, cog_path, args.backup)
            except Exception as exc:  # noqa: BLE001
                logger.exception("REFERENCE_COG_REPAIR_FAILED project_id=%s release_identifier=%s cog_path=%s error=%s", args.project_id, release, cog_path, exc)
                return 1
        results.append(_audit_cog(args.project_id, release, cog_path, args.sample_size))
    print(json.dumps(results, indent=2))
    return 0 if all(result["pass"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
