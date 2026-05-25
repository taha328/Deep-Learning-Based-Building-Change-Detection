from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src.config import get_settings
from src.services.temporal_projects import (
    audit_temporal_project_metadata_bloat,
    audit_temporal_project_metrics,
    publish_completed_tiled_request,
    remove_empty_baseline_output_artifacts_from_metadata,
    repair_temporal_project_reference_imagery_for_publication,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish an already completed tiled inference request into an existing temporal project."
    )
    parser.add_argument("--request-id", required=True, help="Completed request/run hash under runtime_cache/requests.")
    parser.add_argument("--project-id", required=True, help="Temporal project id to update.")
    parser.add_argument("--target-release", required=True, help="Milestone release receiving the request outputs.")
    parser.add_argument("--baseline-release", default=None, help="Expected baseline milestone release for safety.")
    parser.add_argument(
        "--repair-reference-imagery",
        action="store_true",
        help="Repair and validate milestone reference imagery COGs using existing shared Wayback mosaics.",
    )
    parser.add_argument(
        "--audit-metrics",
        action="store_true",
        help="Audit published GeoJSON feature counts and geodesic areas against milestone metrics.",
    )
    parser.add_argument(
        "--repair-metadata",
        action="store_true",
        help="Detect metadata bloat and report whether safe reference-based repair was applied.",
    )
    parser.add_argument(
        "--repair-reference-mask",
        action="store_true",
        help="Alias for --repair-reference-imagery; validates/rebuilds reference COG masks from existing mosaics.",
    )
    parser.add_argument(
        "--invalidate-reference-tile-cache",
        action="store_true",
        help="Remove derived reference PNG tile cache for this project so versioned tiles are regenerated.",
    )
    parser.add_argument(
        "--build-vector-tiles",
        action="store_true",
        help="Report vector tile endpoint readiness for large artifacts. Tiles are generated lazily by the API.",
    )
    parser.add_argument(
        "--skip-empty-baseline-output-layers",
        action="store_true",
        help="Remove empty baseline output artifact metadata so clients do not register fake baseline result layers.",
    )
    args = parser.parse_args()

    settings = get_settings()
    result = publish_completed_tiled_request(
        request_id=args.request_id,
        project_id=args.project_id,
        target_release=args.target_release,
        baseline_release=args.baseline_release,
        settings=settings,
    )
    if args.repair_reference_imagery or args.repair_reference_mask:
        result["reference_imagery_repair"] = repair_temporal_project_reference_imagery_for_publication(
            project_id=args.project_id,
            settings=settings,
        )
    if args.invalidate_reference_tile_cache:
        cache_root = settings.reference_tile_cache_dir / args.project_id
        removed = 0
        if cache_root.exists():
            for release_dir in cache_root.iterdir():
                if release_dir.is_dir():
                    shutil.rmtree(release_dir)
                    removed += 1
        result["reference_tile_cache_invalidation"] = {
            "cache_root": str(cache_root),
            "removed_release_cache_dirs": removed,
        }
    if args.skip_empty_baseline_output_layers:
        result["baseline_empty_output_layer_filter"] = remove_empty_baseline_output_artifacts_from_metadata(
            project_id=args.project_id,
            settings=settings,
        )
    if args.build_vector_tiles:
        result["vector_tiles"] = {
            "mode": "lazy_api",
            "tilejson_template": "/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}/tilejson.json",
            "tiles_template": "/api/temporal-projects/{project_id}/milestones/{release_identifier}/artifacts/{artifact_key}/tiles/{z}/{x}/{y}.mvt",
        }
    if args.audit_metrics:
        result["metric_audit"] = audit_temporal_project_metrics(
            project_id=args.project_id,
            target_release=args.target_release,
            settings=settings,
        )
    if args.repair_metadata:
        result["metadata_audit"] = audit_temporal_project_metadata_bloat(
            project_id=args.project_id,
            settings=settings,
            repair_metadata=True,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
