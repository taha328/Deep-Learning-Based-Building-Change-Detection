from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from functools import lru_cache

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from lxml import etree
from urllib3.util.retry import Retry
from shapely.geometry import LinearRing, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.config import Settings
from src.domain.tiling import tile_range_for_bbox
from src.utils.geometry import parse_aoi_geometry, reproject_geometry


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
    metadata_region_count: int = 0
    capture_date_count: int = 0
    mixed_capture_dates: bool = False
    metadata_coverage_fraction: float | None = None


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


def build_session(settings: Settings) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Building-Change-Detection/1.0",
            "Accept": "*/*",
        }
    )
    retry = Retry(
        total=max(settings.wayback_releases_retries, 0),
        connect=max(settings.wayback_releases_retries, 0),
        read=max(settings.wayback_releases_retries, 0),
        status=max(settings.wayback_releases_retries, 0),
        backoff_factor=max(settings.wayback_releases_retry_backoff_seconds, 0.0),
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.request_timeout_sec = settings.request_timeout_sec  # type: ignore[attr-defined]
    session.wayback_metadata_workers = settings.wayback_metadata_workers  # type: ignore[attr-defined]
    return session


def get_text(session: requests.Session, url: str, *, params: dict | None = None) -> str:
    timeout = getattr(session, "request_timeout_sec", 120)
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.text


def get_json(session: requests.Session, url: str, *, params: dict | None = None) -> dict:
    timeout = getattr(session, "request_timeout_sec", 120)
    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _metadata_service_payload(session: requests.Session, metadata_base_url: str) -> dict:
    return get_json(session, f"{metadata_base_url}?f=pjson")


def _metadata_layer_lookup(session: requests.Session, metadata_base_url: str) -> dict[int, str]:
    service = _metadata_service_payload(session, metadata_base_url)
    return {int(layer["id"]): str(layer["name"]) for layer in service.get("layers", []) if "id" in layer and "name" in layer}


def _metadata_layer_candidates(
    session: requests.Session,
    metadata_base_url: str,
    *,
    zoom: int,
    layer_lookup: dict[int, str] | None = None,
) -> list[int]:
    layer_lookup = layer_lookup or _metadata_layer_lookup(session, metadata_base_url)
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


def _bbox_to_geojson_polygon(bbox: dict[str, float]) -> dict:
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


def _geometry_to_esri_polygon(geometry: BaseGeometry) -> dict[str, object]:
    if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
        raise ValueError("AOI metadata query requires a Polygon or MultiPolygon geometry.")
    polygons = geometry.geoms if isinstance(geometry, MultiPolygon) else [geometry]
    rings: list[list[list[float]]] = []
    for polygon in polygons:
        assert isinstance(polygon, Polygon)
        rings.append([[float(x), float(y)] for x, y in polygon.exterior.coords])
        for interior in polygon.interiors:
            rings.append([[float(x), float(y)] for x, y in interior.coords])
    return {
        "rings": rings,
        "spatialReference": {"wkid": 3857},
    }


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
    if not outer_rings and rings:
        try:
            polygon = Polygon(rings[0], rings[1:])
            return polygon if polygon.is_valid else polygon.buffer(0)
        except Exception:
            return None
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


def _extract_capture_dates_from_polygon_features(features: list[dict[str, object]]) -> list[str]:
    capture_dates: list[str] = []
    for feature in features:
        capture_date = _src_date_to_iso(feature.get("SRC_DATE2")) or _src_date_to_iso(feature.get("SRC_DATE"))
        if capture_date is not None and capture_date not in capture_dates:
            capture_dates.append(capture_date)
    capture_dates.sort()
    return capture_dates


def parse_wmts_capabilities_xml(xml: str) -> list[WaybackRelease]:
    ns = {
        "wmts": "https://www.opengis.net/wmts/1.0",
        "ows": "https://www.opengis.net/ows/1.1",
    }
    root = etree.fromstring(xml.encode("utf-8"))

    releases: list[WaybackRelease] = []
    xpath_ns = {"name" + "sp" + "aces": ns}
    for layer in root.xpath(".//wmts:Layer", **xpath_ns):
        title = layer.xpath("./ows:Title/text()", **xpath_ns)
        identifier = layer.xpath("./ows:Identifier/text()", **xpath_ns)
        resource_urls = layer.xpath("./wmts:ResourceURL", **xpath_ns)
        tile_matrix_sets = layer.xpath("./wmts:TileMatrixSetLink/wmts:TileMatrixSet/text()", **xpath_ns)
        if not title or not identifier or not resource_urls:
            continue

        label = title[0]
        resource_url_template = resource_urls[0].attrib.get("template")
        if "Wayback" not in label or not resource_url_template:
            continue

        release_date_match = re.search(r"(\d{4}-\d{2}-\d{2})", label)
        release_num_match = re.search(r"/tile/(\d+)/\{TileMatrix\}/", resource_url_template)
        if not release_date_match:
            continue

        releases.append(
            WaybackRelease(
                identifier=identifier[0],
                release_date=pd.to_datetime(release_date_match.group(1)).date(),
                label=f"{release_date_match.group(1)} | {identifier[0]}",
                release_num=int(release_num_match.group(1)) if release_num_match else None,
                tile_matrix_sets=tuple(tile_matrix_sets),
                resource_url_template=resource_url_template,
            )
        )

    releases.sort(key=lambda item: item.release_date)
    return releases


def parse_wmts_capabilities(session: requests.Session, url: str) -> list[WaybackRelease]:
    return parse_wmts_capabilities_xml(get_text(session, url))


def metadata_base_url_from_identifier(identifier: str) -> str:
    match = re.fullmatch(r"WB_(\d{4})_R(\d{2})", identifier)
    if not match:
        raise ValueError(f"Unexpected Wayback identifier format: {identifier}")
    year, release_index = match.groups()
    service_name = f"World_Imagery_Metadata_{year}_r{release_index.lower()}"
    return f"https://metadata.maptiles.arcgis.com/arcgis/rest/services/{service_name}/MapServer"


def query_metadata_point(
    session: requests.Session,
    metadata_base_url: str,
    lon: float,
    lat: float,
    *,
    out_fields: list[str] | None = None,
    layer_ids: list[int] | None = None,
    layer_lookup: dict[int, str] | None = None,
) -> dict | None:
    resolved_layer_lookup = layer_lookup or _metadata_layer_lookup(session, metadata_base_url)
    if layer_ids is None:
        layer_ids = sorted(resolved_layer_lookup)
    if out_fields is None:
        out_fields = [
            "SRC_DATE",
            "SRC_DATE2",
            "SRC_RES",
            "SRC_ACC",
            "SAMP_RES",
            "NICE_NAME",
            "NICE_DESC",
            "ReleaseName",
        ]

    params = {
        "geometry": json.dumps({"x": lon, "y": lat}),
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(out_fields),
        "returnGeometry": "false",
        "f": "pjson",
    }

    for layer_id in layer_ids:
        payload = get_json(session, f"{metadata_base_url}/{layer_id}/query", params=params)
        features = payload.get("features", [])
        if not features:
            continue
        attrs = dict(features[0]["attributes"])
        attrs["metadata_layer_id"] = layer_id
        attrs["metadata_layer_name"] = resolved_layer_lookup.get(layer_id)
        return attrs
    return None


def query_metadata_polygon(
    session: requests.Session,
    metadata_base_url: str,
    aoi_geojson: dict[str, object],
    *,
    zoom: int,
    out_fields: list[str] | None = None,
    layer_ids: list[int] | None = None,
    layer_lookup: dict[int, str] | None = None,
) -> list[dict[str, object]]:
    if out_fields is None:
        out_fields = [
            "SRC_DATE",
            "SRC_DATE2",
            "SRC_RES",
            "SRC_ACC",
            "SAMP_RES",
            "NICE_NAME",
            "NICE_DESC",
            "ReleaseName",
        ]
    resolved_layer_lookup = layer_lookup or _metadata_layer_lookup(session, metadata_base_url)
    candidate_layer_ids = layer_ids or _metadata_layer_candidates(
        session,
        metadata_base_url,
        zoom=zoom,
        layer_lookup=resolved_layer_lookup,
    )
    geometry_3857 = reproject_geometry(parse_aoi_geometry(aoi_geojson), "EPSG:4326", "EPSG:3857")
    params = {
        "geometry": json.dumps(_geometry_to_esri_polygon(geometry_3857)),
        "geometryType": "esriGeometryPolygon",
        "inSR": 3857,
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(out_fields),
        "returnGeometry": "true",
        "f": "pjson",
    }
    for layer_id in candidate_layer_ids:
        payload = get_json(session, f"{metadata_base_url}/{layer_id}/query", params=params)
        features = payload.get("features", [])
        if not features:
            continue
        return [
            {
                **dict(feature.get("attributes", {})),
                "_geometry": feature.get("geometry"),
                "metadata_layer_id": layer_id,
                "metadata_layer_name": resolved_layer_lookup.get(layer_id),
            }
            for feature in features
        ]
    return []


def sample_wayback_metadata_grid(
    session: requests.Session,
    metadata_base_url: str,
    bbox: dict[str, float],
    n: int,
    *,
    max_workers: int = 1,
    layer_ids: list[int] | None = None,
    layer_lookup: dict[int, str] | None = None,
) -> pd.DataFrame:
    if n <= 0:
        return pd.DataFrame()
    lons = pd.Series([bbox["west"] + idx * (bbox["east"] - bbox["west"]) / max(n - 1, 1) for idx in range(n)])
    lats = pd.Series([bbox["south"] + idx * (bbox["north"] - bbox["south"]) / max(n - 1, 1) for idx in range(n)])
    points = [(float(lon), float(lat)) for lat in lats for lon in lons]
    rows: list[dict] = []
    worker_count = max(1, min(max_workers, len(points) or 1))

    def _query_point(point: tuple[float, float]) -> dict | None:
        lon, lat = point
        item = query_metadata_point(
            session,
            metadata_base_url,
            lon,
            lat,
            layer_ids=layer_ids,
            layer_lookup=layer_lookup,
        )
        if item:
            item["query_lon"] = lon
            item["query_lat"] = lat
        return item

    if worker_count == 1:
        for lon, lat in points:
            item = query_metadata_point(
                session,
                metadata_base_url,
                lon,
                lat,
                layer_ids=layer_ids,
                layer_lookup=layer_lookup,
            )
            if item:
                item["query_lon"] = lon
                item["query_lat"] = lat
                rows.append(item)
        return pd.DataFrame(rows)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(_query_point, point): point for point in points}
        for future in as_completed(futures):
            try:
                item = future.result()
            except requests.RequestException:
                continue
            if item:
                rows.append(item)

    result = pd.DataFrame(rows)
    if not result.empty and {"query_lat", "query_lon"}.issubset(result.columns):
        result = result.sort_values(["query_lat", "query_lon"]).reset_index(drop=True)
    return result


