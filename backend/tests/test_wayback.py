from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import requests
from shapely.geometry import shape

from src.domain.wayback import (
    TileAvailabilitySummary,
    WaybackRelease,
    preflight_wayback_tile_availability,
    query_metadata_point,
    query_metadata_polygon,
    sample_wayback_metadata_grid,
    summarize_wayback_metadata,
)


class _JsonResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self) -> dict[str, object]:
        return self._payload


def test_preflight_wayback_tile_availability_counts_tiles(monkeypatch) -> None:
    release = WaybackRelease(
        identifier="WB_2026_R03",
        release_date=pd.Timestamp("2026-03-25").date(),
        label="WB_2026_R03",
        release_num=22869,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://wayback.example.com/arcgis/rest/services/World_Imagery/MapServer/tile/22869/{TileMatrix}/{TileRow}/{TileCol}",
    )
    session = requests.Session()
    session.headers.update({"User-Agent": "test"})
    session.request_timeout_sec = 10  # type: ignore[attr-defined]

    monkeypatch.setattr("src.domain.wayback.tile_range_for_bbox", lambda bbox, zoom: (10, 11, 20, 20))

    def fake_get(url: str, *, headers: dict[str, str], timeout: int):
        del headers, timeout
        if url.endswith("/20/10"):
            return _JsonResponse({"data": [1]})
        if url.endswith("/20/11"):
            return _JsonResponse({"data": [0]})
        raise AssertionError(f"unexpected tilemap url: {url}")

    monkeypatch.setattr(session, "get", fake_get)

    result = preflight_wayback_tile_availability(
        session,
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        zoom=19,
        max_workers=10,
    )

    assert result == TileAvailabilitySummary(
        candidate_count=2,
        available_count=1,
        missing_count=1,
        failed_check_count=0,
        preflight_complete=True,
        availability_fraction=0.5,
        available_tiles=frozenset({(10, 20)}),
    )


def test_preflight_wayback_tile_availability_marks_incomplete_when_requests_fail(monkeypatch) -> None:
    release = WaybackRelease(
        identifier="WB_2026_R03",
        release_date=pd.Timestamp("2026-03-25").date(),
        label="WB_2026_R03",
        release_num=22869,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://wayback.example.com/arcgis/rest/services/World_Imagery/MapServer/tile/22869/{TileMatrix}/{TileRow}/{TileCol}",
    )
    session = requests.Session()
    session.headers.update({"User-Agent": "test"})
    session.request_timeout_sec = 10  # type: ignore[attr-defined]

    monkeypatch.setattr("src.domain.wayback.tile_range_for_bbox", lambda bbox, zoom: (10, 11, 20, 20))

    def fake_get(url: str, *, headers: dict[str, str], timeout: int):
        del headers, timeout
        if url.endswith("/20/10"):
            return _JsonResponse({"data": [1]})
        raise requests.ConnectionError("tilemap failed")

    monkeypatch.setattr(session, "get", fake_get)

    result = preflight_wayback_tile_availability(
        session,
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        zoom=19,
        max_workers=10,
    )

    assert result.preflight_complete is False
    assert result.available_count == 1
    assert result.failed_check_count == 1
    assert result.missing_count == 0


def test_summarize_wayback_metadata_reports_polygon_capture_regions(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.domain.wayback._metadata_layer_lookup",
        lambda *args, **kwargs: {4: "Test layer"},
    )
    monkeypatch.setattr(
        "src.domain.wayback._metadata_layer_candidates",
        lambda *args, **kwargs: [4],
    )
    monkeypatch.setattr(
        "src.domain.wayback.sample_wayback_metadata_grid",
        lambda *args, **kwargs: pd.DataFrame({"SRC_DATE": [], "SRC_RES": []}),
    )
    monkeypatch.setattr(
        "src.domain.wayback.query_metadata_polygon",
        lambda *args, **kwargs: [
            {
                "SRC_DATE2": 20220112,
                "_geometry": {
                    "rings": [[
                        [-779236.4, 3895303.9],
                        [-779180.7, 3895303.9],
                        [-779180.7, 3895380.2],
                        [-779236.4, 3895380.2],
                        [-779236.4, 3895303.9],
                    ]]
                },
            },
            {
                "SRC_DATE2": 20221022,
                "_geometry": {
                    "rings": [[
                        [-779180.7, 3895303.9],
                        [-779125.1, 3895303.9],
                        [-779125.1, 3895380.2],
                        [-779180.7, 3895380.2],
                        [-779180.7, 3895303.9],
                    ]]
                },
            },
        ],
    )

    summary = summarize_wayback_metadata(
        SimpleNamespace(),
        "WB_2022_R03",
        {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.0005},
        grid_size=3,
        aoi_geojson={
            "type": "Polygon",
            "coordinates": [[[-7.0, 33.0], [-6.999, 33.0], [-6.999, 33.0005], [-7.0, 33.0005], [-7.0, 33.0]]],
        },
        zoom=19,
    )

    assert summary.dominant_src_date == "2022-01-12"
    assert summary.capture_date_count == 2
    assert summary.mixed_capture_dates is True
    assert summary.metadata_region_count == 2
    assert summary.metadata_coverage_fraction is not None
    assert summary.metadata_coverage_fraction > 0.95


