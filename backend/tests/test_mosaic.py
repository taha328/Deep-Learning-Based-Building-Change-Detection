from __future__ import annotations

import io
import json
import threading
import time
from datetime import date
from pathlib import Path

import numpy as np
from PIL import Image
import requests
import rasterio
from rasterio.transform import from_origin

from src.config import Settings
from src.domain.coregistration import CoregistrationDiagnostics, CoregistrationResult
from src.domain.inference import derive_new_building_products
from src.domain.mosaic import (
    MosaicResult,
    _download_tile_with_retries,
    _wayback_mosaic_cache_key,
    align_mosaic_pair,
    download_wayback_mosaic,
)
from src.domain.wayback import WaybackRelease


def _tile_bytes(color: tuple[int, int, int]) -> bytes:
    image = Image.new("RGB", (256, 256), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_rgb_tif(path: Path, array: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=array.shape[2],
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=from_origin(0.0, float(array.shape[0]), 1.0, 1.0),
    ) as dst:
        for band_index in range(array.shape[2]):
            dst.write(array[:, :, band_index], band_index + 1)


def _write_mask_tif(path: Path, array: np.ndarray) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=array.shape[1],
        height=array.shape[0],
        count=1,
        dtype=array.dtype,
        crs="EPSG:3857",
        transform=from_origin(0.0, float(array.shape[0]), 1.0, 1.0),
    ) as dst:
        dst.write(array, 1)


def _make_mosaic_result(
    *,
    identifier: str,
    release_date: str,
    geotiff_path: Path,
    valid_mask_path: Path,
    zoom: int = 19,
) -> MosaicResult:
    return MosaicResult(
        identifier=identifier,
        release_date=release_date,
        zoom=zoom,
        tile_count=1,
        available_tile_count=1,
        missing_tile_count=0,
        tile_range=(0, 0, 0, 0),
        bounds_3857=(0.0, 0.0, 1.0, 1.0),
        png_path=geotiff_path.with_suffix(".png"),
        geotiff_path=geotiff_path,
        valid_mask_path=valid_mask_path,
    )


def _wayback_release(identifier: str = "WB_2026_R03", release_num: int = 22869) -> WaybackRelease:
    return WaybackRelease(
        identifier=identifier,
        release_date=date(2026, 1, 1),
        label=f"2026-01-01 | {identifier}",
        release_num=release_num,
        tile_matrix_sets=("default028mm",),
        resource_url_template="https://example.com/{TileMatrixSet}/tile/{TileMatrix}/{TileRow}/{TileCol}",
    )


def test_download_wayback_mosaic_tolerates_partial_missing_tiles(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
    )
    release = _wayback_release()

    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 1, 0, 0))

    def fake_download(url: str, timeout_sec: int):
        if url.endswith("/0/0"):
            return _tile_bytes((255, 0, 0))
        return None

    monkeypatch.setattr("src.domain.mosaic._download_tile", fake_download)

    result = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path,
        label="t1",
        max_tiles=4,
    )

    assert result.tile_count == 2
    assert result.available_tile_count == 1
    assert result.missing_tile_count == 1

    monkeypatch.setattr(
        "src.domain.mosaic.coregister_t1_to_t2_with_arosics",
        lambda **kwargs: CoregistrationResult(
            corrected_t1_path=result.geotiff_path,
            corrected_t1_valid_mask_path=result.valid_mask_path,
            diagnostics=CoregistrationDiagnostics(method="reprojection_only", used_arosics=False),
        ),
    )
    alignment = align_mosaic_pair(result, result, settings=settings, out_dir=tmp_path)
    assert alignment.t1_rgb.shape == (256, 512, 3)
    assert alignment.t1_valid_mask.shape == (256, 512)
    assert bool(alignment.t1_valid_mask[:, :256].all()) is True
    assert bool(alignment.t1_valid_mask[:, 256:].any()) is False


def test_download_wayback_mosaic_raises_when_all_tiles_missing(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
    )
    release = _wayback_release()

    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))
    monkeypatch.setattr("src.domain.mosaic._download_tile", lambda url, timeout_sec: None)

    try:
        download_wayback_mosaic(
            release,
            {"west": 0, "south": 0, "east": 1, "north": 1},
            settings=settings,
            out_dir=tmp_path,
            label="t1",
            max_tiles=1,
        )
    except ValueError as exc:
        assert "has no available imagery tiles" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected all-missing tile coverage to raise.")


