from __future__ import annotations

from shapely.geometry import Polygon, mapping

from src.domain.postprocess import AdditionCandidateFilterSettings, filter_addition_candidates
from src.utils.geometry import reproject_geometry


METRIC_CRS = "EPSG:32629"


def _wgs84_box(minx: float, miny: float, maxx: float, maxy: float) -> dict:
    return mapping(reproject_geometry(Polygon([(minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy)]), METRIC_CRS, "EPSG:4326"))


def _fc(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def _feature(minx: float, miny: float, maxx: float, maxy: float, **properties) -> dict:
    return {"type": "Feature", "geometry": _wgs84_box(minx, miny, maxx, maxy), "properties": properties}


def _filter(candidate: dict, existing: dict | None = None):
    return filter_addition_candidates(
        _fc(candidate),
        existing_footprint_geojson=existing,
        settings=AdditionCandidateFilterSettings(),
    )


def test_high_existing_overlap_candidate_rejected() -> None:
    existing = _fc(_feature(0, 0, 20, 20))
    result = _filter(_feature(2, 2, 18, 18, mean_probability=0.9, max_probability=0.95), existing)

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is False
    assert props["reject_reason"] == "overlaps_existing_footprint"


def test_standalone_small_rectangle_is_kept_and_flagged() -> None:
    result = _filter(_feature(100, 100, 101.5, 110, mean_probability=0.8, max_probability=0.9))

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is True
    assert props["reject_reason"] is None
    assert props["review_flag"] == "small_thin_standalone"


def test_small_thin_near_old_edge_low_confidence_rejected() -> None:
    existing = _fc(_feature(0, 0, 20, 20))
    result = _filter(_feature(20.1, 3, 21.6, 18, mean_probability=0.6, max_probability=0.7), existing)

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is False
    assert props["reject_reason"] == "thin_low_confidence_old_edge_artifact"


def test_small_thin_near_old_edge_high_confidence_not_rejected() -> None:
    existing = _fc(_feature(0, 0, 20, 20))
    result = _filter(_feature(20.1, 3, 21.6, 18, mean_probability=0.9, max_probability=0.95), existing)

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is True
    assert props["reject_reason"] is None
    assert props["review_flag"] == "small_thin_high_confidence"


def test_small_thin_standalone_candidate_not_rejected() -> None:
    result = _filter(_feature(50, 50, 51.5, 65, mean_probability=0.55, max_probability=0.7))

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is True
    assert props["reject_reason"] is None
    assert props["review_flag"] == "small_thin_standalone"


def test_tiny_speck_below_min_area_rejected() -> None:
    result = _filter(_feature(0, 0, 2, 2, mean_probability=0.99, max_probability=1.0))

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is False
    assert props["reject_reason"] == "below_min_area"


def test_large_rectangular_building_not_rejected_for_thinness() -> None:
    existing = _fc(_feature(0, 0, 20, 20))
    result = _filter(_feature(40, 40, 46, 70, mean_probability=0.6, max_probability=0.8), existing)

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is True
    assert props["reject_reason"] is None


def test_missing_probability_metrics_flag_review_not_thinness_rejection() -> None:
    existing = _fc(_feature(0, 0, 20, 20))
    result = _filter(_feature(20.1, 3, 21.6, 18), existing)

    props = result.diagnostics_geojson["features"][0]["properties"]
    assert props["kept"] is True
    assert props["reject_reason"] is None
    assert props["review_flag"] == "probability_metric_unavailable"


def test_diagnostics_include_reject_reason_and_review_flag() -> None:
    result = _filter(_feature(100, 100, 101.5, 110, mean_probability=0.8, max_probability=0.9))
    props = result.diagnostics_geojson["features"][0]["properties"]

    assert "reject_reason" in props
    assert "review_flag" in props
    assert "existing_overlap_ratio" in props
    assert "old_edge_overlap_ratio" in props
    assert "thinness_ratio" in props
