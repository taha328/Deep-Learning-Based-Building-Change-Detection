from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import csv
from io import BytesIO
import html
import json
import logging
from pathlib import Path
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

import geopandas as gpd
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from src.config import Settings, get_settings
from src.domain.cache import load_cached_response
from src.domain.mapbox_current import MAPBOX_SOURCE_ID
from src.schemas import RunResponse, TemporalMilestone, TemporalMilestoneMetrics, TemporalProject
from src.services.temporal_projects import (
    _ensure_temporal_derived_geometry_layers,
    _hydrate_milestone_buffer_layers,
    get_temporal_project,
)
from src.utils.geometry import centroid_lonlat, reproject_geometry, utm_epsg_from_lonlat


SURFACE_CRS_MOROCCO = "EPSG:32629"
KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"
DEFAULT_GOOGLE_EARTH_LOOKAT_RANGE_METERS = 2000.0
logger = logging.getLogger(__name__)


TEMPORAL_RESULTS_EXPORT_MEDIA_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "kml": "application/vnd.google-earth.kml+xml",
    "geojson": "application/geo+json",
    "topojson": "application/json",
    "json": "application/json",
    "tsv": "text/tab-separated-values; charset=utf-8",
    "shapefile": "application/zip",
}

TEMPORAL_RESULTS_EXPORT_FILENAMES = {
    "xlsx": "results.xlsx",
    "kml": "results.kml",
    "geojson": "results.geojson",
    "topojson": "results.topojson",
    "json": "results.json",
    "tsv": "results_powerbi.tsv",
    "shapefile": "results_shapefile.zip",
}

TEMPORAL_RESULTS_EXPORT_LABELS = {
    "additions": "Building change polygons / additions",
    "cumulative_growth": "Cumulative growth",
    "buffer_10m": "Cumulative Building-change buffer 10 m",
    "buffer_15m": "Cumulative Building-change buffer 15 m",
    "buffer_20m": "Cumulative Building-change buffer 20 m",
    "diagnostics": "Addition candidate diagnostics",
}

TOPOJSON_DEFAULT_QUANTIZATION = 1_000_000
TOPOJSON_EXPORT_VERSION = "clean-quantized-v3"
TOPOJSON_ALLOWED_LAYERS = ("additions", "buffer_10m", "cumulative_growth")
TOPOJSON_PROPERTY_KEYS = ("id", "project", "date", "year", "period", "layer", "area_m2", "area_ha")
TOPOJSON_LAYER_ID_SLUGS = {
    "additions": "additions",
    "buffer_10m": "buffer10m",
    "cumulative_growth": "cumulative-growth",
}
TOPOJSON_REMOVED_PROPERTY_KEYS = {
    "run_id",
    "release_identifier",
    "release_t1",
    "release_t2",
    "src_date_t1",
    "src_date_t2",
    "source_backend",
    "feature_index",
    "buffer_id",
    "buffer_part_index",
    "source_change_block_id",
    "source_change_count",
    "block_gap_m",
    "cluster_gap_m",
    "kind",
    "release_date",
    "source_building_count",
    "confidence",
    "status",
}
TOPOJSON_ID_PATTERN = re.compile(r"^[0-9]{4}-[a-z0-9-]+-[0-9]{6}$")


@dataclass(frozen=True)
class KmlMilestone:
    source_index: int
    milestone: TemporalMilestone
    archive_date: date
    archive_date_text: str
    date_note: str
    geometry: BaseGeometry


@dataclass(frozen=True)
class KmlView:
    lon: float
    lat: float
    range_m: float = DEFAULT_GOOGLE_EARTH_LOOKAT_RANGE_METERS


def _settings(settings: Settings | None) -> Settings:
    return settings or get_settings()


def _export_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _load_project(project_id: str, settings: Settings) -> TemporalProject:
    project = get_temporal_project(project_id, settings)
    project = _hydrate_milestone_buffer_layers(project, settings)
    return _ensure_temporal_derived_geometry_layers(project)


def _metric_crs(project: TemporalProject) -> str:
    if project.aoi_geojson:
        try:
            geometry = shape(project.aoi_geojson)
            lon, lat = centroid_lonlat(geometry)
            if -13.5 <= lon <= -0.5 and 20.0 <= lat <= 36.5:
                return SURFACE_CRS_MOROCCO
            return f"EPSG:{utm_epsg_from_lonlat(lon, lat)}"
        except Exception:
            return SURFACE_CRS_MOROCCO
    return SURFACE_CRS_MOROCCO


