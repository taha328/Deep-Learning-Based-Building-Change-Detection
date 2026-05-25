from __future__ import annotations

import argparse
import json
import urllib.request


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit project/tilejson reference imagery configuration used by frontend.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", required=True)
    args = parser.parse_args()
    project_url = f"{args.base_url}/api/temporal-projects/{args.project_id}"
    project = _fetch_json(project_url)
    result: dict[str, object] = {
        "project_id": args.project_id,
        "project_url": project_url,
        "milestones": {},
    }
    for milestone in project.get("milestones", []):
        release = milestone.get("release_identifier")
        imagery = milestone.get("reference_imagery") or {}
        tilejson_url = imagery.get("tilejson_url")
        if tilejson_url and tilejson_url.startswith("/"):
            tilejson_url = f"{args.base_url}{tilejson_url}"
        entry = {
            "has_reference_imagery": bool(imagery),
            "tilejson_url": tilejson_url,
            "tiles_url_template": imagery.get("tiles_url_template"),
            "bounds": imagery.get("raster_bounds_wgs84"),
            "tile_url_includes_version": False,
            "tilejson_bounds": None,
        }
        if tilejson_url:
            tilejson = _fetch_json(tilejson_url)
            entry["tile_url_includes_version"] = "?v=" in json.dumps(tilejson.get("tiles", []))
            entry["tilejson_bounds"] = tilejson.get("bounds")
        result["milestones"][release] = entry
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
