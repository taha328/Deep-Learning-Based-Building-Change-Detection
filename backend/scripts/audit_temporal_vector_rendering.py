from __future__ import annotations

import argparse
import json
import urllib.request
from urllib.parse import urljoin


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _head_or_get_status(url: str) -> tuple[int, int]:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
        return response.status, len(payload)


def _sample_tile_url(tilejson: dict) -> str | None:
    tiles = tilejson.get("tiles") or []
    if not tiles:
        return None
    bounds = tilejson.get("bounds")
    zoom = min(14, int(tilejson.get("maxzoom") or 14))
    if isinstance(bounds, list) and len(bounds) >= 4:
        lon = (float(bounds[0]) + float(bounds[2])) / 2
        lat = (float(bounds[1]) + float(bounds[3])) / 2
    else:
        lon = lat = 0.0
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 2**zoom
    x = int((lon + 180.0) / 360.0 * n)
    import math

    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return tiles[0].replace("{z}", str(zoom)).replace("{x}", str(x)).replace("{y}", str(y))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit temporal output vector-tile readiness for large artifacts.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--release", default=None)
    parser.add_argument("--feature-threshold", type=int, default=20_000)
    parser.add_argument("--size-threshold", type=int, default=25_000_000)
    args = parser.parse_args()
    project = _fetch_json(f"{args.base_url}/api/temporal-projects/{args.project_id}")
    rows = []
    for milestone in project.get("milestones", []):
        release = milestone.get("release_identifier")
        if args.release and release != args.release:
            continue
        for artifact in milestone.get("artifacts", []):
            feature_count = artifact.get("feature_count") or 0
            size_bytes = artifact.get("size_bytes") or 0
            huge = feature_count >= args.feature_threshold or size_bytes >= args.size_threshold
            tilejson_url = artifact.get("tilejson_url")
            tilejson_status = None
            tilejson_source_layer = None
            sample_mvt_status = None
            sample_mvt_bytes = None
            if tilejson_url:
                absolute_tilejson_url = urljoin(args.base_url.rstrip("/") + "/", str(tilejson_url).lstrip("/"))
                try:
                    tilejson = _fetch_json(absolute_tilejson_url)
                    tilejson_status = 200
                    tilejson_source_layer = (tilejson.get("vector_layers") or [{}])[0].get("id")
                    sample_url = _sample_tile_url(tilejson)
                    if sample_url:
                        sample_mvt_status, sample_mvt_bytes = _head_or_get_status(sample_url)
                except Exception as exc:  # pragma: no cover - command-line diagnostics
                    tilejson_status = f"ERR {exc}"
            rows.append(
                {
                    "release_identifier": release,
                    "key": artifact.get("key"),
                    "feature_count": feature_count,
                    "size_bytes": size_bytes,
                    "huge": huge,
                    "tilejson_url": tilejson_url,
                    "tilejson_status": tilejson_status,
                    "vector_source_layer": artifact.get("vector_source_layer"),
                    "tilejson_source_layer": tilejson_source_layer,
                    "sample_mvt_status": sample_mvt_status,
                    "sample_mvt_bytes": sample_mvt_bytes,
                    "vector_tile_ready": (not huge) or bool(artifact.get("tilejson_url")),
                }
            )
    print(
        json.dumps(
            {
                "project_id": args.project_id,
                "feature_threshold": args.feature_threshold,
                "size_threshold": args.size_threshold,
                "artifacts": rows,
                "huge_geojson_source_expected": False,
                "vector_tile_source_expected": any(row["huge"] for row in rows),
                "all_huge_artifacts_have_tilejson": all(row["vector_tile_ready"] for row in rows),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