def _features(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    features = payload.get("features")
    return features if isinstance(features, list) else []


def _geometry_from_geojson(payload: dict[str, Any] | None) -> BaseGeometry:
    geometries: list[BaseGeometry] = []
    for feature in _features(payload):
        if not isinstance(feature, dict):
            continue
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            continue
        try:
            geometry = shape(geometry_payload).buffer(0)
        except Exception:
            continue
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        geometries.append(geometry)
    if not geometries:
        return GeometryCollection()
    return unary_union(geometries).buffer(0)


def _area_m2(payload: dict[str, Any] | None, crs: str) -> float | None:
    geometry = _geometry_from_geojson(payload)
    if geometry.is_empty:
        return 0.0
    try:
        return float(reproject_geometry(geometry, "EPSG:4326", crs).area)
    except Exception:
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _percent(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _date_string(value: str | None) -> str | None:
    if not value or value == "current_basemap":
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    if match:
        return match.group(0)
    match = re.search(r"(20\d{2})", value)
    return f"{match.group(1)}-01-01" if match else None


def _parse_date(value: str | None) -> date | None:
    parsed = _date_string(value)
    if not parsed:
        return None
    try:
        return date.fromisoformat(parsed)
    except ValueError:
        return None


def _date_cell(value: str | None) -> date | str | None:
    parsed = _date_string(value)
    if not parsed:
        return value
    try:
        return date.fromisoformat(parsed)
    except ValueError:
        return value


def _run_response_for_milestone(project: TemporalProject, milestone: TemporalMilestone, settings: Settings) -> tuple[RunResponse | None, str | None]:
    if milestone.pair_request_hash:
        return load_cached_response(settings, milestone.pair_request_hash), "t2"
    milestones = project.milestones
    index = milestones.index(milestone)
    if index + 1 < len(milestones):
        next_hash = milestones[index + 1].pair_request_hash
        if next_hash:
            return load_cached_response(settings, next_hash), "t1"
    return None, None


def _imagery_source(project: TemporalProject, milestone: TemporalMilestone) -> str:
    if milestone.release_identifier == MAPBOX_SOURCE_ID or project.latest_source == "mapbox_current":
        return "Mapbox Satellite actuel"
    return "ESRI Wayback"


def _archive_date(
    project: TemporalProject,
    milestone: TemporalMilestone,
    settings: Settings,
    export_now: datetime,
) -> tuple[str | None, str]:
    run_response, side = _run_response_for_milestone(project, milestone, settings)
    summary = run_response.summary if run_response else None

    if milestone.release_identifier == MAPBOX_SOURCE_ID:
        fallback = export_now.date().isoformat()
        return fallback, "Image actuelle / Mapbox - date d'imagerie indisponible; date d'export utilisee."

    if summary is not None and side == "t2":
        for value in (summary.dominant_src_date_t2, summary.release_date_t2):
            parsed = _date_string(value)
            if parsed:
                note = "" if value == summary.dominant_src_date_t2 else "Date utilisée: date de publication, date d'acquisition indisponible"
                return parsed, note
    if summary is not None and side == "t1":
        for value in (summary.dominant_src_date_t1, summary.release_date_t1):
            parsed = _date_string(value)
            if parsed:
                note = "" if value == summary.dominant_src_date_t1 else "Date utilisée: date de publication, date d'acquisition indisponible"
                return parsed, note

    parsed = _date_string(milestone.release_date)
    if parsed:
        return parsed, "Date utilisée: date de publication, date d'acquisition indisponible"

    label_year = re.search(r"(20\d{2})", milestone.release_identifier)
    if label_year:
        return f"{label_year.group(1)}-01-01", "Date utilisée: année du libellé, date d'acquisition indisponible"

    return None, "Date d'archive indisponible"


def _backend_label(project: TemporalProject) -> str:
    return project.execution_config.model_backend if project.execution_config else ""


def _metrics(milestone: TemporalMilestone) -> TemporalMilestoneMetrics:
    return milestone.metrics or TemporalMilestoneMetrics()


def _milestone_rows(project: TemporalProject, settings: Settings, export_now: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    completed = [milestone for milestone in project.milestones if milestone.status == "complete"]
    for index, milestone in enumerate(completed):
        metrics = _metrics(milestone)
        previous = completed[index - 1] if index > 0 else None
        previous_total = previous.metrics.total_area_m2 if previous and previous.metrics else None
        footprint_growth = metrics.total_area_m2 - previous_total if previous_total is not None else None
        archive_date, _note = _archive_date(project, milestone, settings, export_now)
        rows.append(
            {
                "Jalon": milestone.release_identifier,
                "Date d'archive": _date_cell(archive_date),
                "Source d'imagerie": _imagery_source(project, milestone),
                "Surface ajoutée (m²)": metrics.added_area_m2,
                "Emprise bâtie actuelle (m²)": metrics.total_area_m2,
                "Nombre d'ajouts détectés": metrics.additions_feature_count,
                "Nombre de blocs ajoutés": metrics.added_block_count,
                "Densité de croissance (%)": _percent(metrics.total_area_m2, metrics.growth_envelope_area_m2),
                "Ajouté / actuel (%)": _percent(metrics.added_area_m2, metrics.total_area_m2),
                "Emprise / enveloppe (%)": _percent(metrics.total_area_m2, metrics.growth_envelope_area_m2),
                "Surface blocs ajoutés (m²)": metrics.added_block_area_m2,
                "Surface cumulée (m²)": metrics.cumulative_block_area_m2,
                "Surface enveloppe (m²)": metrics.growth_envelope_area_m2,
                "Comparé avec": previous.release_identifier if previous else "",
                "Croissance de l'emprise (m²)": footprint_growth,
                "Croissance en %": _percent(footprint_growth, previous_total),
                "Statut": milestone.status,
            }
        )
    return rows


def _block_rows(project: TemporalProject, settings: Settings, export_now: datetime, crs: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for milestone in project.milestones:
        if milestone.status != "complete":
            continue
        archive_date, _note = _archive_date(project, milestone, settings, export_now)
        cumulative_area = _metrics(milestone).cumulative_block_area_m2
        for index, feature in enumerate(_features(milestone.effective_building_blocks_geojson), start=1):
            geometry_payload = feature.get("geometry") if isinstance(feature, dict) else None
            properties = feature.get("properties") if isinstance(feature, dict) else None
            try:
                geometry = shape(geometry_payload).buffer(0) if isinstance(geometry_payload, dict) else GeometryCollection()
            except Exception:
                geometry = GeometryCollection()
            metric_area = _float(properties.get("area_m2")) if isinstance(properties, dict) else None
            if metric_area is None and not geometry.is_empty:
                try:
                    metric_area = float(reproject_geometry(geometry, "EPSG:4326", crs).area)
                except Exception:
                    metric_area = None
            centroid = geometry.centroid if not geometry.is_empty else None
            block_id = properties.get("block_id") if isinstance(properties, dict) else None
            rows.append(
                {
                    "Jalon": milestone.release_identifier,
                    "Date d'archive": _date_cell(archive_date),
                    "Identifiant bloc": block_id or f"{milestone.release_identifier}-{index}",
                    "Surface (m²)": metric_area,
                    "Surface cumulée (m²)": cumulative_area,
                    "Type géométrie": geometry.geom_type if not geometry.is_empty else "",
                    "Source couche": "Blocs ajoutés",
                    "Longitude centroïde": float(centroid.x) if centroid is not None else None,
                    "Latitude centroïde": float(centroid.y) if centroid is not None else None,
                }
            )
    return rows


def _qc_rows(project: TemporalProject, settings: Settings, export_now: datetime, crs: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for milestone in project.milestones:
        if milestone.status != "complete":
            continue
        archive_date, _note = _archive_date(project, milestone, settings, export_now)
        displayed = milestone.metrics.total_area_m2 if milestone.metrics else None
        recalculated = _area_m2(milestone.cumulative_union_geojson, crs)
        diff = None if displayed is None or recalculated is None else recalculated - displayed
        diff_percent = _percent(diff, displayed)
        comments: list[str] = []
        if milestone.metrics is None:
            comments.append("Indicateurs absents.")
        if recalculated is None:
            comments.append("Surface recalculée indisponible.")
        rows.append(
            {
                "Jalon": milestone.release_identifier,
                "Surface affichée (m²)": displayed,
                "Surface recalculée depuis la géométrie (m²)": recalculated,
                "Écart (m²)": diff,
                "Écart (%)": diff_percent,
                "CRS de calcul": crs,
                "Commentaire": " ".join(comments),
                "Date d'archive": _date_cell(archive_date),
            }
        )
    return rows


def _append_rows(sheet, rows: list[dict[str, Any]], headers: list[str]) -> None:
    sheet.append(headers)
    for row in rows:
        sheet.append([row.get(header) for header in headers])
    _format_sheet(sheet)


def _format_sheet(sheet) -> None:
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        max_length = 0
        column_letter = get_column_letter(column[0].column)
        for cell in column:
            value = cell.value
            if value is not None:
                max_length = max(max_length, len(str(value)))
            if isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
            elif isinstance(value, (int, float)):
                header = str(sheet.cell(row=1, column=cell.column).value or "")
                if "%" in header:
                    cell.number_format = "0.0%"
                elif "m²" in header or "Surface" in header:
                    cell.number_format = '#,##0'
                elif "Longitude" in header or "Latitude" in header:
                    cell.number_format = "0.000000"
        sheet.column_dimensions[column_letter].width = min(max(max_length + 2, 12), 48)


def build_temporal_results_workbook(project_id: str, settings: Settings | None = None) -> bytes:
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()
    crs = _metric_crs(project)
    workbook = Workbook()

    summary = workbook.active
    summary.title = "Synthèse"
    summary_rows = [
        {"Champ": "Identifiant du projet", "Valeur": project.project_id},
        {"Champ": "Nom du projet", "Valeur": project.name},
        {"Champ": "Date d'export", "Valeur": export_now.date()},
        {"Champ": "Nombre de jalons", "Valeur": len(project.milestones)},
        {"Champ": "Source du jalon le plus récent", "Valeur": project.latest_source},
        {"Champ": "Backend utilisé", "Valeur": _backend_label(project)},
        {"Champ": "Système de coordonnées utilisé pour les surfaces", "Valeur": crs},
    ]
    _append_rows(summary, summary_rows, ["Champ", "Valeur"])

    milestones = workbook.create_sheet("Jalons")
    _append_rows(
        milestones,
        _milestone_rows(project, resolved_settings, export_now),
        [
            "Jalon",
            "Date d'archive",
            "Source d'imagerie",
            "Surface ajoutée (m²)",
            "Emprise bâtie actuelle (m²)",
            "Nombre d'ajouts détectés",
            "Nombre de blocs ajoutés",
            "Densité de croissance (%)",
            "Ajouté / actuel (%)",
            "Emprise / enveloppe (%)",
            "Surface blocs ajoutés (m²)",
            "Surface cumulée (m²)",
            "Surface enveloppe (m²)",
            "Comparé avec",
            "Croissance de l'emprise (m²)",
            "Croissance en %",
            "Statut",
        ],
    )

    blocks = workbook.create_sheet("Détails blocs")
    _append_rows(
        blocks,
        _block_rows(project, resolved_settings, export_now, crs),
        [
            "Jalon",
            "Date d'archive",
            "Identifiant bloc",
            "Surface (m²)",
            "Surface cumulée (m²)",
            "Type géométrie",
            "Source couche",
            "Longitude centroïde",
            "Latitude centroïde",
        ],
    )

    qc = workbook.create_sheet("Contrôle qualité")
    _append_rows(
        qc,
        _qc_rows(project, resolved_settings, export_now, crs),
        [
            "Jalon",
            "Surface affichée (m²)",
            "Surface recalculée depuis la géométrie (m²)",
            "Écart (m²)",
            "Écart (%)",
            "CRS de calcul",
            "Commentaire",
            "Date d'archive",
        ],
    )

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def _kml_text(name: str, text: str | None = None) -> ET.Element:
    element = ET.Element(name)
    if text is not None:
        element.text = text
    return element


def _kml_child(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
    child = ET.SubElement(parent, name)
    if text is not None:
        child.text = text
    return child


def _gx_child(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
    child = ET.SubElement(parent, f"{{{GX_NS}}}{name}")
    if text is not None:
        child.text = text
    return child


def _coords(points: Any) -> str:
    return " ".join(f"{float(lon):.8f},{float(lat):.8f},0" for lon, lat, *_ in points)


def _append_polygon(parent: ET.Element, polygon: Polygon) -> None:
    polygon_el = _kml_child(parent, "Polygon")
    _kml_child(polygon_el, "tessellate", "1")
    outer = _kml_child(polygon_el, "outerBoundaryIs")
    outer_ring = _kml_child(outer, "LinearRing")
    _kml_child(outer_ring, "coordinates", _coords(polygon.exterior.coords))
    for interior in polygon.interiors:
        inner = _kml_child(polygon_el, "innerBoundaryIs")
        inner_ring = _kml_child(inner, "LinearRing")
        _kml_child(inner_ring, "coordinates", _coords(interior.coords))


def _append_geometry(parent: ET.Element, geometry: BaseGeometry) -> bool:
    if geometry.is_empty:
        return False
    if isinstance(geometry, Polygon):
        _append_polygon(parent, geometry)
        return True
    if isinstance(geometry, MultiPolygon):
        multi = _kml_child(parent, "MultiGeometry")
        wrote = False
        for polygon in geometry.geoms:
            if not polygon.is_empty:
                _append_polygon(multi, polygon)
                wrote = True
        return wrote
    repaired = geometry.buffer(0)
    if repaired.is_empty:
        return False
    return _append_geometry(parent, repaired)


def _project_view(project: TemporalProject, fallback_geometry: BaseGeometry) -> KmlView:
    geometry: BaseGeometry = GeometryCollection()
    if project.aoi_geojson:
        try:
            geometry = shape(project.aoi_geojson).buffer(0)
        except Exception:
            geometry = GeometryCollection()
    if geometry.is_empty:
        geometry = fallback_geometry
    if geometry.is_empty:
        return KmlView(lon=0.0, lat=0.0)
    point = geometry.representative_point()
    return KmlView(lon=float(point.x), lat=float(point.y))


def _append_look_at(parent: ET.Element, view: KmlView, archive_date: str | None = None) -> ET.Element:
    look_at = _kml_child(parent, "LookAt")
    _kml_child(look_at, "longitude", f"{view.lon:.8f}")
    _kml_child(look_at, "latitude", f"{view.lat:.8f}")
    _kml_child(look_at, "altitude", "0")
    _kml_child(look_at, "heading", "0")
    _kml_child(look_at, "tilt", "0")
    _kml_child(look_at, "range", f"{view.range_m:.2f}")
    _kml_child(look_at, "altitudeMode", "relativeToGround")
    if archive_date:
        timestamp = _gx_child(look_at, "TimeStamp")
        _kml_child(timestamp, "when", archive_date)
    return look_at


def _append_timespan(parent: ET.Element, begin: str, end: str | None) -> None:
    timespan = _kml_child(parent, "TimeSpan")
    _kml_child(timespan, "begin", begin)
    if end:
        _kml_child(timespan, "end", end)


def _all_view_geometry(project: TemporalProject, milestones: list[KmlMilestone]) -> BaseGeometry:
    geometries: list[BaseGeometry] = [entry.geometry for entry in milestones if not entry.geometry.is_empty]
    if project.aoi_geojson:
        try:
            aoi = shape(project.aoi_geojson).buffer(0)
            if not aoi.is_empty:
                geometries.append(aoi)
        except Exception:
            pass
    if not geometries:
        return GeometryCollection()
    return unary_union(geometries).buffer(0)


def _kml_milestones(project: TemporalProject, settings: Settings, export_now: datetime) -> tuple[list[KmlMilestone], list[str]]:
    milestones: list[KmlMilestone] = []
    skipped: list[str] = []
    for source_index, milestone in enumerate(project.milestones):
        if milestone.status != "complete":
            continue
        archive_date_text, date_note = _archive_date(project, milestone, settings, export_now)
        archive_date = _parse_date(archive_date_text or milestone.release_date or milestone.release_identifier)
        if archive_date is None:
            skipped.append(milestone.release_identifier)
            continue
        milestones.append(
            KmlMilestone(
                source_index=source_index,
                milestone=milestone,
                archive_date=archive_date,
                archive_date_text=archive_date.isoformat(),
                date_note=date_note,
                geometry=_cumulative_buffer_geometry(project, source_index),
            )
        )
    return sorted(milestones, key=lambda item: (item.archive_date, item.source_index)), skipped


def _append_tour(document: ET.Element, milestones: list[KmlMilestone], view: KmlView) -> None:
    if not milestones:
        return
    tour = _gx_child(document, "Tour")
    _kml_child(tour, "name", "Chronological building growth")
    playlist = _gx_child(tour, "Playlist")
    for entry in milestones:
        fly_to = _gx_child(playlist, "FlyTo")
        _gx_child(fly_to, "duration", "2.5")
        _gx_child(fly_to, "flyToMode", "smooth")
        _append_look_at(fly_to, view, entry.archive_date_text)
        wait = _gx_child(playlist, "Wait")
        _gx_child(wait, "duration", "1.0")


def _description(project: TemporalProject, milestone: TemporalMilestone, date_note: str) -> str:
    metrics = _metrics(milestone)
    rows = [
        ("Source d'imagerie", _imagery_source(project, milestone)),
        ("Surface ajoutée", f"{metrics.added_area_m2:.0f} m²"),
        ("Emprise bâtie actuelle", f"{metrics.total_area_m2:.0f} m²"),
        ("Ajouts détectés", str(metrics.additions_feature_count)),
        ("Blocs ajoutés", str(metrics.added_block_count)),
        ("Densité de croissance", f"{(_percent(metrics.total_area_m2, metrics.growth_envelope_area_m2) or 0) * 100:.1f}%"),
        ("Ajouté / actuel", f"{(_percent(metrics.added_area_m2, metrics.total_area_m2) or 0) * 100:.1f}%"),
        ("Emprise / enveloppe", f"{(_percent(metrics.total_area_m2, metrics.growth_envelope_area_m2) or 0) * 100:.1f}%"),
    ]
    if date_note:
        rows.append(("Note", date_note))
    if milestone.release_identifier == MAPBOX_SOURCE_ID:
        rows.append(("Limite KML", "Image actuelle / Mapbox - l'imagerie de fond Google Earth Pro n'est pas contrôlable par KML."))
    html_rows = "".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in rows)
    return f"<table>{html_rows}</table>"


def _cumulative_buffer_geometry(project: TemporalProject, milestone_index: int) -> BaseGeometry:
    geometries = [
        _geometry_from_geojson(milestone.buffer_layers_geojson.get("10m") or milestone.buffer_layers_geojson.get("10"))
        for milestone in project.milestones[: milestone_index + 1]
    ]
    geometries = [geometry for geometry in geometries if not geometry.is_empty]
    if not geometries:
        return GeometryCollection()
    return unary_union(geometries).buffer(0)


def _building_name(milestone: TemporalMilestone, feature: dict[str, Any], index: int) -> str:
    properties = feature.get("properties") if isinstance(feature, dict) else None
    block_id = properties.get("block_id") if isinstance(properties, dict) else None
    return f"Building block {block_id or index} - {milestone.release_identifier}"


def _building_description(milestone: TemporalMilestone, feature: dict[str, Any], first_seen: str) -> str:
    properties = feature.get("properties") if isinstance(feature, dict) else None
    rows = [("Jalon première détection", milestone.release_identifier), ("Date première détection", first_seen)]
    if isinstance(properties, dict):
        for key in ("block_id", "source_building_count", "area_m2"):
            value = properties.get(key)
            if value is not None:
                rows.append((str(key), str(value)))
    html_rows = "".join(f"<tr><th>{html.escape(label)}</th><td>{html.escape(value)}</td></tr>" for label, value in rows)
    return f"<table>{html_rows}</table>"


def _append_building_placemarks(folder: ET.Element, entry: KmlMilestone, final_date: str, *, is_baseline: bool = False) -> None:
    features = _features(entry.milestone.effective_building_blocks_geojson)
    if is_baseline and not features:
        features = _features(entry.milestone.cumulative_union_geojson)
    end_date = None if entry.archive_date_text == final_date else final_date
    for feature_index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            continue
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            continue
        try:
            geometry = shape(geometry_payload).buffer(0)
        except Exception:
            continue
        if geometry.is_empty or geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            continue
        placemark = _kml_child(folder, "Placemark")
        _kml_child(placemark, "name", _building_name(entry.milestone, feature, feature_index))
        _kml_child(placemark, "styleUrl", "#buffer-rouge-transparent")
        _kml_child(placemark, "description", _building_description(entry.milestone, feature, entry.archive_date_text))
        _append_timespan(placemark, entry.archive_date_text, end_date)
        _append_geometry(placemark, geometry)


def build_temporal_results_kml(project_id: str, settings: Settings | None = None) -> bytes:
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()

    ET.register_namespace("", KML_NS)
    ET.register_namespace("gx", GX_NS)
    kml = _kml_text(f"{{{KML_NS}}}kml")
    document = _kml_child(kml, "Document")
    _kml_child(document, "name", f"Projet: {project.name or project.project_id}")
    kml_milestones, skipped_milestones = _kml_milestones(project, resolved_settings, export_now)
    if skipped_milestones:
        _kml_child(
            document,
            "description",
            "Jalons exclus du tour chronologique faute de date valide: " + ", ".join(skipped_milestones),
        )
    style = _kml_child(document, "Style")
    style.set("id", "buffer-rouge-transparent")
    line_style = _kml_child(style, "LineStyle")
    _kml_child(line_style, "color", "cc0000ff")
    _kml_child(line_style, "width", "1.5")
    poly_style = _kml_child(style, "PolyStyle")
    _kml_child(poly_style, "color", "660000ff")
    _kml_child(poly_style, "fill", "1")
    _kml_child(poly_style, "outline", "1")

    view_geometry = _all_view_geometry(project, kml_milestones)
    project_view = _project_view(project, view_geometry)
    final_date = kml_milestones[-1].archive_date_text if kml_milestones else None
    _append_look_at(document, project_view)
    for sorted_index, entry in enumerate(kml_milestones):
        milestone = entry.milestone
        folder = _kml_child(document, "Folder")
        label = entry.archive_date_text
        _kml_child(folder, "name", f"Jalon {milestone.release_identifier} - {label}")
        _append_look_at(folder, project_view, entry.archive_date_text)
        geometry = entry.geometry
        if geometry.is_empty:
            _append_building_placemarks(folder, entry, final_date or entry.archive_date_text, is_baseline=sorted_index == 0)
            continue
        placemark = _kml_child(folder, "Placemark")
        _kml_child(placemark, "name", "Buffer cumulatif changement bâtiment 10 m")
        _kml_child(placemark, "styleUrl", "#buffer-rouge-transparent")
        _kml_child(placemark, "description", _description(project, milestone, entry.date_note))
        _append_timespan(placemark, entry.archive_date_text, None if entry.archive_date_text == final_date else final_date)
        _append_geometry(placemark, geometry)
        _append_building_placemarks(folder, entry, final_date or entry.archive_date_text, is_baseline=sorted_index == 0)

    _append_tour(document, kml_milestones, project_view)

    return ET.tostring(kml, encoding="utf-8", xml_declaration=True)


def _source_backend(project: TemporalProject) -> str | None:
    return project.execution_config.model_backend if project.execution_config is not None else None


def _feature_area_m2(feature: dict[str, Any], crs: str) -> float | None:
    properties = feature.get("properties") if isinstance(feature, dict) else None
    if isinstance(properties, dict):
        for key in ("area_m2", "added_area_m2", "surface_m2"):
            value = _float(properties.get(key))
            if value is not None:
                return value
    geometry_payload = feature.get("geometry") if isinstance(feature, dict) else None
    if not isinstance(geometry_payload, dict):
        return None
    try:
        geometry = shape(geometry_payload).buffer(0)
    except Exception:
        return None
    if geometry.is_empty:
        return 0.0
    try:
        return float(reproject_geometry(geometry, "EPSG:4326", crs).area)
    except Exception:
        return None


def _result_layer_payloads(milestone: TemporalMilestone) -> list[tuple[str, dict[str, Any] | None]]:
    return [
        ("additions", milestone.additions_geojson),
        ("cumulative_growth", milestone.cumulative_growth_blocks_geojson),
        ("buffer_10m", milestone.buffer_layers_geojson.get("10m") or milestone.buffer_layers_geojson.get("10")),
        ("buffer_15m", milestone.buffer_layers_geojson.get("15m") or milestone.buffer_layers_geojson.get("15")),
        ("buffer_20m", milestone.buffer_layers_geojson.get("20m") or milestone.buffer_layers_geojson.get("20")),
        ("diagnostics", milestone.automated_candidate_footprint_geojson),
    ]


def _temporal_result_features(project: TemporalProject, settings: Settings, export_now: datetime) -> list[dict[str, Any]]:
    crs = _metric_crs(project)
    backend = _source_backend(project)
    features: list[dict[str, Any]] = []
    for milestone in project.milestones:
        if milestone.status != "complete":
            continue
        archive_date, _note = _archive_date(project, milestone, settings, export_now)
        for layer_type, payload in _result_layer_payloads(milestone):
            for index, feature in enumerate(_features(payload), start=1):
                if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
                    continue
                original_properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
                area_m2 = _feature_area_m2(feature, crs)
                properties = {
                    **original_properties,
                    "project_id": project.project_id,
                    "run_id": milestone.pair_request_hash,
                    "release_identifier": milestone.release_identifier,
                    "date": archive_date or milestone.release_date,
                    "layer_type": layer_type,
                    "layer_label": TEMPORAL_RESULTS_EXPORT_LABELS[layer_type],
                    "feature_index": index,
                    "area_m2": area_m2,
                    "source_backend": backend,
                }
                features.append({"type": "Feature", "properties": properties, "geometry": feature["geometry"]})
    return features


def build_temporal_results_geojson(project_id: str, settings: Settings | None = None) -> bytes:
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()
    payload = {
        "type": "FeatureCollection",
        "name": f"{project.project_id}_temporal_results",
        "features": _temporal_result_features(project, resolved_settings, export_now),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _clean_project_display_name(project: TemporalProject) -> str:
    name = (project.name or "").strip()
    if name and not name.lower().startswith("temporal-"):
        return name.title() if name.isupper() else name
    candidate = re.sub(r"^(temporal|project|qgis)[-_]+", "", project.project_id, flags=re.IGNORECASE)
    words: list[str] = []
    for token in re.split(r"[-_\s]+", candidate):
        if not token:
            continue
        if re.fullmatch(r"mp[a-z0-9]+", token, flags=re.IGNORECASE):
            break
        if re.fullmatch(r"[a-z0-9]{6,}", token, flags=re.IGNORECASE) and any(char.isdigit() for char in token):
            break
        words.append(token)
    return " ".join(word.capitalize() for word in words) or project.project_id


def _topojson_result_layer_payloads(milestone: TemporalMilestone) -> list[tuple[str, dict[str, Any] | None]]:
    return [
        ("additions", milestone.additions_geojson),
        ("buffer_10m", milestone.buffer_layers_geojson.get("10m") or milestone.buffer_layers_geojson.get("10")),
        ("cumulative_growth", milestone.cumulative_growth_envelope_geojson),
        ("buffer_15m", milestone.buffer_layers_geojson.get("15m") or milestone.buffer_layers_geojson.get("15")),
        ("buffer_20m", milestone.buffer_layers_geojson.get("20m") or milestone.buffer_layers_geojson.get("20")),
        ("diagnostics", milestone.automated_candidate_footprint_geojson),
    ]


def _milestone_archive_dates(project: TemporalProject, settings: Settings, export_now: datetime) -> dict[str, str]:
    archive_dates: dict[str, str] = {}
    for milestone in project.milestones:
        archive_date, _note = _archive_date(project, milestone, settings, export_now)
        fallback = _date_string(milestone.release_date) or _date_string(milestone.release_identifier)
        if archive_date or fallback:
            archive_dates[milestone.release_identifier] = archive_date or fallback or ""
    return archive_dates


def _topojson_period(
    layer_type: str,
    milestone_index: int,
    archive_dates: dict[str, str],
    project: TemporalProject,
    current_year: int,
) -> str:
    milestone_years = [
        int(date_text[:4])
        for milestone in project.milestones
        if (date_text := archive_dates.get(milestone.release_identifier))
    ]
    baseline_year = milestone_years[0] if milestone_years else current_year
    if layer_type == "cumulative_growth":
        return f"{baseline_year}-{current_year}"
    previous_year = baseline_year
    for previous in reversed(project.milestones[:milestone_index]):
        previous_date = archive_dates.get(previous.release_identifier)
        if previous_date:
            previous_year = int(previous_date[:4])
            break
    return f"{previous_year}-{current_year}"


def _topojson_clean_features(project: TemporalProject, settings: Settings, export_now: datetime) -> list[dict[str, Any]]:
    crs = _metric_crs(project)
    project_name = _clean_project_display_name(project)
    archive_dates = _milestone_archive_dates(project, settings, export_now)
    sequence_by_layer_year: dict[tuple[int, str], int] = {}
    features: list[dict[str, Any]] = []
    raw_feature_count = 0
    filtered_feature_count = 0

    for milestone_index, milestone in enumerate(project.milestones):
        if milestone.status != "complete":
            continue
        date_text = archive_dates.get(milestone.release_identifier)
        if not date_text:
            continue
        year = int(date_text[:4])
        for layer_type, payload in _topojson_result_layer_payloads(milestone):
            layer_features = _features(payload)
            raw_feature_count += len(layer_features)
            if layer_type not in TOPOJSON_ALLOWED_LAYERS:
                continue
            for feature in layer_features:
                if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
                    continue
                filtered_feature_count += 1
                sequence_key = (year, layer_type)
                sequence_by_layer_year[sequence_key] = sequence_by_layer_year.get(sequence_key, 0) + 1
                area_m2 = _feature_area_m2(feature, crs)
                rounded_area_m2 = round(float(area_m2 or 0.0), 2)
                slug = TOPOJSON_LAYER_ID_SLUGS[layer_type]
                properties = {
                    "id": f"{year}-{slug}-{sequence_by_layer_year[sequence_key]:06d}",
                    "project": project_name,
                    "date": date_text,
                    "year": year,
                    "period": _topojson_period(layer_type, milestone_index, archive_dates, project, year),
                    "layer": layer_type,
                    "area_m2": rounded_area_m2,
                    "area_ha": round(rounded_area_m2 / 10000, 4),
                }
                features.append({"type": "Feature", "properties": properties, "geometry": feature["geometry"]})

    logger.info("TOPOJSON_EXPORT_FEATURES_COLLECTED count=%s", raw_feature_count)
    logger.info(
        "TOPOJSON_EXPORT_FILTERED layers=%s count=%s",
        ",".join(TOPOJSON_ALLOWED_LAYERS),
        filtered_feature_count,
    )
    logger.info("TOPOJSON_EXPORT_PROPERTIES_NORMALIZED allowedKeys=%s", ",".join(TOPOJSON_PROPERTY_KEYS))
    return features


def _topojson_bbox_from_project(project: TemporalProject) -> list[float]:
    if project.aoi_geojson:
        try:
            bounds = shape(project.aoi_geojson).bounds
            if len(bounds) == 4:
                return [round(float(value), 6) for value in bounds]
        except Exception:
            pass
    return [0.0, 0.0, 0.0, 0.0]


def _topojson_bbox(features: list[dict[str, Any]], project: TemporalProject) -> list[float]:
    geometries: list[BaseGeometry] = []
    for feature in features:
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            continue
        try:
            geometry = shape(geometry_payload)
        except Exception:
            continue
        if not geometry.is_empty:
            geometries.append(geometry)
    if not geometries:
        return _topojson_bbox_from_project(project)
    return [round(float(value), 6) for value in unary_union(geometries).bounds]


def _topojson_transform(bbox: list[float], quantization: int) -> dict[str, list[float]]:
    min_lon, min_lat, max_lon, max_lat = bbox
    denominator = max(quantization - 1, 1)
    lon_scale = (max_lon - min_lon) / denominator if max_lon > min_lon else 1.0 / denominator
    lat_scale = (max_lat - min_lat) / denominator if max_lat > min_lat else 1.0 / denominator
    return {
        "scale": [lon_scale, lat_scale],
        "translate": [min_lon, min_lat],
    }


def _quantize_point(point: Any, transform: dict[str, list[float]], quantization: int) -> list[int] | None:
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    try:
        lon = float(point[0])
        lat = float(point[1])
    except (TypeError, ValueError):
        return None
    lon_scale, lat_scale = transform["scale"]
    min_lon, min_lat = transform["translate"]
    qx = 0 if lon_scale == 0 else int(round((lon - min_lon) / lon_scale))
    qy = 0 if lat_scale == 0 else int(round((lat - min_lat) / lat_scale))
    return [max(0, min(quantization - 1, qx)), max(0, min(quantization - 1, qy))]


def _delta_encode_ring(ring: Any, transform: dict[str, list[float]], quantization: int) -> list[list[int]] | None:
    if not isinstance(ring, list) or len(ring) < 4:
        return None
    quantized: list[list[int]] = []
    for point in ring:
        quantized_point = _quantize_point(point, transform, quantization)
        if quantized_point is None:
            continue
        if not quantized or quantized[-1] != quantized_point:
            quantized.append(quantized_point)
    if len(quantized) < 3:
        return None
    if quantized[0] != quantized[-1]:
        quantized.append(quantized[0])
    if len(quantized) < 4:
        return None
    encoded: list[list[int]] = []
    previous = [0, 0]
    for point in quantized:
        encoded.append([point[0] - previous[0], point[1] - previous[1]])
        previous = point
    return encoded


def _topojson_geometry(
    geometry_payload: dict[str, Any],
    arcs: list[Any],
    transform: dict[str, list[float]],
    quantization: int,
) -> dict[str, Any] | None:
    geometry_type = geometry_payload.get("type")
    coordinates = geometry_payload.get("coordinates")
    if geometry_type == "Polygon" and isinstance(coordinates, list):
        polygon_arcs: list[list[int]] = []
        for ring in coordinates:
            encoded_ring = _delta_encode_ring(ring, transform, quantization)
            if encoded_ring is None:
                continue
            arcs.append(encoded_ring)
            polygon_arcs.append([len(arcs) - 1])
        return {"type": "Polygon", "arcs": polygon_arcs} if polygon_arcs else None
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        multipolygon_arcs: list[list[list[int]]] = []
        for polygon in coordinates:
            if not isinstance(polygon, list):
                continue
            polygon_arcs = []
            for ring in polygon:
                encoded_ring = _delta_encode_ring(ring, transform, quantization)
                if encoded_ring is None:
                    continue
                arcs.append(encoded_ring)
                polygon_arcs.append([len(arcs) - 1])
            if polygon_arcs:
                multipolygon_arcs.append(polygon_arcs)
        return {"type": "MultiPolygon", "arcs": multipolygon_arcs} if multipolygon_arcs else None
    return None


def _validate_topojson_payload(payload: dict[str, Any]) -> None:
    if payload.get("type") != "Topology":
        raise ValueError("invalid_topojson_type")
    if not isinstance(payload.get("bbox"), list) or len(payload["bbox"]) != 4:
        raise ValueError("missing_topojson_bbox")
    if not isinstance(payload.get("transform"), dict):
        raise ValueError("missing_topojson_transform")
    objects = payload.get("objects")
    if not isinstance(objects, dict) or not isinstance(objects.get("results"), dict):
        raise ValueError("missing_topojson_results_object")
    results = objects["results"]
    if results.get("type") != "GeometryCollection":
        raise ValueError("invalid_topojson_results_type")
    if not isinstance(payload.get("arcs"), list):
        raise ValueError("missing_topojson_arcs")
    allowed_keys = set(TOPOJSON_PROPERTY_KEYS)
    for geometry in results.get("geometries", []):
        properties = geometry.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("missing_topojson_properties")
        if set(properties) != allowed_keys:
            raise ValueError(f"invalid_topojson_property_keys:{sorted(set(properties) - allowed_keys)}")
        if set(properties) & TOPOJSON_REMOVED_PROPERTY_KEYS:
            raise ValueError("topojson_internal_properties_present")
        if properties["layer"] not in TOPOJSON_ALLOWED_LAYERS:
            raise ValueError("invalid_topojson_layer")
        if not TOPOJSON_ID_PATTERN.fullmatch(str(properties["id"])):
            raise ValueError("invalid_topojson_id")
        expected_ha = round(float(properties["area_m2"]) / 10000, 4)
        if abs(float(properties["area_ha"]) - expected_ha) > 0.0001:
            raise ValueError("invalid_topojson_area_ha")


def build_temporal_results_topojson(project_id: str, settings: Settings | None = None) -> bytes:
    logger.info("TOPOJSON_EXPORT_START projectId=%s", project_id)
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()
    features = _topojson_clean_features(project, resolved_settings, export_now)
    bbox = _topojson_bbox(features, project)
    transform = _topojson_transform(bbox, TOPOJSON_DEFAULT_QUANTIZATION)
    logger.info("TOPOJSON_EXPORT_QUANTIZED quantization=%s", TOPOJSON_DEFAULT_QUANTIZATION)
    logger.info("TOPOJSON_EXPORT_BBOX bbox=%s", bbox)
    arcs: list[Any] = []
    geometries: list[dict[str, Any]] = []
    for feature in features:
        geometry_payload = feature.get("geometry")
        if not isinstance(geometry_payload, dict):
            continue
        geometry = _topojson_geometry(geometry_payload, arcs, transform, TOPOJSON_DEFAULT_QUANTIZATION)
        if geometry is None:
            continue
        geometry["properties"] = feature.get("properties") or {}
        geometries.append(geometry)
    payload = {
        "type": "Topology",
        "bbox": bbox,
        "transform": transform,
        "objects": {"results": {"type": "GeometryCollection", "geometries": geometries}},
        "arcs": arcs,
    }
    try:
        _validate_topojson_payload(payload)
    except ValueError as exc:
        logger.error("TOPOJSON_EXPORT_VALIDATION_FAILED projectId=%s reason=%s", project_id, exc)
        raise
    result = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    logger.info(
        "TOPOJSON_EXPORT_DONE projectId=%s sizeBytes=%s features=%s arcs=%s hasTransform=%s hasBbox=%s",
        project_id,
        len(result),
        len(geometries),
        len(arcs),
        bool(payload.get("transform")),
        bool(payload.get("bbox")),
    )
    return result


def build_temporal_results_json(project_id: str, settings: Settings | None = None) -> bytes:
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()
    milestones = []
    for milestone in project.milestones:
        metrics = milestone.metrics.model_dump(mode="json") if milestone.metrics is not None else {}
        archive_date, date_note = _archive_date(project, milestone, resolved_settings, export_now)
        layer_counts = {
            layer_type: len(_features(payload))
            for layer_type, payload in _result_layer_payloads(milestone)
        }
        milestones.append(
            {
                "release_identifier": milestone.release_identifier,
                "date": archive_date or milestone.release_date,
                "date_note": date_note,
                "status": milestone.status,
                "source_mode": milestone.source_mode,
                "run_id": milestone.pair_request_hash,
                "metrics": metrics,
                "layer_feature_counts": layer_counts,
                "artifacts": [artifact.model_dump(mode="json") for artifact in milestone.artifacts],
            }
        )
    payload = {
        "project": {
            "project_id": project.project_id,
            "name": project.name,
            "semantics": project.semantics,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "latest_source": project.latest_source,
        },
        "run": {
            "source_backend": _source_backend(project),
            "exported_at": export_now.isoformat().replace("+00:00", "Z"),
        },
        "milestones": milestones,
        "artifacts": {
            "download_bundle_path": project.download_bundle_path,
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def build_temporal_results_tsv(project_id: str, settings: Settings | None = None) -> bytes:
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()
    crs = _metric_crs(project)
    columns = [
        "project_id",
        "project_name",
        "run_id",
        "release_identifier",
        "date",
        "layer_type",
        "feature_count",
        "added_surface_m2",
        "area_m2",
        "current_footprint_m2",
        "growth_label",
        "centroid_lon",
        "centroid_lat",
    ]
    rows: list[dict[str, Any]] = []
    for milestone in project.milestones:
        if milestone.status != "complete":
            continue
        metrics = _metrics(milestone)
        archive_date, _note = _archive_date(project, milestone, resolved_settings, export_now)
        for layer_type, payload in _result_layer_payloads(milestone):
            feature_count = len(_features(payload))
            if feature_count == 0:
                continue
            geometry = _geometry_from_geojson(payload)
            centroid = geometry.centroid if not geometry.is_empty else None
            rows.append(
                {
                    "project_id": project.project_id,
                    "project_name": project.name,
                    "run_id": milestone.pair_request_hash or "",
                    "release_identifier": milestone.release_identifier,
                    "date": archive_date or milestone.release_date or "",
                    "layer_type": layer_type,
                    "feature_count": feature_count,
                    "added_surface_m2": metrics.added_area_m2,
                    "area_m2": _area_m2(payload, crs),
                    "current_footprint_m2": metrics.total_area_m2,
                    "growth_label": TEMPORAL_RESULTS_EXPORT_LABELS[layer_type],
                    "centroid_lon": float(centroid.x) if centroid is not None else "",
                    "centroid_lat": float(centroid.y) if centroid is not None else "",
                }
            )
    stream = BytesIO()
    writer = csv.DictWriter(TextIOBytesWriter(stream), fieldnames=columns, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


class TextIOBytesWriter:
    def __init__(self, stream: BytesIO) -> None:
        self._stream = stream

    def write(self, value: str) -> int:
        data = value.encode("utf-8")
        self._stream.write(data)
        return len(value)


def build_temporal_results_shapefile_zip(project_id: str, settings: Settings | None = None) -> bytes:
    resolved_settings = _settings(settings)
    project = _load_project(project_id, resolved_settings)
    export_now = _export_now()
    features_by_layer: dict[str, list[dict[str, Any]]] = {}
    for feature in _temporal_result_features(project, resolved_settings, export_now):
        properties = feature.get("properties") or {}
        layer_type = str(properties.get("layer_type") or "results")
        features_by_layer.setdefault(layer_type, []).append(feature)

    zip_stream = BytesIO()
    with tempfile.TemporaryDirectory(prefix="temporal-shapefile-export-") as tmp_name:
        tmp_dir = Path(tmp_name)
        with zipfile.ZipFile(zip_stream, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for layer_type, features in sorted(features_by_layer.items()):
                records: list[dict[str, Any]] = []
                geometries: list[BaseGeometry] = []
                for feature in features:
                    geometry_payload = feature.get("geometry")
                    if not isinstance(geometry_payload, dict):
                        continue
                    try:
                        geometry = shape(geometry_payload).buffer(0)
                    except Exception:
                        continue
                    if geometry.is_empty:
                        continue
                    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
                    records.append(
                        {
                            "project_id": str(properties.get("project_id") or "")[:254],
                            "release_id": str(properties.get("release_identifier") or "")[:254],
                            "date": str(properties.get("date") or "")[:254],
                            "layer_type": str(properties.get("layer_type") or "")[:254],
                            "area_m2": _float(properties.get("area_m2")),
                            "run_id": str(properties.get("run_id") or "")[:254],
                        }
                    )
                    geometries.append(geometry)
                if not records:
                    continue
                gdf = gpd.GeoDataFrame(records, geometry=geometries, crs="EPSG:4326")
                layer_dir = tmp_dir / layer_type
                layer_dir.mkdir(parents=True, exist_ok=True)
                shp_path = layer_dir / f"{layer_type}.shp"
                gdf.to_file(shp_path, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
                for path in sorted(layer_dir.iterdir()):
                    archive.write(path, arcname=f"{layer_type}/{path.name}")
    return zip_stream.getvalue()


def _project_json_path(project_id: str, settings: Settings) -> Path:
    return settings.temporal_projects_dir / project_id / "project.json"


def _topojson_export_metadata_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.metadata.json")


def _topojson_cache_version_is_valid(path: Path) -> bool:
    metadata_path = _topojson_export_metadata_path(path)
    if not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return metadata.get("version") == TOPOJSON_EXPORT_VERSION


def _write_topojson_export_metadata(path: Path, project_id: str) -> None:
    metadata = {
        "version": TOPOJSON_EXPORT_VERSION,
        "project_id": project_id,
        "quantization": TOPOJSON_DEFAULT_QUANTIZATION,
        "layers": list(TOPOJSON_ALLOWED_LAYERS),
        "property_keys": list(TOPOJSON_PROPERTY_KEYS),
        "updated_at": _export_now().isoformat().replace("+00:00", "Z"),
    }
    _atomic_write_bytes(_topojson_export_metadata_path(path), json.dumps(metadata, separators=(",", ":")).encode("utf-8"))


def _export_cache_is_valid(path: Path, project_id: str, settings: Settings, export_format: str | None = None) -> bool:
    if not path.is_file():
        return False
    if export_format == "topojson" and not _topojson_cache_version_is_valid(path):
        return False
    project_path = _project_json_path(project_id, settings)
    if not project_path.is_file():
        return True
    return path.stat().st_mtime_ns >= project_path.stat().st_mtime_ns


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{datetime.now(UTC).timestamp():.6f}.tmp")
    try:
        tmp_path.write_bytes(payload)
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def build_temporal_results_export_file(project_id: str, export_format: str, settings: Settings | None = None) -> Path:
    resolved_settings = _settings(settings)
    normalized_format = export_format.lower()
    if normalized_format == "zip":
        normalized_format = "shapefile"
    if normalized_format not in TEMPORAL_RESULTS_EXPORT_FILENAMES:
        raise ValueError(f"Unsupported temporal results export format: {export_format}")

    started_at = datetime.now(UTC)
    cache_path = (
        resolved_settings.temporal_projects_dir
        / project_id
        / "exports"
        / TEMPORAL_RESULTS_EXPORT_FILENAMES[normalized_format]
    )
    logger.info("EXPORT_REQUEST projectId=%s format=%s", project_id, normalized_format)
    if _export_cache_is_valid(cache_path, project_id, resolved_settings, normalized_format):
        logger.info("EXPORT_CACHE_HIT projectId=%s format=%s path=%s", project_id, normalized_format, cache_path)
        return cache_path

    logger.info("EXPORT_GENERATE_START projectId=%s format=%s", project_id, normalized_format)
    try:
        builders = {
            "xlsx": build_temporal_results_workbook,
            "kml": build_temporal_results_kml,
            "geojson": build_temporal_results_geojson,
            "topojson": build_temporal_results_topojson,
            "json": build_temporal_results_json,
            "tsv": build_temporal_results_tsv,
            "shapefile": build_temporal_results_shapefile_zip,
        }
        payload = builders[normalized_format](project_id, settings=resolved_settings)
        _atomic_write_bytes(cache_path, payload)
        if normalized_format == "topojson":
            _write_topojson_export_metadata(cache_path, project_id)
    except Exception as exc:
        logger.exception("EXPORT_GENERATE_FAILED projectId=%s format=%s error=%s", project_id, normalized_format, exc)
        raise
    duration_ms = round((datetime.now(UTC) - started_at).total_seconds() * 1000, 2)
    logger.info(
        "EXPORT_GENERATE_DONE projectId=%s format=%s bytes=%s durationMs=%s",
        project_id,
        normalized_format,
        cache_path.stat().st_size,
        duration_ms,
    )
    return cache_path
