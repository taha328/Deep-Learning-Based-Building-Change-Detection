from __future__ import annotations

from functools import lru_cache

from src.config import Settings
from src.domain.wayback import WaybackRelease, build_session, parse_wmts_capabilities
from src.schemas import ReleaseListResponse, ReleaseMetadata


@lru_cache(maxsize=1)
def _get_releases(wmts_capabilities_url: str, request_timeout_sec: int) -> tuple[WaybackRelease, ...]:
    settings = Settings(
        wmts_capabilities_url=wmts_capabilities_url,
        request_timeout_sec=request_timeout_sec,
    )
    session = build_session(settings)
    return tuple(parse_wmts_capabilities(session, settings.wmts_capabilities_url))


def list_releases(settings: Settings) -> list[WaybackRelease]:
    return list(_get_releases(settings.wmts_capabilities_url, settings.request_timeout_sec))


def list_releases_response(settings: Settings) -> ReleaseListResponse:
    releases = [
        ReleaseMetadata(
            identifier=item.identifier,
            release_date=str(item.release_date),
            label=item.label,
            release_num=item.release_num,
        )
        for item in list_releases(settings)
    ]
    return ReleaseListResponse(releases=releases)