def test_query_metadata_point_reuses_provided_layer_lookup(monkeypatch) -> None:
    session = SimpleNamespace()
    calls: list[str] = []

    def fake_get_json(_session, url: str, *, params=None):
        del _session, params
        calls.append(url)
        if url.endswith("/3/query"):
            return {"features": []}
        if url.endswith("/7/query"):
            return {"features": [{"attributes": {"SRC_DATE": 20220112}}]}
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("src.domain.wayback.get_json", fake_get_json)
    monkeypatch.setattr(
        "src.domain.wayback._metadata_layer_lookup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("layer lookup should not be fetched")),
    )

    result = query_metadata_point(
        session,
        "https://metadata.example.com/MapServer",
        -7.0,
        33.0,
        layer_ids=[3, 7],
        layer_lookup={3: "Layer 3", 7: "Layer 7"},
    )

    assert result is not None
    assert result["metadata_layer_id"] == 7
    assert result["metadata_layer_name"] == "Layer 7"
    assert calls == [
        "https://metadata.example.com/MapServer/3/query",
        "https://metadata.example.com/MapServer/7/query",
    ]


def test_query_metadata_polygon_reuses_provided_layer_context(monkeypatch) -> None:
    session = SimpleNamespace()
    calls: list[str] = []

    def fake_get_json(_session, url: str, *, params=None):
        del _session, params
        calls.append(url)
        if url.endswith("/7/query"):
            return {
                "features": [
                    {
                        "attributes": {"SRC_DATE": 20220112},
                        "geometry": {"rings": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]},
                    }
                ]
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("src.domain.wayback.get_json", fake_get_json)
    monkeypatch.setattr(
        "src.domain.wayback._metadata_layer_lookup",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("layer lookup should not be fetched")),
    )
    monkeypatch.setattr(
        "src.domain.wayback._metadata_layer_candidates",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("layer candidates should not be fetched")),
    )
    monkeypatch.setattr("src.domain.wayback.reproject_geometry", lambda geom, *_args, **_kwargs: geom)
    monkeypatch.setattr("src.domain.wayback.parse_aoi_geometry", lambda geojson: shape(geojson))

    result = query_metadata_polygon(
        session,
        "https://metadata.example.com/MapServer",
        {"type": "Polygon", "coordinates": [[[-7.0, 33.0], [-6.999, 33.0], [-6.999, 33.001], [-7.0, 33.001], [-7.0, 33.0]]]},
        zoom=19,
        layer_ids=[7],
        layer_lookup={7: "Layer 7"},
    )

    assert len(result) == 1
    assert result[0]["metadata_layer_id"] == 7
    assert result[0]["metadata_layer_name"] == "Layer 7"
    assert calls == ["https://metadata.example.com/MapServer/7/query"]


def test_sample_wayback_metadata_grid_uses_parallel_workers(monkeypatch) -> None:
    session = SimpleNamespace()
    calls: list[tuple[float, float]] = []

    def fake_query_metadata_point(_session, _base_url, lon, lat, **kwargs):
        del _session, _base_url, kwargs
        calls.append((lon, lat))
        return {"SRC_DATE": 20220112, "SRC_RES": 0.5}

    monkeypatch.setattr("src.domain.wayback.query_metadata_point", fake_query_metadata_point)

    df = sample_wayback_metadata_grid(
        session,
        "https://metadata.example.com/MapServer",
        {"west": -7.0, "south": 33.0, "east": -6.999, "north": 33.001},
        n=3,
        max_workers=4,
        layer_ids=[7],
        layer_lookup={7: "Layer 7"},
    )

    assert len(df) == 9
    assert len(calls) == 9
