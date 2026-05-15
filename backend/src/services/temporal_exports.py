from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from io import BytesIO
import html
import re
import xml.etree.ElementTree as ET
from typing import Any

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