def test_download_wayback_mosaic_allows_large_tile_counts_when_no_cap_is_provided(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
    )
    release = _wayback_release()

    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 2, 0, 1))
    monkeypatch.setattr("src.domain.mosaic._download_tile", lambda url, timeout_sec: _tile_bytes((0, 255, 0)))

    result = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path,
        label="t1",
        max_tiles=None,
    )

    assert result.tile_count == 6
    assert result.available_tile_count == 6


def test_download_wayback_mosaic_reuses_shared_cache_for_compatible_requests(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 1, 0, 0))

    calls: list[str] = []

    def fake_download(url: str, timeout_sec: int):
        del timeout_sec
        calls.append(url)
        if url.endswith("/0/0"):
            return _tile_bytes((255, 0, 0))
        return None

    monkeypatch.setattr("src.domain.mosaic._download_tile", fake_download)

    first = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_a",
        label="t2",
        max_tiles=4,
    )
    assert len(calls) == 2
    assert first.available_tile_count == 1
    assert first.missing_tile_count == 1
    assert not (tmp_path / "request_a" / "t2_WB_2026_R03_z19.png").exists()

    def fail_download(url: str, timeout_sec: int):
        del timeout_sec
        raise AssertionError(f"cache hit should not download {url}")

    monkeypatch.setattr("src.domain.mosaic._download_tile", fail_download)

    second = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_b",
        label="t2",
        max_tiles=4,
    )

    assert second.tile_count == first.tile_count
    assert second.available_tile_count == first.available_tile_count
    assert second.missing_tile_count == first.missing_tile_count
    assert second.png_path.exists()
    assert second.geotiff_path.exists()
    assert second.valid_mask_path.exists()
    assert len(list(settings.wayback_mosaic_cache_dir.iterdir())) == 1

    with rasterio.open(first.geotiff_path) as first_src, rasterio.open(second.geotiff_path) as second_src:
        assert first_src.crs == second_src.crs
        assert first_src.transform == second_src.transform
        assert first_src.width == second_src.width
        assert first_src.height == second_src.height
        assert first_src.dtypes == second_src.dtypes
        assert np.array_equal(first_src.read(), second_src.read())

    with rasterio.open(first.valid_mask_path) as first_mask, rasterio.open(second.valid_mask_path) as second_mask:
        assert np.array_equal(first_mask.read(1), second_mask.read(1))
        assert bool(second_mask.read(1)[:, :256].all()) is True
        assert bool(second_mask.read(1)[:, 256:].any()) is False


def test_download_wayback_mosaic_materialization_can_be_disabled(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
        materialize_source_imagery_in_requests=False,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))
    monkeypatch.setattr("src.domain.mosaic._download_tile", lambda url, timeout_sec: _tile_bytes((255, 0, 0)))

    request_dir = tmp_path / "request_no_materialize"
    result = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=request_dir,
        label="t1",
        max_tiles=1,
    )

    assert result.materialized_in_request_dir is False
    assert settings.wayback_mosaic_cache_dir in result.geotiff_path.parents
    assert not (request_dir / "t1_WB_2026_R03_z19.tif").exists()
    assert not (request_dir / "t1_WB_2026_R03_z19_valid_mask.tif").exists()


def test_download_wayback_mosaic_materialization_can_be_enabled(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
        materialize_source_imagery_in_requests=True,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))
    monkeypatch.setattr("src.domain.mosaic._download_tile", lambda url, timeout_sec: _tile_bytes((255, 0, 0)))

    request_dir = tmp_path / "request_materialize"
    result = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=request_dir,
        label="t1",
        max_tiles=1,
    )

    assert result.materialized_in_request_dir is True
    assert result.geotiff_path == request_dir / "t1_WB_2026_R03_z19.tif"
    assert result.valid_mask_path == request_dir / "t1_WB_2026_R03_z19_valid_mask.tif"
    assert result.geotiff_path.exists()
    assert result.valid_mask_path.exists()


