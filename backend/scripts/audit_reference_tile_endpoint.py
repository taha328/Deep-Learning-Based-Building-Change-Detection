from __future__ import annotations

import argparse
from io import BytesIO
import json
import math
import urllib.request

import numpy as np
from PIL import Image


def _tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _fetch_tile_stats(url: str) -> dict[str, int | str]:
    with urllib.request.urlopen(url, timeout=60) as response:
        content = response.read()
    image = Image.open(BytesIO(content)).convert("RGBA")
    arr = np.asarray(image)
    alpha = arr[:, :, 3]
    rgb = arr[:, :, :3]
    black = np.all(rgb <= 2, axis=2)
    return {
        "url": url,
        "bytes": len(content),
        "transparent_pixels": int((alpha == 0).sum()),
        "black_opaque_pixels": int(((alpha > 0) & black).sum()),
        "opaque_pixels": int((alpha > 0).sum()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit reference tile endpoint alpha and black opaque pixels.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--release", action="append", dest="releases", required=True)
    parser.add_argument("--zoom", type=int, default=18)
    args = parser.parse_args()
    results: dict[str, object] = {}
    for release in args.releases:
        tilejson_url = f"{args.base_url}/api/temporal-projects/{args.project_id}/milestones/{release}/reference/tilejson.json"
        tilejson = _fetch_json(tilejson_url)
        bounds = tilejson.get("bounds") or [-180, -85, 180, 85]
        west, south, east, north = [float(value) for value in bounds]
        probes = {
            "south_center": ((west + east) / 2, south + (north - south) * 0.15),
            "south_west": (west + (east - west) * 0.15, south + (north - south) * 0.15),
            "south_east": (west + (east - west) * 0.85, south + (north - south) * 0.15),
        }
        release_stats = {"tilejson": tilejson_url, "tile_url_versioned": "?v=" in json.dumps(tilejson), "probes": {}}
        template = str(tilejson["tiles"][0])
        for name, (lon, lat) in probes.items():
            x, y = _tile(lon, lat, args.zoom)
            tile_url = template.replace("{z}", str(args.zoom)).replace("{x}", str(x)).replace("{y}", str(y))
            release_stats["probes"][name] = _fetch_tile_stats(tile_url)
        results[release] = release_stats
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