def preflight_wayback_tile_availability(
    session: requests.Session,
    release: WaybackRelease,
    bbox: dict[str, float],
    *,
    zoom: int,
    max_workers: int,
) -> TileAvailabilitySummary:
    if release.release_num is None:
        return TileAvailabilitySummary(
            candidate_count=0,
            available_count=0,
            missing_count=0,
            failed_check_count=0,
            preflight_complete=False,
            availability_fraction=None,
            available_tiles=frozenset(),
        )

    x_min, x_max, y_min, y_max = tile_range_for_bbox(bbox, zoom)
    candidate_tiles = [(x, y) for y in range(y_min, y_max + 1) for x in range(x_min, x_max + 1)]
    tilemap_base = release.resource_url_template.split("/tile/")[0] + "/tilemap"
    timeout = getattr(session, "request_timeout_sec", 120)
    headers = dict(session.headers)
    available_tiles: set[tuple[int, int]] = set()
    failed_check_count = 0

    def _check_tile(tile: tuple[int, int]) -> tuple[tuple[int, int], bool]:
        x, y = tile
        url = f"{tilemap_base}/{release.release_num}/{zoom}/{y}/{x}"
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        return tile, bool(payload.get("data") and payload["data"][0] == 1)

    worker_count = max(1, min(max_workers, len(candidate_tiles) or 1))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {executor.submit(_check_tile, tile): tile for tile in candidate_tiles}
        for future in as_completed(future_map):
            try:
                tile, is_available = future.result()
            except requests.RequestException:
                failed_check_count += 1
                continue
            if is_available:
                available_tiles.add(tile)

    candidate_count = len(candidate_tiles)
    available_count = len(available_tiles)
    missing_count = max(candidate_count - available_count - failed_check_count, 0)
    preflight_complete = failed_check_count == 0
    availability_fraction = (
        available_count / candidate_count
        if candidate_count > 0
        else 0.0
    )
    return TileAvailabilitySummary(
        candidate_count=candidate_count,
        available_count=available_count,
        missing_count=missing_count,
        failed_check_count=failed_check_count,
        preflight_complete=preflight_complete,
        availability_fraction=availability_fraction,
        available_tiles=frozenset(available_tiles),
    )