def test_wayback_mosaic_cache_key_changes_for_distinct_imagery_inputs() -> None:
    release = _wayback_release()
    base = _wayback_mosaic_cache_key(
        release=release,
        tile_matrix_set="default028mm",
        zoom=19,
        tile_range=(0, 1, 0, 0),
    )

    variants = {
        _wayback_mosaic_cache_key(
            release=_wayback_release("WB_2025_R03", 22000),
            tile_matrix_set="default028mm",
            zoom=19,
            tile_range=(0, 1, 0, 0),
        ),
        _wayback_mosaic_cache_key(
            release=release,
            tile_matrix_set="default028mm",
            zoom=18,
            tile_range=(0, 1, 0, 0),
        ),
        _wayback_mosaic_cache_key(
            release=release,
            tile_matrix_set="default028mm",
            zoom=19,
            tile_range=(0, 2, 0, 0),
        ),
    }

    assert base not in variants
    assert len(variants) == 3


def test_download_wayback_mosaic_rebuilds_invalid_shared_cache_entry(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=1,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))

    cache_key = _wayback_mosaic_cache_key(
        release=release,
        tile_matrix_set=settings.tile_matrix_set,
        zoom=settings.zoom,
        tile_range=(0, 0, 0, 0),
    )
    corrupt_cache_dir = settings.wayback_mosaic_cache_dir / cache_key
    corrupt_cache_dir.mkdir(parents=True)
    (corrupt_cache_dir / "metadata.json").write_text("{not-json")

    calls = {"count": 0}

    def fake_download(url: str, timeout_sec: int):
        del url, timeout_sec
        calls["count"] += 1
        return _tile_bytes((0, 255, 0))

    monkeypatch.setattr("src.domain.mosaic._download_tile", fake_download)

    result = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request",
        label="t1",
        max_tiles=1,
    )

    assert calls["count"] == 1
    assert result.available_tile_count == 1
    assert (corrupt_cache_dir / "metadata.json").read_text().startswith("{\n")


def test_download_wayback_mosaic_reuses_cache_between_complete_and_incomplete_preflight(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=1,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 1, 0, 0))

    calls: list[str] = []

    def fake_download(url: str, timeout_sec: int):
        del timeout_sec
        calls.append(url)
        if url.endswith("/0/0"):
            return _tile_bytes((255, 0, 0))
        return None

    monkeypatch.setattr("src.domain.mosaic._download_tile", fake_download)

    first = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_a",
        label="t1",
        max_tiles=4,
        available_tiles=frozenset({(0, 0)}),
    )

    assert len(calls) == 1
    assert first.available_tile_count == 1
    assert first.missing_tile_count == 1

    def fail_download(url: str, timeout_sec: int):
        del timeout_sec
        raise AssertionError(f"cache hit should not download {url}")

    monkeypatch.setattr("src.domain.mosaic._download_tile", fail_download)

    second = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_b",
        label="t1",
        max_tiles=4,
        available_tiles=None,
    )

    assert second.available_tile_count == 1
    assert second.missing_tile_count == 1
    assert second.png_path.parent.parent == settings.wayback_mosaic_cache_dir
    assert not (tmp_path / "request_b" / "t1_WB_2026_R03_z19.png").exists()


def test_download_wayback_mosaic_reuses_cache_between_incomplete_and_complete_preflight(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=1,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 1, 0, 0))

    calls: list[str] = []

    def fake_download(url: str, timeout_sec: int):
        del timeout_sec
        calls.append(url)
        if url.endswith("/0/0"):
            return _tile_bytes((0, 255, 0))
        return None

    monkeypatch.setattr("src.domain.mosaic._download_tile", fake_download)

    first = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_a",
        label="t2",
        max_tiles=4,
        available_tiles=None,
    )

    assert len(calls) == 2
    assert first.available_tile_count == 1
    assert first.missing_tile_count == 1

    def fail_download(url: str, timeout_sec: int):
        del timeout_sec
        raise AssertionError(f"cache hit should not download {url}")

    monkeypatch.setattr("src.domain.mosaic._download_tile", fail_download)

    second = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_b",
        label="t2",
        max_tiles=4,
        available_tiles=frozenset({(0, 0)}),
    )

    assert second.available_tile_count == 1
    assert second.missing_tile_count == 1
    assert not (tmp_path / "request_b" / "t2_WB_2026_R03_z19.png").exists()


