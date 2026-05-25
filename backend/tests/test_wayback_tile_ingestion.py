from __future__ import annotations

from src.config import Settings
from src.domain.mosaic import create_wayback_tile_session
from src.domain.wayback_metrics import record_tile_download, render_prometheus_text
from src.domain.wayback_tile_cache import WaybackTileCache


def test_sqlite_wayback_tile_cache_round_trip_and_file_fallback(tmp_path) -> None:
    settings = Settings(runtime_cache_dir=tmp_path / "runtime", wayback_tile_cache_backend="sqlite")
    legacy_path = settings.wayback_tile_cache_dir / "WB_2026_R03" / settings.tile_matrix_set / "18" / "10" / "20.tile"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b"legacy-tile")

    with WaybackTileCache(settings=settings, release_id="WB_2026_R03", layer_id=settings.tile_matrix_set, zoom=18) as cache:
        assert cache.get_tile(z=18, x=10, y=20, file_fallback_path=legacy_path) == b"legacy-tile"
        cache.put_tile(z=18, x=11, y=21, content=b"sqlite-tile")
        assert cache.get_tile(z=18, x=11, y=21, file_fallback_path=tmp_path / "missing.tile") == b"sqlite-tile"


def test_wayback_tile_session_pool_matches_concurrency() -> None:
    session = create_wayback_tile_session(12)
    try:
        adapter = session.get_adapter("https://example.com")
        assert adapter._pool_maxsize == 12
        assert adapter._pool_connections == 12
        assert adapter._pool_block is True
    finally:
        session.close()


def test_wayback_metrics_render_prometheus_text() -> None:
    record_tile_download(
        release="WB_2026_R03",
        zoom=18,
        status="available",
        duration_seconds=0.25,
        byte_size=123,
        cache_hit=False,
        retry_count=1,
        throttle_count=0,
        timeout_count=0,
    )
    text = render_prometheus_text()

    assert "wayback_tiles_total" in text
    assert 'release="WB_2026_R03"' in text
    assert 'zoom="18"' in text
    assert "wayback_tile_download_duration_seconds_count" in text
