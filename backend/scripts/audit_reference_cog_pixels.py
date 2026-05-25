from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from shapely.geometry import shape
from shapely.ops import transform as shapely_transform
from pyproj import Transformer


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import get_settings


def _project_aoi(project_path: Path) -> dict:
    payload = json.loads(project_path.read_text())
    aoi = payload.get("aoi_geojson")
    if not isinstance(aoi, dict):
        raise SystemExit(f"Project has no aoi_geojson: {project_path}")
    return aoi


def _sample_windows(width: int, height: int, sample_step: int) -> list[tuple[int, int, int, int]]:
    windows: list[tuple[int, int, int, int]] = []
    for row in range(0, height, sample_step):
        for col in range(0, width, sample_step):
            windows.append((col, row, min(sample_step, width - col), min(sample_step, height - row)))
    return windows


def audit_cog(cog_path: Path, aoi_geojson: dict, sample_step: int) -> dict[str, object]:
    with rasterio.open(cog_path) as src:
        aoi_geom = shape(aoi_geojson)
        if src.crs and src.crs.to_string() != "EPSG:4326":
            transformer = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            aoi_geom = shapely_transform(transformer.transform, aoi_geom)

        totals = {
            "inside": 0,
            "black_inside": 0,
            "black_valid_inside": 0,
            "valid_inside": 0,
            "south_inside": 0,
            "south_visual_valid": 0,
            "south_mask_valid": 0,
        }
        for col, row, width, height in _sample_windows(src.width, src.height, sample_step):
            window = rasterio.windows.Window(col, row, width, height)
            transform = rasterio.windows.transform(window, src.transform)
            inside = ~geometry_mask([aoi_geom], out_shape=(height, width), transform=transform, invert=False)
            if not inside.any():
                continue
            data = src.read([1, 2, 3], window=window, boundless=False)
            mask = src.dataset_mask(window=window)
            black = np.all(data <= 2, axis=0)
            valid = mask > 0
            south = np.zeros((height, width), dtype=bool)
            south[(row + np.arange(height)) >= (src.height // 2), :] = True
            inside_count = int(inside.sum())
            totals["inside"] += inside_count
            totals["black_inside"] += int((inside & black).sum())
            totals["black_valid_inside"] += int((inside & black & valid).sum())
            totals["valid_inside"] += int((inside & valid).sum())
            totals["south_inside"] += int((inside & south).sum())
            totals["south_visual_valid"] += int((inside & south & ~black).sum())
            totals["south_mask_valid"] += int((inside & south & valid).sum())
        inside_total = max(int(totals["inside"]), 1)
        south_total = max(int(totals["south_inside"]), 1)
        return {
            "path": str(cog_path),
            "width": src.width,
            "height": src.height,
            "crs": str(src.crs),
            "black_pixels_inside_aoi": totals["black_inside"],
            "black_pixels_marked_valid": totals["black_valid_inside"],
            "valid_mask_coverage_pct": round(int(totals["valid_inside"]) / inside_total * 100, 4),
            "south_half_visual_valid_pct": round(int(totals["south_visual_valid"]) / south_total * 100, 4),
            "south_half_mask_valid_pct": round(int(totals["south_mask_valid"]) / south_total * 100, 4),
            "sampled_pixels_inside_aoi": totals["inside"],
            "sample_step": sample_step,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit reference imagery COG pixels and masks inside the project AOI.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--release", action="append", dest="releases", default=[])
    parser.add_argument("--sample-step", type=int, default=512)
    args = parser.parse_args()
    settings = get_settings()
    project_dir = settings.temporal_projects_dir / args.project_id
    project_path = project_dir / "project.json"
    aoi = _project_aoi(project_path)
    releases = args.releases or [p.name for p in (project_dir / "milestones").iterdir() if p.is_dir()]
    result = {
        release: audit_cog(project_dir / "milestones" / release / "reference_imagery_cog.tif", aoi, args.sample_step)
        for release in releases
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