def test_download_wayback_mosaic_does_not_reuse_nonreusable_transient_cache_entry(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=1,
        download_retries=0,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 1, 0, 0))

    calls = {"count": 0}

    def transient_then_success(url: str, timeout_sec: int):
        del timeout_sec
        calls["count"] += 1
        if url.endswith("/0/0"):
            return _tile_bytes((12, 34, 56))
        if calls["count"] == 2:
            raise requests.ConnectTimeout("temporary failure")
        return _tile_bytes((12, 34, 56))

    monkeypatch.setattr("src.domain.mosaic._download_tile", transient_then_success)

    first = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_a",
        label="t1",
        max_tiles=4,
    )

    cache_dir = next(settings.wayback_mosaic_cache_dir.iterdir())
    metadata = json.loads((cache_dir / "metadata.json").read_text())
    assert metadata["reusable"] is False
    assert metadata["transient_failure_count"] == 1
    assert metadata["actual_available_tile_count"] == 1
    assert first.png_path == cache_dir / "mosaic.png"

    second_calls = {"count": 0}

    def success_download(url: str, timeout_sec: int):
        del url, timeout_sec
        second_calls["count"] += 1
        return _tile_bytes((12, 34, 56))

    monkeypatch.setattr("src.domain.mosaic._download_tile", success_download)

    second = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path / "request_b",
        label="t1",
        max_tiles=4,
    )

    assert second_calls["count"] == 2
    metadata = json.loads((cache_dir / "metadata.json").read_text())
    assert metadata["reusable"] is True
    assert metadata["transient_failure_count"] == 0
    assert second.available_tile_count == 2


