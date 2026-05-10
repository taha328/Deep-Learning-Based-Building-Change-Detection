from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.config import Settings
from src.domain.mapbox_current import download_mapbox_current_mosaic
from src.domain.mosaic import MosaicResult, download_wayback_mosaic
from src.domain.wayback import WaybackRelease


class ImageryProvider(Protocol):
    provider: str


@dataclass(frozen=True)
class EsriWaybackProvider:
    provider: str = "esri_wayback"

    def download(
        self,
        release: WaybackRelease,
        bbox: dict[str, float],
        *,
        settings: Settings,
        zoom: int,
        out_dir: Path,
        label: str,
        available_tiles: frozenset[tuple[int, int]] | None = None,
    ) -> MosaicResult:
        return download_wayback_mosaic(
            release,
            bbox,
            settings=settings,
            zoom=zoom,
            out_dir=out_dir,
            label=label,
            max_tiles=None,
            available_tiles=available_tiles,
        )


@dataclass(frozen=True)
class MapboxCurrentProvider:
    provider: str = "mapbox"

    def download(
        self,
        bbox: dict[str, float],
        *,
        settings: Settings,
        zoom: int | None = None,
        aoi_geojson: dict[str, object] | None = None,
    ) -> MosaicResult:
        return download_mapbox_current_mosaic(bbox, settings=settings, zoom=zoom, aoi_geojson=aoi_geojson)
