from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from requests import HTTPError

from src.api.main import create_fastapi_app
from src.domain.wayback import WaybackRelease, parse_wmts_capabilities, select_release
from src.schemas import ReleaseListResponse, ReleaseMetadata
from src.services import releases as releases_service
from src.services.releases import ReleaseServiceError, _build_release_session


@pytest.fixture(autouse=True)
def _clear_releases_memory_cache() -> None:
    releases_service._memory_cache.clear()


def _sample_release(identifier: str = "WB_2026_R04") -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2026, 3, 25),
        label=f"2026-03-25 | {identifier}",
        release_num=4,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://example.com/tile/123/{TileMatrix}/{TileRow}/{TileCol}",
    )


def test_select_release_by_identifier() -> None:
    releases = [
        WaybackRelease("WB_2021_R01", date(2021, 1, 1), "one", 1, ("default028mm",), "https://example.com"),
        WaybackRelease("WB_2022_R01", date(2022, 1, 1), "two", 1, ("default028mm",), "https://example.com"),
    ]
    assert select_release(releases, "WB_2022_R01").identifier == "WB_2022_R01"


def test_existing_wmts_release_parser_output_unchanged(monkeypatch) -> None:
    xml = """
    <Capabilities xmlns="https://www.opengis.net/wmts/1.0" xmlns:ows="https://www.opengis.net/ows/1.1">
      <Contents>
        <Layer>
          <ows:Title>World Imagery (Wayback 2026-03-25)</ows:Title>
          <ows:Identifier>WB_2026_R04</ows:Identifier>
          <ResourceURL template="https://example.com/tile/22869/{TileMatrix}/{TileRow}/{TileCol}" />
          <TileMatrixSetLink>
            <TileMatrixSet>default028mm</TileMatrixSet>
          </TileMatrixSetLink>
        </Layer>
      </Contents>
    </Capabilities>
    """.strip()

    class _Response:
        text = xml

        def raise_for_status(self) -> None:
            return None

    class _Session:
        request_timeout_sec = 1

        def get(self, *_args, **_kwargs):
            return _Response()

    releases = parse_wmts_capabilities(_Session(), "https://example.com/capabilities.xml")
    assert len(releases) == 1
    assert releases[0].identifier == "WB_2026_R04"
    assert releases[0].release_date.isoformat() == "2026-03-25"
    assert releases[0].release_num == 22869


def test_wayback_releases_retry_count_is_bounded(tmp_path) -> None:
    settings = releases_service.Settings(runtime_cache_dir=tmp_path, wayback_releases_retries=2)
    session = _build_release_session(settings)
    adapter = session.adapters["https://"]
    assert adapter.max_retries.total == 2
    assert adapter.max_retries.connect == 2
    assert adapter.max_retries.read == 2


def test_wayback_releases_does_not_retry_403(tmp_path) -> None:
    settings = releases_service.Settings(runtime_cache_dir=tmp_path)
    session = _build_release_session(settings)
    adapter = session.adapters["https://"]
    assert 403 not in adapter.max_retries.status_forcelist
    assert 404 not in adapter.max_retries.status_forcelist


def test_wayback_releases_retries_503(tmp_path) -> None:
    settings = releases_service.Settings(runtime_cache_dir=tmp_path)
    session = _build_release_session(settings)
    adapter = session.adapters["https://"]
    assert 503 in adapter.max_retries.status_forcelist
    assert 429 in adapter.max_retries.status_forcelist


def test_wayback_releases_uses_connect_and_read_timeouts(tmp_path, monkeypatch) -> None:
    settings = releases_service.Settings(
        runtime_cache_dir=tmp_path,
        wayback_releases_connect_timeout_seconds=7,
        wayback_releases_read_timeout_seconds=21,
    )
    session = _build_release_session(settings)
    captured: dict[str, object] = {}

    class _Response:
        text = "<broken"

        def raise_for_status(self) -> None:
            return None

    def _fake_get(_url: str, *, params=None, timeout=None):
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(session, "get", _fake_get)
    with pytest.raises(Exception):
        parse_wmts_capabilities(session, "https://example.com")
    assert captured["timeout"] == (7, 21)


def test_api_releases_no_unhandled_traceback_on_connection_error(monkeypatch) -> None:
    app = create_fastapi_app()
    client = TestClient(app)

    def _raise(*, settings=None):
        raise ReleaseServiceError(
            code="wayback_releases_unreachable",
            message="Esri Wayback release service is temporarily unreachable. Check DNS/network and retry.",
            details={"source_url": "https://example.com", "cache_available": False},
        )

    monkeypatch.setattr("src.api.routes.releases.list_releases_api", _raise)
    response = client.get("/api/releases")

    assert response.status_code == 503
    payload = response.json()["detail"]
    assert payload["code"] == "wayback_releases_unreachable"
    assert "traceback" not in str(payload).lower()


def test_api_releases_returns_stale_warning_when_fallback_used(monkeypatch) -> None:
    app = create_fastapi_app()
    client = TestClient(app)
    response_model = ReleaseListResponse(
        releases=[
            ReleaseMetadata(
                identifier="WB_2026_R04",
                release_date="2026-03-25",
                label="2026-03-25 | WB_2026_R04",
                release_num=4,
            )
        ],
        source_status="stale",
        warnings=[
            {
                "code": "wayback_releases_stale_fallback",
                "severity": "warning",
                "message": "Using cached Esri Wayback releases because the live WMTS capabilities endpoint is temporarily unreachable.",
            }
        ],
        fetched_at="2026-05-05T00:00:00Z",
    )
    monkeypatch.setattr("src.api.routes.releases.list_releases_api", lambda *, settings=None: response_model)
    response = client.get("/api/releases")

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_status"] == "stale"
    assert payload["warnings"][0]["code"] == "wayback_releases_stale_fallback"


def test_api_releases_response_backward_compatible(monkeypatch) -> None:
    app = create_fastapi_app()
    client = TestClient(app)
    monkeypatch.setattr(
        "src.api.routes.releases.list_releases_api",
        lambda *, settings=None: ReleaseListResponse(
            releases=[
                ReleaseMetadata(
                    identifier="WB_2026_R04",
                    release_date="2026-03-25",
                    label="2026-03-25 | WB_2026_R04",
                    release_num=4,
                )
            ],
        ),
    )
    response = client.get("/api/releases")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload["releases"], list)
    assert payload["releases"][0]["identifier"] == "WB_2026_R04"


def test_api_releases_frontend_safe_error_payload_when_no_cache(monkeypatch) -> None:
    app = create_fastapi_app()
    client = TestClient(app)

    monkeypatch.setattr(
        "src.api.routes.releases.list_releases_api",
        lambda *, settings=None: (_ for _ in ()).throw(
            ReleaseServiceError(
                code="wayback_releases_unreachable",
                message="Esri Wayback release service is temporarily unreachable. Check DNS/network and retry.",
                details={"source_url": "https://example.com", "cache_available": False},
            )
        ),
    )
    response = client.get("/api/releases")

    assert response.status_code == 503
    payload = response.json()["detail"]
    assert payload["code"] == "wayback_releases_unreachable"
    assert payload["details"]["cache_available"] is False