def summarize_wayback_metadata(
    session: requests.Session,
    release_identifier: str,
    bbox: dict[str, float],
    *,
    grid_size: int,
    aoi_geojson: dict[str, object] | None = None,
    zoom: int = 18,
) -> MetadataSummary:
    metadata_base_url = metadata_base_url_from_identifier(release_identifier)
    layer_lookup = _metadata_layer_lookup(session, metadata_base_url)
    candidate_layer_ids = _metadata_layer_candidates(
        session,
        metadata_base_url,
        zoom=zoom,
        layer_lookup=layer_lookup,
    )
    metadata_grid = sample_wayback_metadata_grid(
        session,
        metadata_base_url,
        bbox,
        n=grid_size,
        max_workers=getattr(session, "wayback_metadata_workers", 1),
        layer_ids=candidate_layer_ids,
        layer_lookup=layer_lookup,
    )
    polygon_features = query_metadata_polygon(
        session,
        metadata_base_url,
        aoi_geojson or _bbox_to_geojson_polygon(bbox),
        zoom=zoom,
        layer_ids=candidate_layer_ids,
        layer_lookup=layer_lookup,
    )
    dominant_src_date: str | None = None
    dominant_src_res_m: float | None = None
    if not metadata_grid.empty and metadata_grid["SRC_DATE"].dropna().shape[0] > 0:
        dominant = int(metadata_grid["SRC_DATE"].dropna().mode().iloc[0])
        dominant_src_date = str(pd.to_datetime(str(dominant), format="%Y%m%d").date())
    if not metadata_grid.empty and metadata_grid["SRC_RES"].dropna().shape[0] > 0:
        dominant_src_res_m = float(metadata_grid["SRC_RES"].dropna().median())
    if dominant_src_date is None:
        capture_dates = _extract_capture_dates_from_polygon_features(polygon_features)
        if capture_dates:
            dominant_src_date = capture_dates[0]

    metadata_region_count = len(polygon_features)
    capture_dates = _extract_capture_dates_from_polygon_features(polygon_features)
    coverage_fraction: float | None = None
    if polygon_features:
        try:
            aoi_3857 = reproject_geometry(
                parse_aoi_geometry(aoi_geojson or _bbox_to_geojson_polygon(bbox)),
                "EPSG:4326",
                "EPSG:3857",
            )
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
        metadata_region_count=metadata_region_count,
        capture_date_count=len(capture_dates),
        mixed_capture_dates=len(capture_dates) > 1,
        metadata_coverage_fraction=coverage_fraction,
    )


def select_release(releases: list[WaybackRelease], selector: str) -> WaybackRelease:
    selector = selector.strip()
    if selector == "latest":
        return releases[-1]

    latest_minus = re.fullmatch(r"latest_minus_(\d+)", selector)
    if latest_minus:
        offset = int(latest_minus.group(1))
        index = len(releases) - 1 - offset
        if index < 0:
            raise ValueError(f"Selector {selector!r} is older than the first available release.")
        return releases[index]

    for release in releases:
        if release.identifier == selector or str(release.release_date) == selector:
            return release

    raise ValueError(
        "Release selector must be 'latest', 'latest_minus_N', a Wayback identifier, or YYYY-MM-DD."
    )


@lru_cache(maxsize=1)
def cached_releases_snapshot(wmts_capabilities_url: str, timeout_sec: int) -> list[WaybackRelease]:
    settings = Settings(request_timeout_sec=timeout_sec, wmts_capabilities_url=wmts_capabilities_url)
    session = build_session(settings)
    return parse_wmts_capabilities(session, wmts_capabilities_url)
