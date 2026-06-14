from __future__ import annotations

import json
from pathlib import Path
import textwrap


ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "notebooks" / "kaggle_bandon_wayback_mtgcd.ipynb"


def lines(text: str) -> list[str]:
    text = textwrap.dedent(text).strip("\n")
    return [f"{line}\n" for line in text.splitlines()]


def markdown_cell(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": lines(text)}


def code_cell(text: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": lines(text),
    }


cells = [
    markdown_cell(
        """
        # Kaggle MTGCDNet Notebook for Esri Wayback Building Change Detection

        This notebook is designed for **Kaggle GPU + Internet enabled** and uses the **official BANDON / MTGCDNet repository** as the model source:

        - Repo: [fitzpchao/BANDON](https://github.com/fitzpchao/BANDON)
        - Paper: [Detecting Building Changes with Off-Nadir Aerial Images](https://arxiv.org/abs/2301.10922)
        - Dataset / published checkpoint links are taken from the repo README

        ## Honesty / blocker policy

        This notebook is intentionally strict:

        - It does **not** silently replace MTGCDNet with another model.
        - It uses **Esri Wayback** imagery as input, not Google Earth.
        - It tries to run **official BANDON code paths** with only minimal runtime patches needed for Kaggle inference.
        - If the pretrained MTGCDNet checkpoint cannot be downloaded, or if the BANDON stack cannot be imported on the current Kaggle runtime, the notebook raises a clear blocker instead of pretending success.
        - Selected Wayback **release dates** are kept separate from underlying **imagery capture dates**.
        """
    ),
    markdown_cell(
        """
        ## What the notebook does

        1. Accepts a location or explicit AOI bbox.
        2. Queries live Esri Wayback WMTS releases.
        3. Validates the selected T1 / T2 releases against AOI metadata and tile availability.
        4. Downloads T1 and T2 Wayback mosaics as GeoTIFFs.
        5. Attempts local co-registration with AROSICS; if unavailable, falls back to a clearly labeled degraded alignment path.
        6. Adapts the **official BANDON MTGCDNet** inference path to a single arbitrary Wayback image pair.
        7. Exports:
           - T1 / T2 GeoTIFFs
           - aligned T1 GeoTIFF when available
           - change probability GeoTIFF
           - binary change mask GeoTIFF
           - optional GeoJSON polygons
           - preview overlays
        8. Prints diagnostics and a final summary.
        """
    ),
    code_cell(
        """
        from pathlib import Path

        # -----------------------------
        # User configuration
        # -----------------------------
        PLACE_NAME = "Casablanca, Morocco"
        AOI_BBOX = None
        AOI_HALF_SIZE_M = 600.0

        # Explicit release ids such as "WB_2022_R03". If left as None, the
        # notebook will scan releases covering the AOI and auto-pick the oldest
        # and newest covered releases.
        T1_RELEASE = None
        T2_RELEASE = None

        ZOOM = 19
        RUN_ALIGNMENT = True
        ALLOW_DEGRADED_ALIGNMENT = True
        EXPORT_GEOJSON = True
        DISCOVER_COVERING_RELEASES = True

        MAX_TILES_PER_SCENE = 1800
        DOWNLOAD_WORKERS = 12
        METADATA_SCAN_WORKERS = 8

        PATCH_SIZE = 513
        STRIDE = 337
        CHANGE_THRESHOLD = 0.5

        OUTPUT_ROOT = Path("/kaggle/working/wayback_bandon_mtgcd")
        REPO_DIR = OUTPUT_ROOT / "BANDON"
        RUNTIME_DIR = OUTPUT_ROOT / "runtime"
        OUTPUT_DIR = OUTPUT_ROOT / "outputs"
        OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        WMTS_CAPABILITIES_URL = (
            "https://wayback.maptiles.arcgis.com/arcgis/rest/services/"
            "World_Imagery/MapServer/WMTS/1.0.0/WMTSCapabilities.xml"
        )

        MTGCDNET_CHECKPOINT_ID = "17KMvDbVDa8b7mwH7JTZ0iXwurSJsOqFe"
        MTGCDNET_CHECKPOINT_URL = f"https://drive.google.com/uc?id={MTGCDNET_CHECKPOINT_ID}"
        """
    ),
    code_cell(
        """
        import os
        import platform
        import subprocess
        import sys


        def run(cmd: list[str]) -> None:
            print("+", " ".join(cmd))
            subprocess.run(cmd, check=True)


        def try_run(cmd: list[str]) -> bool:
            print("+", " ".join(cmd))
            try:
                subprocess.run(cmd, check=True)
                return True
            except subprocess.CalledProcessError as exc:
                print(f"Command failed: {exc}")
                return False


        if not os.environ.get("KAGGLE_KERNEL_RUN_TYPE"):
            print("Warning: this notebook is intended for Kaggle. It can still be inspected locally.")

        run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-q",
                "gdown",
                "mmcv==1.7.0",
                "lxml",
                "requests",
                "rasterio",
                "pyproj",
                "shapely",
                "matplotlib",
                "pandas",
                "opencv-python-headless",
                "tqdm",
                "Pillow",
            ]
        )

        AROSICS_INSTALL_OK = False
        if RUN_ALIGNMENT:
            AROSICS_INSTALL_OK = try_run([sys.executable, "-m", "pip", "install", "-q", "arosics"])

        if not REPO_DIR.exists():
            run(["git", "clone", "https://github.com/fitzpchao/BANDON", str(REPO_DIR)])

        print("Python:", sys.version)
        print("Platform:", platform.platform())
        """
    ),
    code_cell(
        """
        from pathlib import Path
        import sys


        # Minimal Kaggle inference patch:
        # The official repo imports all decode heads, including optional heads
        # that depend on mmcv.ops. MTGCDNet itself uses only FCN-style heads.
        patched_files = []
        decode_init = REPO_DIR / "mmseg" / "models" / "decode_heads" / "__init__.py"
        decode_init.write_text(
            "\\n".join(
                [
                    "from .fcn_head import FCNHead",
                    "from .shared_fcn_head import SharedFCNHead",
                    "from .forward_head import ForwardHead",
                    "__all__ = ['FCNHead', 'SharedFCNHead', 'ForwardHead']",
                    "",
                ]
            )
        )
        patched_files.append(str(decode_init))

        if str(REPO_DIR) not in sys.path:
            sys.path.insert(0, str(REPO_DIR))

        print("Patched files:")
        for item in patched_files:
            print("-", item)
        """
    ),
    code_cell(
        """
        import concurrent.futures as futures
        from dataclasses import asdict, dataclass
        from datetime import date
        import io
        import json
        import math
        import re
        import warnings

        import matplotlib.pyplot as plt
        import numpy as np
        import pandas as pd
        from PIL import Image
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.features import shapes
        from rasterio.transform import from_bounds
        from rasterio.warp import calculate_default_transform, reproject
        import requests
        from shapely.geometry import LinearRing, MultiPolygon, Polygon, box, mapping, shape
        from shapely.geometry.base import BaseGeometry
        from shapely.ops import transform as shapely_transform, unary_union
        from pyproj import CRS, Geod, Transformer
        from lxml import etree
        from tqdm.auto import tqdm

        GEOD = Geod(ellps="WGS84")
        session = requests.Session()
        session.headers.update({"User-Agent": "Kaggle-BANDON-Wayback/1.0", "Accept": "*/*"})


        def blocker(message: str) -> None:
            raise RuntimeError(f"BLOCKER: {message}")


        @dataclass(frozen=True)
        class WaybackRelease:
            identifier: str
            release_date: date
            label: str
            release_num: int | None
            tile_matrix_sets: tuple[str, ...]
            resource_url_template: str


        @dataclass(frozen=True)
        class MetadataSummary:
            dominant_src_date: str | None
            dominant_src_res_m: float | None
            metadata_region_count: int
            capture_date_count: int
            mixed_capture_dates: bool
            metadata_coverage_fraction: float | None


        @dataclass(frozen=True)
        class TileAvailabilitySummary:
            candidate_count: int
            available_count: int
            missing_count: int
            failed_check_count: int
            preflight_complete: bool
            availability_fraction: float | None
            available_tiles: frozenset[tuple[int, int]]

            def to_dict(self) -> dict[str, object]:
                return {
                    "candidate_count": self.candidate_count,
                    "available_count": self.available_count,
                    "missing_count": self.missing_count,
                    "failed_check_count": self.failed_check_count,
                    "preflight_complete": self.preflight_complete,
                    "availability_fraction": self.availability_fraction,
                }


        def get_text(url: str, *, params: dict | None = None, timeout: int = 120) -> str:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.text


        def get_json(url: str, *, params: dict | None = None, timeout: int = 120) -> dict:
            response = session.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            return response.json()


        def parse_wmts_capabilities(url: str = WMTS_CAPABILITIES_URL) -> list[WaybackRelease]:
            xml = get_text(url)
            ns = {
                "wmts": "https://www.opengis.net/wmts/1.0",
                "ows": "https://www.opengis.net/ows/1.1",
            }
            root = etree.fromstring(xml.encode("utf-8"))
            releases: list[WaybackRelease] = []
            for layer in root.xpath(".//wmts:Layer", namespaces=ns):
                title = layer.xpath("./ows:Title/text()", namespaces=ns)
                identifier = layer.xpath("./ows:Identifier/text()", namespaces=ns)
                resource_urls = layer.xpath("./wmts:ResourceURL", namespaces=ns)
                tile_matrix_sets = layer.xpath("./wmts:TileMatrixSetLink/wmts:TileMatrixSet/text()", namespaces=ns)
                if not title or not identifier or not resource_urls:
                    continue
                label = title[0]
                resource_url_template = resource_urls[0].attrib.get("template")
                if "Wayback" not in label or not resource_url_template:
                    continue
                release_date_match = re.search(r"(\\d{4}-\\d{2}-\\d{2})", label)
                release_num_match = re.search(r"/tile/(\\d+)/\\{TileMatrix\\}/", resource_url_template)
                if not release_date_match:
                    continue
                releases.append(
                    WaybackRelease(
                        identifier=identifier[0],
                        release_date=pd.to_datetime(release_date_match.group(1)).date(),
                        label=f"{identifier[0]} ({release_date_match.group(1)})",
                        release_num=int(release_num_match.group(1)) if release_num_match else None,
                        tile_matrix_sets=tuple(tile_matrix_sets),
                        resource_url_template=resource_url_template,
                    )
                )
            releases.sort(key=lambda item: item.release_date)
            return releases


        def metadata_base_url_from_identifier(identifier: str) -> str:
            match = re.fullmatch(r"WB_(\\d{4})_R(\\d{2})", identifier)
            if not match:
                raise ValueError(f"Unexpected Wayback identifier format: {identifier}")
            year, release_index = match.groups()
            service_name = f"World_Imagery_Metadata_{year}_r{release_index.lower()}"
            return f"https://metadata.maptiles.arcgis.com/arcgis/rest/services/{service_name}/MapServer"


        def geocode_place(place_name: str) -> tuple[float, float]:
            response = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": place_name, "format": "jsonv2", "limit": 1},
                headers={"User-Agent": "Kaggle-BANDON-Wayback/1.0"},
                timeout=60,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload:
                blocker(f"Could not geocode place name: {place_name}")
            return float(payload[0]["lon"]), float(payload[0]["lat"])


        def square_bbox_from_center(lon: float, lat: float, half_size_m: float) -> dict[str, float]:
            to_3857 = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
            to_4326 = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
            x, y = to_3857.transform(lon, lat)
            west, south = to_4326.transform(x - half_size_m, y - half_size_m)
            east, north = to_4326.transform(x + half_size_m, y + half_size_m)
            return {"west": west, "south": south, "east": east, "north": north}


        def resolve_bbox() -> dict[str, float]:
            if AOI_BBOX is not None:
                west, south, east, north = AOI_BBOX
                return {"west": float(west), "south": float(south), "east": float(east), "north": float(north)}
            if not PLACE_NAME:
                blocker("Set either PLACE_NAME or AOI_BBOX.")
            lon, lat = geocode_place(PLACE_NAME)
            return square_bbox_from_center(lon, lat, AOI_HALF_SIZE_M)


        def bbox_to_geojson_polygon(bbox: dict[str, float]) -> dict:
            return {
                "type": "Polygon",
                "coordinates": [[
                    [bbox["west"], bbox["south"]],
                    [bbox["east"], bbox["south"]],
                    [bbox["east"], bbox["north"]],
                    [bbox["west"], bbox["north"]],
                    [bbox["west"], bbox["south"]],
                ]],
            }


        def parse_aoi_geometry(aoi_geojson: dict[str, object]) -> BaseGeometry:
            geom = shape(aoi_geojson)
            if geom.is_empty or not geom.is_valid:
                blocker("AOI geometry is empty or invalid.")
            return geom


        def reproject_geometry(geometry: BaseGeometry, src_crs: str, dst_crs: str) -> BaseGeometry:
            transformer = Transformer.from_crs(src_crs, dst_crs, always_xy=True)
            return shapely_transform(lambda x, y, z=None: transformer.transform(x, y), geometry)


        def _metadata_layer_lookup(metadata_base_url: str) -> dict[int, str]:
            service = get_json(f"{metadata_base_url}?f=pjson")
            return {int(layer["id"]): str(layer["name"]) for layer in service.get("layers", []) if "id" in layer and "name" in layer}


        def _metadata_layer_candidates(metadata_base_url: str, *, zoom: int) -> list[int]:
            layer_lookup = _metadata_layer_lookup(metadata_base_url)
            if not layer_lookup:
                return []
            preferred_layer_id = max(0, min(13, 23 - zoom))
            candidates: list[int] = []
            if preferred_layer_id in layer_lookup:
                candidates.append(preferred_layer_id)
            for layer_id in sorted(layer_lookup):
                if layer_id not in candidates:
                    candidates.append(layer_id)
            return candidates


        def _geometry_to_esri_polygon(geometry: BaseGeometry) -> dict[str, object]:
            polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else [geometry]
            rings: list[list[list[float]]] = []
            for polygon in polygons:
                assert isinstance(polygon, Polygon)
                rings.append([[float(x), float(y)] for x, y in polygon.exterior.coords])
                for interior in polygon.interiors:
                    rings.append([[float(x), float(y)] for x, y in interior.coords])
            return {"rings": rings, "spatialReference": {"wkid": 3857}}


        def _esri_rings_to_geometry(rings: list[list[list[float]]]) -> BaseGeometry | None:
            outer_rings: list[LinearRing] = []
            hole_rings: list[LinearRing] = []
            for ring_coords in rings:
                if len(ring_coords) < 4:
                    continue
                ring = LinearRing(ring_coords)
                if ring.is_ccw:
                    hole_rings.append(ring)
                else:
                    outer_rings.append(ring)
            polygons: list[Polygon] = []
            remaining_holes = list(hole_rings)
            for outer in outer_rings:
                outer_polygon = Polygon(outer)
                assigned_holes: list[list[tuple[float, float]]] = []
                next_remaining: list[LinearRing] = []
                for hole in remaining_holes:
                    hole_polygon = Polygon(hole)
                    if outer_polygon.contains(hole_polygon.representative_point()):
                        assigned_holes.append(list(hole.coords))
                    else:
                        next_remaining.append(hole)
                remaining_holes = next_remaining
                polygons.append(Polygon(list(outer.coords), assigned_holes))
            if not polygons:
                return None
            unioned = unary_union(polygons)
            return unioned if not unioned.is_empty else None


        def _src_date_to_iso(value: object) -> str | None:
            if value in (None, ""):
                return None
            try:
                if isinstance(value, (int, float)) and int(value) > 10_000_000_000:
                    return str(pd.to_datetime(int(value), unit="ms").date())
                return str(pd.to_datetime(str(int(value)), format="%Y%m%d").date())
            except Exception:
                return None


        def query_metadata_polygon(metadata_base_url: str, aoi_geojson: dict[str, object], *, zoom: int) -> list[dict[str, object]]:
            layer_lookup = _metadata_layer_lookup(metadata_base_url)
            layer_ids = _metadata_layer_candidates(metadata_base_url, zoom=zoom)
            geometry_3857 = reproject_geometry(parse_aoi_geometry(aoi_geojson), "EPSG:4326", "EPSG:3857")
            params = {
                "geometry": json.dumps(_geometry_to_esri_polygon(geometry_3857)),
                "geometryType": "esriGeometryPolygon",
                "inSR": 3857,
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": ",".join(["SRC_DATE", "SRC_DATE2", "SRC_RES", "SRC_ACC", "SAMP_RES", "NICE_NAME", "NICE_DESC", "ReleaseName"]),
                "returnGeometry": "true",
                "f": "pjson",
            }
            for layer_id in layer_ids:
                payload = get_json(f"{metadata_base_url}/{layer_id}/query", params=params)
                features = payload.get("features", [])
                if not features:
                    continue
                return [
                    {
                        **dict(feature.get("attributes", {})),
                        "_geometry": feature.get("geometry"),
                        "metadata_layer_id": layer_id,
                        "metadata_layer_name": layer_lookup.get(layer_id),
                    }
                    for feature in features
                ]
            return []


        def summarize_wayback_metadata(release_identifier: str, bbox: dict[str, float], *, zoom: int) -> MetadataSummary:
            metadata_base_url = metadata_base_url_from_identifier(release_identifier)
            polygon_features = query_metadata_polygon(metadata_base_url, bbox_to_geojson_polygon(bbox), zoom=zoom)
            capture_dates = sorted(
                {
                    date_str
                    for feature in polygon_features
                    for date_str in [_src_date_to_iso(feature.get("SRC_DATE2")), _src_date_to_iso(feature.get("SRC_DATE"))]
                    if date_str is not None
                }
            )
            dominant_src_date = capture_dates[0] if capture_dates else None
            src_res_values = [float(feature["SRC_RES"]) for feature in polygon_features if feature.get("SRC_RES") not in (None, "")]
            dominant_src_res_m = float(np.median(src_res_values)) if src_res_values else None
            coverage_fraction = None
            if polygon_features:
                try:
                    aoi_3857 = reproject_geometry(parse_aoi_geometry(bbox_to_geojson_polygon(bbox)), "EPSG:4326", "EPSG:3857")
                    region_geometries = []
                    for feature in polygon_features:
                        geometry_payload = feature.get("_geometry")
                        if isinstance(geometry_payload, dict):
                            rings = geometry_payload.get("rings")
                            if isinstance(rings, list):
                                geometry = _esri_rings_to_geometry(rings)
                                if geometry is not None and not geometry.is_empty:
                                    region_geometries.append(geometry)
                    if region_geometries and aoi_3857.area > 0:
                        unioned = unary_union(region_geometries)
                        coverage_fraction = min(1.0, float(unioned.intersection(aoi_3857).area / aoi_3857.area))
                except Exception:
                    coverage_fraction = None
            return MetadataSummary(
                dominant_src_date=dominant_src_date,
                dominant_src_res_m=dominant_src_res_m,
                metadata_region_count=len(polygon_features),
                capture_date_count=len(capture_dates),
                mixed_capture_dates=len(capture_dates) > 1,
                metadata_coverage_fraction=coverage_fraction,
            )


        def lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[int, int]:
            lat_rad = math.radians(lat)
            n = 2 ** zoom
            x = int((lon + 180.0) / 360.0 * n)
            y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
            return x, y


        def tile_range_for_bbox(bbox: dict[str, float], zoom: int) -> tuple[int, int, int, int]:
            x_min, y_max = lonlat_to_tile(bbox["west"], bbox["south"], zoom)
            x_max, y_min = lonlat_to_tile(bbox["east"], bbox["north"], zoom)
            return min(x_min, x_max), max(x_min, x_max), min(y_min, y_max), max(y_min, y_max)


        def tile_bounds_3857(x: int, y: int, zoom: int) -> tuple[float, float, float, float]:
            n = 2 ** zoom
            tile_size = 2 * math.pi * 6378137 / n
            left = -math.pi * 6378137 + x * tile_size
            top = math.pi * 6378137 - y * tile_size
            right = left + tile_size
            bottom = top - tile_size
            return left, bottom, right, top


        def build_tile_url(template: str, tile_matrix_set: str, zoom: int, x: int, y: int) -> str:
            return (
                template.replace("{TileMatrixSet}", tile_matrix_set)
                .replace("{TileMatrix}", str(zoom))
                .replace("{TileRow}", str(y))
                .replace("{TileCol}", str(x))
            )


        def choose_tile_matrix_set(release: WaybackRelease) -> str:
            candidates = [item for item in release.tile_matrix_sets if item]
            if not candidates:
                blocker(f"No TileMatrixSet is advertised for {release.identifier}.")

            exact_preference = [
                "GoogleMapsCompatible_Level23",
                "GoogleMapsCompatible",
                "default028mm",
            ]
            for preferred in exact_preference:
                if preferred in candidates:
                    return preferred

            for candidate in candidates:
                if "googlemapscompatible" in candidate.lower():
                    return candidate

            # Fall back to the first advertised set rather than hard-failing on
            # releases that do not expose the Level23 alias.
            return candidates[0]


        def preflight_wayback_tile_availability(release: WaybackRelease, bbox: dict[str, float], *, zoom: int) -> TileAvailabilitySummary:
            if release.release_num is None:
                return TileAvailabilitySummary(0, 0, 0, 0, False, None, frozenset())
            x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
            candidate_tiles = [(x, y) for y in range(y_min, y_max + 1) for x in range(x_min, x_max + 1)]
            tilemap_base = release.resource_url_template.split("/tile/")[0] + "/tilemap"
            available_tiles: set[tuple[int, int]] = set()
            failed = 0

            def _check(tile: tuple[int, int]) -> tuple[tuple[int, int], bool]:
                x, y = tile
                url = f"{tilemap_base}/{release.release_num}/{zoom}/{y}/{x}"
                response = requests.get(url, headers=session.headers, timeout=120)
                response.raise_for_status()
                payload = response.json()
                return tile, bool(payload.get("data") and payload["data"][0] == 1)

            with futures.ThreadPoolExecutor(max_workers=min(METADATA_SCAN_WORKERS, max(1, len(candidate_tiles)))) as executor:
                future_map = {executor.submit(_check, tile): tile for tile in candidate_tiles}
                for future in tqdm(futures.as_completed(future_map), total=len(future_map), desc=f"Preflight {release.identifier}"):
                    try:
                        tile, ok = future.result()
                    except requests.RequestException:
                        failed += 1
                        continue
                    if ok:
                        available_tiles.add(tile)

            candidate_count = len(candidate_tiles)
            available_count = len(available_tiles)
            missing_count = max(candidate_count - available_count - failed, 0)
            availability_fraction = available_count / candidate_count if candidate_count else 0.0
            return TileAvailabilitySummary(
                candidate_count=candidate_count,
                available_count=available_count,
                missing_count=missing_count,
                failed_check_count=failed,
                preflight_complete=failed == 0,
                availability_fraction=availability_fraction,
                available_tiles=frozenset(available_tiles),
            )


        def discover_covering_releases(releases: list[WaybackRelease], bbox: dict[str, float], *, zoom: int) -> pd.DataFrame:
            rows: list[dict[str, object]] = []
            aoi = bbox_to_geojson_polygon(bbox)

            def _scan(release: WaybackRelease) -> dict[str, object] | None:
                try:
                    summary = summarize_wayback_metadata(release.identifier, bbox, zoom=zoom)
                except requests.RequestException:
                    return None
                if summary.metadata_region_count <= 0:
                    return None
                return {
                    "identifier": release.identifier,
                    "release_date": str(release.release_date),
                    "capture_date_count": summary.capture_date_count,
                    "mixed_capture_dates": summary.mixed_capture_dates,
                    "dominant_src_date": summary.dominant_src_date,
                    "dominant_src_res_m": summary.dominant_src_res_m,
                    "metadata_coverage_fraction": summary.metadata_coverage_fraction,
                }

            with futures.ThreadPoolExecutor(max_workers=METADATA_SCAN_WORKERS) as executor:
                future_map = {executor.submit(_scan, release): release for release in releases}
                for future in tqdm(futures.as_completed(future_map), total=len(future_map), desc="Scan AOI coverage"):
                    row = future.result()
                    if row is not None:
                        rows.append(row)
            df = pd.DataFrame(rows).sort_values("release_date").reset_index(drop=True)
            return df
        """
    ),
    code_cell(
        """
        @dataclass(frozen=True)
        class MosaicResult:
            identifier: str
            release_date: str
            tile_matrix_set: str
            tile_count: int
            available_tile_count: int
            missing_tile_count: int
            bounds_3857: tuple[float, float, float, float]
            geotiff_path: Path
            png_path: Path
            valid_mask_path: Path


        def download_wayback_mosaic(release: WaybackRelease, bbox: dict[str, float], *, label: str, available_tiles: frozenset[tuple[int, int]] | None = None) -> MosaicResult:
            tile_matrix_set = choose_tile_matrix_set(release)
            print(
                f"Using TileMatrixSet {tile_matrix_set!r} for {release.identifier} "
                f"(advertised: {', '.join(release.tile_matrix_sets)})"
            )

            x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, ZOOM)
            tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
            if tile_count > MAX_TILES_PER_SCENE:
                blocker(
                    f"AOI requires {tile_count} tiles for {release.identifier} at z={ZOOM}, "
                    f"exceeding MAX_TILES_PER_SCENE={MAX_TILES_PER_SCENE}. Increase the budget or reduce the AOI."
                )

            width = (x_max - x_min + 1) * 256
            height = (y_max - y_min + 1) * 256
            canvas = Image.new("RGB", (width, height))
            valid_mask = np.zeros((height, width), dtype=np.uint8)

            jobs = []
            skipped = 0
            for y in range(y_min, y_max + 1):
                for x in range(x_min, x_max + 1):
                    if available_tiles is not None and (x, y) not in available_tiles:
                        skipped += 1
                        continue
                    jobs.append((x, y, build_tile_url(release.resource_url_template, tile_matrix_set, ZOOM, x, y)))

            def _download_tile(url: str) -> bytes | None:
                response = requests.get(url, timeout=120)
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                return response.content

            available_tile_count = 0
            missing_tile_count = skipped
            with futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as executor:
                future_map = {executor.submit(_download_tile, url): (x, y) for x, y, url in jobs}
                for future in tqdm(futures.as_completed(future_map), total=len(future_map), desc=f"Download {release.identifier}"):
                    x, y = future_map[future]
                    tile_bytes = future.result()
                    if tile_bytes is None:
                        missing_tile_count += 1
                        continue
                    available_tile_count += 1
                    tile = Image.open(io.BytesIO(tile_bytes)).convert("RGB")
                    canvas.paste(tile, ((x - x_min) * 256, (y - y_min) * 256))
                    valid_mask[(y - y_min) * 256 : (y - y_min + 1) * 256, (x - x_min) * 256 : (x - x_min + 1) * 256] = 1

            if available_tile_count == 0:
                blocker(f"Selected Wayback release {release.identifier} has no available imagery tiles for the requested AOI at z={ZOOM}.")

            left, _, _, top = tile_bounds_3857(x_min, y_min, ZOOM)
            _, bottom, right, _ = tile_bounds_3857(x_max, y_max, ZOOM)
            bounds_3857 = (left, bottom, right, top)
            transform = from_bounds(*bounds_3857, width=width, height=height)

            png_path = OUTPUT_DIR / f"{label}_{release.identifier}_z{ZOOM}.png"
            geotiff_path = OUTPUT_DIR / f"{label}_{release.identifier}_z{ZOOM}.tif"
            valid_mask_path = OUTPUT_DIR / f"{label}_{release.identifier}_z{ZOOM}_valid_mask.tif"
            canvas.save(png_path)

            arr = np.asarray(canvas)
            with rasterio.open(
                geotiff_path,
                "w",
                driver="GTiff",
                width=width,
                height=height,
                count=3,
                dtype=arr.dtype,
                crs="EPSG:3857",
                transform=transform,
            ) as dst:
                for band_index in range(3):
                    dst.write(arr[:, :, band_index], band_index + 1)
            with rasterio.open(
                valid_mask_path,
                "w",
                driver="GTiff",
                width=width,
                height=height,
                count=1,
                dtype=valid_mask.dtype,
                crs="EPSG:3857",
                transform=transform,
            ) as dst:
                dst.write(valid_mask, 1)

            return MosaicResult(
                identifier=release.identifier,
                release_date=str(release.release_date),
                tile_matrix_set=tile_matrix_set,
                tile_count=tile_count,
                available_tile_count=available_tile_count,
                missing_tile_count=missing_tile_count,
                bounds_3857=bounds_3857,
                geotiff_path=geotiff_path,
                png_path=png_path,
                valid_mask_path=valid_mask_path,
            )


        def read_rgb(path: Path) -> np.ndarray:
            with rasterio.open(path) as src:
                arr = src.read([1, 2, 3])
            return np.transpose(arr, (1, 2, 0))


        def write_single_band(path: Path, array: np.ndarray, reference_path: Path, *, dtype: str) -> Path:
            with rasterio.open(reference_path) as ref:
                profile = ref.profile.copy()
                profile.update(count=1, dtype=dtype, nodata=None)
                with rasterio.open(path, "w", **profile) as dst:
                    dst.write(array.astype(dtype), 1)
            return path


        def align_t1_to_t2(t1_mosaic: MosaicResult, t2_mosaic: MosaicResult) -> tuple[Path, dict[str, object]]:
            diagnostics = {"alignment_mode": "identity", "alignment_degraded": False}
            if not RUN_ALIGNMENT:
                return t1_mosaic.geotiff_path, diagnostics

            corrected_path = OUTPUT_DIR / "t1_coregistered_to_t2.tif"
            bad_ref_path = OUTPUT_DIR / "t2_bad_data_mask.tif"
            bad_tgt_path = OUTPUT_DIR / "t1_bad_data_mask.tif"

            with rasterio.open(t2_mosaic.valid_mask_path) as src:
                valid_ref = src.read(1)
            with rasterio.open(t1_mosaic.valid_mask_path) as src:
                valid_tgt = src.read(1)
            write_single_band(bad_ref_path, (valid_ref == 0).astype(np.uint8), t2_mosaic.valid_mask_path, dtype="uint8")
            write_single_band(bad_tgt_path, (valid_tgt == 0).astype(np.uint8), t1_mosaic.valid_mask_path, dtype="uint8")

            try:
                from arosics import COREG_LOCAL

                coreg = COREG_LOCAL(
                    str(t2_mosaic.geotiff_path),
                    str(t1_mosaic.geotiff_path),
                    path_out=str(corrected_path),
                    grid_res=120,
                    window_size=(256, 256),
                    max_shift=48,
                    tieP_filter_level=3,
                    min_reliability=60,
                    resamp_alg_calc="cubic",
                    resamp_alg_deshift="cubic",
                    align_grids=True,
                    match_gsd=False,
                    mask_baddata_ref=str(bad_ref_path),
                    mask_baddata_tgt=str(bad_tgt_path),
                    nodata=(0, 0),
                    CPUs=2,
                )
                coreg.correct_shifts()
                diagnostics = {
                    "alignment_mode": "AROSICS_LOCAL",
                    "alignment_degraded": False,
                    "tie_points_available": True,
                }
                return corrected_path, diagnostics
            except Exception as exc:
                warnings.warn(f"AROSICS alignment failed, falling back to raster reprojection: {exc}")
                if not ALLOW_DEGRADED_ALIGNMENT:
                    blocker(f"AROSICS alignment failed and ALLOW_DEGRADED_ALIGNMENT is False: {exc}")

            with rasterio.open(t1_mosaic.geotiff_path) as src, rasterio.open(t2_mosaic.geotiff_path) as ref:
                profile = ref.profile.copy()
                profile.update(count=3, dtype=src.dtypes[0], nodata=None)
                with rasterio.open(corrected_path, "w", **profile) as dst:
                    for band in range(1, 4):
                        reproject(
                            source=rasterio.band(src, band),
                            destination=rasterio.band(dst, band),
                            src_transform=src.transform,
                            src_crs=src.crs,
                            dst_transform=ref.transform,
                            dst_crs=ref.crs,
                            resampling=Resampling.bilinear,
                        )
            diagnostics = {
                "alignment_mode": "reproject_only",
                "alignment_degraded": True,
            }
            return corrected_path, diagnostics
        """
    ),
    code_cell(
        """
        releases = parse_wmts_capabilities()
        bbox = resolve_bbox()
        aoi_geojson = bbox_to_geojson_polygon(bbox)
        release_lookup = {release.identifier: release for release in releases}

        covered_releases_df = pd.DataFrame()
        if DISCOVER_COVERING_RELEASES and (T1_RELEASE is None or T2_RELEASE is None):
            covered_releases_df = discover_covering_releases(releases, bbox, zoom=ZOOM)
            if covered_releases_df.empty:
                blocker("No Wayback releases with metadata coverage were found for the requested AOI.")
            display(covered_releases_df.tail(20))
            if T1_RELEASE is None:
                T1_RELEASE = str(covered_releases_df.iloc[0]["identifier"])
            if T2_RELEASE is None:
                T2_RELEASE = str(covered_releases_df.iloc[-1]["identifier"])

        if T1_RELEASE is None or T2_RELEASE is None:
            blocker("Set T1_RELEASE and T2_RELEASE explicitly, or leave them None and enable DISCOVER_COVERING_RELEASES.")
        if T1_RELEASE == T2_RELEASE:
            blocker("T1_RELEASE and T2_RELEASE must be different.")
        if T1_RELEASE not in release_lookup or T2_RELEASE not in release_lookup:
            blocker("One or both selected releases are not present in the live Wayback WMTS capabilities.")

        release_t1 = release_lookup[T1_RELEASE]
        release_t2 = release_lookup[T2_RELEASE]
        if release_t1.release_date >= release_t2.release_date:
            blocker(f"T1 must be older than T2. Got {release_t1.release_date} and {release_t2.release_date}.")

        metadata_t1 = summarize_wayback_metadata(release_t1.identifier, bbox, zoom=ZOOM)
        metadata_t2 = summarize_wayback_metadata(release_t2.identifier, bbox, zoom=ZOOM)
        preflight_t1 = preflight_wayback_tile_availability(release_t1, bbox, zoom=ZOOM)
        preflight_t2 = preflight_wayback_tile_availability(release_t2, bbox, zoom=ZOOM)

        if preflight_t1.available_count == 0:
            blocker(f"T1 release {release_t1.identifier} has zero available imagery tiles at z={ZOOM}.")
        if preflight_t2.available_count == 0:
            blocker(f"T2 release {release_t2.identifier} has zero available imagery tiles at z={ZOOM}.")

        diagnostics = {
            "aoi_bbox": bbox,
            "selected_release_t1": {"identifier": release_t1.identifier, "release_date": str(release_t1.release_date)},
            "selected_release_t2": {"identifier": release_t2.identifier, "release_date": str(release_t2.release_date)},
            "metadata_t1": asdict(metadata_t1),
            "metadata_t2": asdict(metadata_t2),
            "tilemap_t1": preflight_t1.to_dict(),
            "tilemap_t2": preflight_t2.to_dict(),
        }
        print(json.dumps(diagnostics, indent=2))
        """
    ),
    code_cell(
        """
        mosaic_t1 = download_wayback_mosaic(
            release_t1,
            bbox,
            label="t1",
            available_tiles=preflight_t1.available_tiles if preflight_t1.preflight_complete else None,
        )
        mosaic_t2 = download_wayback_mosaic(
            release_t2,
            bbox,
            label="t2",
            available_tiles=preflight_t2.available_tiles if preflight_t2.preflight_complete else None,
        )
        aligned_t1_path, alignment_diag = align_t1_to_t2(mosaic_t1, mosaic_t2)

        t1_rgb = read_rgb(aligned_t1_path)
        t2_rgb = read_rgb(mosaic_t2.geotiff_path)

        t1_png_for_model = OUTPUT_DIR / "mtgcd_t1.png"
        t2_png_for_model = OUTPUT_DIR / "mtgcd_t2.png"
        Image.fromarray(t1_rgb.astype(np.uint8)).save(t1_png_for_model)
        Image.fromarray(t2_rgb.astype(np.uint8)).save(t2_png_for_model)

        diagnostics["alignment"] = alignment_diag
        diagnostics["download"] = {
            "t1_geotiff": str(mosaic_t1.geotiff_path),
            "t2_geotiff": str(mosaic_t2.geotiff_path),
            "aligned_t1_geotiff": str(aligned_t1_path),
            "t1_available_tiles": mosaic_t1.available_tile_count,
            "t2_available_tiles": mosaic_t2.available_tile_count,
        }
        print(json.dumps(diagnostics["download"], indent=2))
        """
    ),
    code_cell(
        """
        import gdown
        import torch
        import mmcv
        from mmcv.parallel import MMDataParallel
        from mmcv.runner import load_checkpoint
        from mmseg.datasets import build_dataloader, build_dataset
        from mmseg.models import build_segmentor


        if not torch.cuda.is_available():
            blocker("Kaggle GPU is not available. This notebook is intended for GPU-enabled Kaggle sessions.")

        checkpoint_path = OUTPUT_DIR / "mtgcdnet_iter_40000.pth"
        if not checkpoint_path.exists():
            ok = gdown.download(MTGCDNET_CHECKPOINT_URL, str(checkpoint_path), quiet=False, fuzzy=True)
            if not ok or not checkpoint_path.exists():
                blocker(
                    "The published MTGCDNet checkpoint could not be downloaded from the public Google Drive link in the BANDON README. "
                    "The notebook cannot truthfully run official MTGCDNet inference without that checkpoint."
                )

        list_file = RUNTIME_DIR / "wayback_pair.txt"
        list_file.write_text(f"{t1_png_for_model} {t2_png_for_model}\\n")

        test_pipeline = [
            dict(type="LoadImageFromFile", file_client_args=dict(backend="disk")),
            dict(
                type="MultiScaleAug_RS",
                img_ratios=[1.0],
                transforms=[
                    dict(type="Resize", keep_ratio=True),
                    dict(type="Normalize", mean=[0, 0, 0], std=[255, 255, 255], to_rgb=True),
                    dict(type="ImageToTensor", keys=["img"]),
                    dict(type="Collect", keys=["img"]),
                ],
            ),
        ]

        dataset_cfg = dict(
            type="TxtMIMODatasetForBANDON",
            txt_fn=str(list_file),
            pipeline=test_pipeline,
            data_root=None,
            has_mask=False,
            test_mode=True,
            classes=["unchange", "change"],
            palette=[[0, 0, 0], [255, 255, 255]],
        )

        cfg = mmcv.Config.fromfile(str(REPO_DIR / "workdirs_bandon" / "MTGCDNet" / "config.py"))
        cfg.model.pretrained = None
        cfg.model.backbone.pretrained = None
        cfg.model.backbone.norm_cfg = dict(type="BN", requires_grad=True)
        for head in cfg.model.decode_head:
            if isinstance(head, dict):
                head["norm_cfg"] = dict(type="BN", requires_grad=True)
        cfg.test_cfg.mode = "slide"
        cfg.test_cfg.crop_size = (PATCH_SIZE, PATCH_SIZE)
        cfg.test_cfg.stride = (STRIDE, STRIDE)

        dataset = build_dataset(dataset_cfg)
        data_loader = build_dataloader(dataset, samples_per_gpu=1, workers_per_gpu=1, dist=False, shuffle=False)

        model = build_segmentor(cfg.model, train_cfg=None, test_cfg=cfg.test_cfg)
        checkpoint = load_checkpoint(model, str(checkpoint_path), map_location="cpu")
        model.CLASSES = checkpoint["meta"].get("CLASSES", ["unchange", "change"])
        model.PALETTE = checkpoint["meta"].get("PALETTE", [[0, 0, 0], [255, 255, 255]])
        model = MMDataParallel(model.cuda(), device_ids=[0])
        model.eval()

        batch = next(iter(data_loader))
        with torch.no_grad():
            raw_result = model(return_loss=False, rescale=True, **batch)[0]

        if not isinstance(raw_result, (list, tuple)) or len(raw_result) == 0:
            blocker("Official MTGCDNet inference returned an unexpected output structure.")

        pred_logits = raw_result[0]
        if pred_logits.shape[0] != 2:
            blocker(f"Expected a 2-class change prediction, got shape {pred_logits.shape}.")

        change_probability = pred_logits[1].astype(np.float32)
        change_mask = np.argmax(pred_logits, axis=0).astype(np.uint8)
        binary_mask = (change_probability >= CHANGE_THRESHOLD).astype(np.uint8)

        diagnostics["model"] = {
            "official_repo": str(REPO_DIR),
            "checkpoint_path": str(checkpoint_path),
            "checkpoint_loaded": True,
            "patched_files": patched_files,
            "gpu_name": torch.cuda.get_device_name(0),
            "torch_version": torch.__version__,
            "mmcv_version": mmcv.__version__,
        }
        print(json.dumps(diagnostics["model"], indent=2))
        """
    ),
    code_cell(
        """
        def save_georeferenced_outputs(
            reference_tif: Path,
            probability: np.ndarray,
            binary: np.ndarray,
            *,
            export_geojson: bool,
        ) -> dict[str, str]:
            with rasterio.open(reference_tif) as ref:
                profile = ref.profile.copy()
                transform = ref.transform
                crs = ref.crs

            probability_tif = OUTPUT_DIR / "change_probability.tif"
            binary_tif = OUTPUT_DIR / "change_mask.tif"

            with rasterio.open(probability_tif, "w", driver="GTiff", width=probability.shape[1], height=probability.shape[0], count=1, dtype="float32", crs=crs, transform=transform) as dst:
                dst.write(probability.astype(np.float32), 1)
            with rasterio.open(binary_tif, "w", driver="GTiff", width=binary.shape[1], height=binary.shape[0], count=1, dtype="uint8", crs=crs, transform=transform) as dst:
                dst.write(binary.astype(np.uint8), 1)

            probability_png = OUTPUT_DIR / "change_probability.png"
            mask_png = OUTPUT_DIR / "change_mask_overlay.png"

            prob_vis = np.clip(probability * 255.0, 0, 255).astype(np.uint8)
            Image.fromarray(prob_vis).save(probability_png)

            base = read_rgb(reference_tif).astype(np.uint8)
            overlay = base.copy()
            overlay[binary == 1] = np.array([255, 0, 0], dtype=np.uint8)
            blended = (0.65 * base + 0.35 * overlay).astype(np.uint8)
            Image.fromarray(blended).save(mask_png)

            result = {
                "change_probability_tif": str(probability_tif),
                "change_mask_tif": str(binary_tif),
                "change_probability_png": str(probability_png),
                "change_mask_overlay_png": str(mask_png),
            }

            if export_geojson:
                features = []
                for idx, (geom, value) in enumerate(shapes(binary.astype(np.uint8), mask=binary.astype(bool), transform=transform), start=1):
                    if int(value) != 1:
                        continue
                    geom_native = shape(geom)
                    geom_wgs84 = reproject_geometry(geom_native, str(crs), "EPSG:4326")
                    area_m2 = abs(GEOD.geometry_area_perimeter(geom_wgs84)[0])
                    if area_m2 <= 0:
                        continue
                    features.append(
                        {
                            "type": "Feature",
                            "geometry": mapping(geom_wgs84),
                            "properties": {
                                "feature_id": idx,
                                "area_m2": float(area_m2),
                                "selected_release_t1": release_t1.identifier,
                                "selected_release_t2": release_t2.identifier,
                                "selected_release_date_t1": str(release_t1.release_date),
                                "selected_release_date_t2": str(release_t2.release_date),
                                "dominant_capture_date_t1": metadata_t1.dominant_src_date,
                                "dominant_capture_date_t2": metadata_t2.dominant_src_date,
                            },
                        }
                    )
                geojson_path = OUTPUT_DIR / "change_buildings.geojson"
                geojson_path.write_text(json.dumps({"type": "FeatureCollection", "features": features}, indent=2))
                result["change_buildings_geojson"] = str(geojson_path)
            return result


        output_artifacts = save_georeferenced_outputs(
            mosaic_t2.geotiff_path,
            change_probability,
            binary_mask,
            export_geojson=EXPORT_GEOJSON,
        )
        diagnostics["artifacts"] = output_artifacts
        diagnostics_path = OUTPUT_DIR / "run_diagnostics.json"
        diagnostics_path.write_text(json.dumps(diagnostics, indent=2))
        print(json.dumps(output_artifacts, indent=2))
        """
    ),
    code_cell(
        """
        fig, axes = plt.subplots(1, 3, figsize=(18, 6))
        axes[0].imshow(t1_rgb)
        axes[0].set_title(f"T1 RGB | {release_t1.identifier} ({release_t1.release_date})")
        axes[1].imshow(t2_rgb)
        axes[1].set_title(f"T2 RGB | {release_t2.identifier} ({release_t2.release_date})")
        axes[2].imshow(t2_rgb)
        axes[2].imshow(binary_mask, cmap="Reds", alpha=0.45)
        axes[2].set_title("MTGCDNet change mask overlay")
        for ax in axes:
            ax.axis("off")
        plt.tight_layout()
        plt.show()

        print("Diagnostics file:", diagnostics_path)
        display(pd.DataFrame([diagnostics["selected_release_t1"], diagnostics["selected_release_t2"]]))
        """
    ),
    markdown_cell(
        """
        ## Final reporting checklist

        After running all cells, verify the following:

        - The notebook cloned the official BANDON repo.
        - The notebook downloaded the published MTGCDNet checkpoint from the README link.
        - The notebook used Esri Wayback imagery chosen by location / AOI and release ids.
        - The notebook kept **selected release dates** separate from **dominant capture dates**.
        - The notebook reported whether AROSICS local co-registration was used or whether a degraded fallback was used.
        - The notebook exported georeferenced outputs.

        ## Important remaining limitation

        Even when the notebook runs successfully, the result is still **domain-shifted**:

        - MTGCDNet was published for the BANDON off-nadir aerial-image domain.
        - This notebook feeds it **Esri Wayback satellite imagery**.

        That means successful execution does **not** imply scientifically validated accuracy on Wayback. It means the official model path ran on the user-selected pair and produced outputs that can be inspected and benchmarked honestly.
        """
    ),
]


nb = {
    "cells": cells,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.10"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

OUT_PATH.write_text(json.dumps(nb, indent=1))
print(f"Wrote {OUT_PATH}")