def test_download_wayback_mosaic_atomic_publish_under_concurrency(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=1,
    )
    release = _wayback_release()
    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 0, 0, 0))

    calls = {"count": 0}

    def fake_download(url: str, timeout_sec: int):
        del url, timeout_sec
        time.sleep(0.05)
        calls["count"] += 1
        return _tile_bytes((90, 45, 15))

    monkeypatch.setattr("src.domain.mosaic._download_tile", fake_download)

    results: list[MosaicResult] = []
    errors: list[Exception] = []

    def worker(label: str) -> None:
        try:
            results.append(
                download_wayback_mosaic(
                    release,
                    {"west": 0, "south": 0, "east": 1, "north": 1},
                    settings=settings,
                    out_dir=tmp_path / label,
                    label="t1",
                    max_tiles=1,
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"request_{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 2
    assert calls["count"] == 1
    cache_entries = [path for path in settings.wayback_mosaic_cache_dir.iterdir() if path.is_dir() and not path.name.endswith(".lock")]
    assert len(cache_entries) == 1
    cache_dir = cache_entries[0]
    metadata = json.loads((cache_dir / "metadata.json").read_text())
    assert metadata["reusable"] is True
    assert metadata["actual_available_tile_count"] == 1
    assert all(result.png_path == cache_dir / "mosaic.png" for result in results)


def test_download_tile_with_retries_recovers_from_transient_timeout(monkeypatch) -> None:
    settings = Settings(
        request_timeout_sec=1,
        download_retries=2,
        download_retry_backoff_initial_sec=0.0,
        download_retry_backoff_max_sec=0.0,
    )
    attempts = {"count": 0}

    class _Response:
        status_code = 200
        content = b"tile-bytes"

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, timeout: int):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise requests.ConnectTimeout("temporary timeout")
        return _Response()

    monkeypatch.setattr("src.domain.mosaic.requests.get", fake_get)

    result = _download_tile_with_retries("https://example.com/tile", settings)
    assert result.status == "available"
    assert result.content == b"tile-bytes"
    assert attempts["count"] == 2


def test_download_wayback_mosaic_marks_retry_exhausted_transient_failures_as_nonreusable(tmp_path, monkeypatch) -> None:
    settings = Settings(
        runtime_cache_dir=tmp_path / "runtime",
        tile_matrix_set="default028mm",
        zoom=19,
        download_workers=2,
        request_timeout_sec=1,
        download_retries=1,
        download_retry_backoff_initial_sec=0.0,
        download_retry_backoff_max_sec=0.0,
    )
    release = _wayback_release()

    monkeypatch.setattr("src.domain.mosaic.tile_range_for_bbox", lambda bbox, zoom: (0, 1, 0, 0))
    attempts: dict[str, int] = {}

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self.status_code = 200
            self.content = payload

        def raise_for_status(self) -> None:
            return None

    def fake_get(url: str, timeout: int):
        attempts[url] = attempts.get(url, 0) + 1
        if url.endswith("/0/0"):
            return _Response(_tile_bytes((0, 255, 0)))
        raise requests.ConnectTimeout("persistent timeout")

    monkeypatch.setattr("src.domain.mosaic.requests.get", fake_get)

    result = download_wayback_mosaic(
        release,
        {"west": 0, "south": 0, "east": 1, "north": 1},
        settings=settings,
        out_dir=tmp_path,
        label="t1",
        max_tiles=4,
    )

    assert result.available_tile_count == 1
    assert result.missing_tile_count == 0
    failing_url = "https://example.com/default028mm/tile/19/0/1"
    assert attempts[failing_url] == 2
    cache_dir = next(settings.wayback_mosaic_cache_dir.iterdir())
    metadata = json.loads((cache_dir / "metadata.json").read_text())
    assert metadata["reusable"] is False
    assert metadata["transient_failure_count"] == 1


def test_derive_new_building_products_respects_valid_comparison_mask() -> None:
    change_prob = np.array([[0.8, 0.8], [0.8, 0.8]], dtype=np.float32)
    t1_prob = np.zeros((2, 2), dtype=np.float32)
    t2_prob = np.ones((2, 2), dtype=np.float32)
    valid_mask = np.array([[True, False], [False, True]])

    products = derive_new_building_products(
        change_prob,
        t1_prob,
        t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=0,
        valid_comparison_mask=valid_mask,
    )

    expected = np.array([[True, False], [False, True]])
    assert np.array_equal(products["change_mask"], expected)
    assert np.array_equal(products["t2_building_mask"], expected)
    assert np.array_equal(products["new_building_mask"], expected)


def test_align_mosaic_pair_uses_coregistered_t1_outputs(tmp_path, monkeypatch) -> None:
    settings = Settings()
    t2 = np.zeros((6, 6, 3), dtype=np.uint8)
    t2[2:5, 2:5] = 255
    t1_shifted = np.zeros((6, 6, 3), dtype=np.uint8)
    t1_shifted[2:5, 1:4] = 255
    t1_corrected = t2.copy()
    valid_mask = np.ones((6, 6), dtype=np.uint8)

    t2_path = tmp_path / "t2.tif"
    t1_shifted_path = tmp_path / "t1_shifted.tif"
    t1_corrected_path = tmp_path / "t1_corrected.tif"
    t2_mask_path = tmp_path / "t2_valid.tif"
    t1_mask_path = tmp_path / "t1_valid.tif"
    t1_corrected_mask_path = tmp_path / "t1_valid_corrected.tif"
    _write_rgb_tif(t2_path, t2)
    _write_rgb_tif(t1_shifted_path, t1_shifted)
    _write_rgb_tif(t1_corrected_path, t1_corrected)
    _write_mask_tif(t2_mask_path, valid_mask)
    _write_mask_tif(t1_mask_path, valid_mask)
    _write_mask_tif(t1_corrected_mask_path, valid_mask)

    t1_mosaic = _make_mosaic_result(
        identifier="WB_2022_R03",
        release_date="2022-03-16",
        geotiff_path=t1_shifted_path,
        valid_mask_path=t1_mask_path,
    )
    t2_mosaic = _make_mosaic_result(
        identifier="WB_2026_R03",
        release_date="2026-03-25",
        geotiff_path=t2_path,
        valid_mask_path=t2_mask_path,
    )

    monkeypatch.setattr(
        "src.domain.mosaic.coregister_t1_to_t2_with_arosics",
        lambda **kwargs: CoregistrationResult(
            corrected_t1_path=t1_corrected_path,
            corrected_t1_valid_mask_path=t1_corrected_mask_path,
            diagnostics=CoregistrationDiagnostics(
                method="arosics_local",
                used_arosics=True,
                corrected_t1_path=str(t1_corrected_path),
                corrected_t1_valid_mask_path=str(t1_corrected_mask_path),
                tie_point_count=12,
            ),
        ),
    )

    alignment = align_mosaic_pair(t1_mosaic, t2_mosaic, settings=settings, out_dir=tmp_path)

    assert np.array_equal(alignment.t1_rgb, t1_corrected)
    assert np.array_equal(alignment.t2_rgb, t2)
    assert alignment.diagnostics["used_arosics"] is True
    assert alignment.diagnostics["method"] == "arosics_local"


def test_coregistration_reduces_shift_false_positives(tmp_path, monkeypatch) -> None:
    settings = Settings(arosics_enabled=True)
    t2 = np.zeros((7, 7, 3), dtype=np.uint8)
    t2[2:5, 2:5] = 255
    t1_shifted = np.zeros((7, 7, 3), dtype=np.uint8)
    t1_shifted[2:5, 1:4] = 255
    valid_mask = np.ones((7, 7), dtype=np.uint8)

    t2_path = tmp_path / "t2_scene.tif"
    t1_shifted_path = tmp_path / "t1_scene_shifted.tif"
    t1_corrected_path = tmp_path / "t1_scene_corrected.tif"
    t2_mask_path = tmp_path / "t2_scene_valid.tif"
    t1_mask_path = tmp_path / "t1_scene_valid.tif"
    _write_rgb_tif(t2_path, t2)
    _write_rgb_tif(t1_shifted_path, t1_shifted)
    _write_rgb_tif(t1_corrected_path, t2)
    _write_mask_tif(t2_mask_path, valid_mask)
    _write_mask_tif(t1_mask_path, valid_mask)

    t1_mosaic = _make_mosaic_result(
        identifier="WB_2022_R03",
        release_date="2022-03-16",
        geotiff_path=t1_shifted_path,
        valid_mask_path=t1_mask_path,
    )
    t2_mosaic = _make_mosaic_result(
        identifier="WB_2026_R03",
        release_date="2026-03-25",
        geotiff_path=t2_path,
        valid_mask_path=t2_mask_path,
    )

    monkeypatch.setattr(
        "src.domain.mosaic.coregister_t1_to_t2_with_arosics",
        lambda **kwargs: CoregistrationResult(
            corrected_t1_path=t1_shifted_path,
            corrected_t1_valid_mask_path=t1_mask_path,
            diagnostics=CoregistrationDiagnostics(method="reprojection_fallback", used_arosics=False),
        ),
    )
    baseline_alignment = align_mosaic_pair(t1_mosaic, t2_mosaic, settings=settings, out_dir=tmp_path)
    baseline_t1_prob = baseline_alignment.t1_rgb[:, :, 0].astype(np.float32) / 255.0
    baseline_t2_prob = baseline_alignment.t2_rgb[:, :, 0].astype(np.float32) / 255.0
    baseline_products = derive_new_building_products(
        np.clip(baseline_t2_prob - baseline_t1_prob, 0.0, 1.0),
        baseline_t1_prob,
        baseline_t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=0,
        new_building_core_distance_pixels=0,
    )

    monkeypatch.setattr(
        "src.domain.mosaic.coregister_t1_to_t2_with_arosics",
        lambda **kwargs: CoregistrationResult(
            corrected_t1_path=t1_corrected_path,
            corrected_t1_valid_mask_path=t1_mask_path,
            diagnostics=CoregistrationDiagnostics(method="arosics_local", used_arosics=True),
        ),
    )
    corrected_alignment = align_mosaic_pair(t1_mosaic, t2_mosaic, settings=settings, out_dir=tmp_path)
    corrected_t1_prob = corrected_alignment.t1_rgb[:, :, 0].astype(np.float32) / 255.0
    corrected_t2_prob = corrected_alignment.t2_rgb[:, :, 0].astype(np.float32) / 255.0
    corrected_products = derive_new_building_products(
        np.clip(corrected_t2_prob - corrected_t1_prob, 0.0, 1.0),
        corrected_t1_prob,
        corrected_t2_prob,
        change_threshold=0.5,
        semantic_threshold=0.5,
        min_new_building_pixels=1,
        old_building_mask_dilation_pixels=0,
        new_building_core_distance_pixels=0,
    )

    assert baseline_products["new_building_mask"].sum() > 0
    assert corrected_products["new_building_mask"].sum() == 0
