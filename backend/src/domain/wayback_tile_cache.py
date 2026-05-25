from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from src.config import Settings

logger = logging.getLogger(__name__)


_SAFE_PART_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_part(value: str) -> str:
    return _SAFE_PART_RE.sub("_", value).strip("._") or "unknown"


class WaybackTileCache:
    """Tile cache with SQLite/MBTiles-like storage and legacy file fallback."""

    def __init__(self, *, settings: Settings, release_id: str, layer_id: str, zoom: int) -> None:
        self.settings = settings
        self.release_id = release_id
        self.layer_id = layer_id
        self.zoom = zoom
        self.backend = settings.wayback_tile_cache_backend
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        sqlite_dir = settings.wayback_tile_sqlite_cache_dir
        self.sqlite_path = (
            sqlite_dir / f"{_safe_part(release_id)}_{_safe_part(layer_id)}_z{zoom}.sqlite"
            if sqlite_dir is not None
            else None
        )

    def __enter__(self) -> "WaybackTileCache":
        if self.backend == "sqlite":
            self._open_sqlite()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def delete_storage(self) -> None:
        self.close()
        if self.sqlite_path is not None:
            for path in (
                self.sqlite_path,
                self.sqlite_path.with_name(f"{self.sqlite_path.name}-wal"),
                self.sqlite_path.with_name(f"{self.sqlite_path.name}-shm"),
            ):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass

    def _open_sqlite(self) -> sqlite3.Connection:
        if self.sqlite_path is None:
            raise RuntimeError("SQLite Wayback tile cache path is not configured.")
        with self._lock:
            if self._conn is not None:
                return self._conn
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.sqlite_path, check_same_thread=False, timeout=30)
            if self.settings.wayback_tile_sqlite_wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tiles (
                    release_id TEXT NOT NULL,
                    layer_id TEXT NOT NULL,
                    z INTEGER NOT NULL,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    content BLOB NOT NULL,
                    content_type TEXT NOT NULL DEFAULT 'image/png',
                    byte_size INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (release_id, layer_id, z, x, y)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tiles_updated_at ON tiles(updated_at)")
            self._conn = conn
            logger.info(
                "WAYBACK_TILE_CACHE_OPEN backend=sqlite release=%s layer=%s zoom=%s path=%s",
                self.release_id,
                self.layer_id,
                self.zoom,
                self.sqlite_path,
            )
            return conn

    def get_tile(self, *, z: int, x: int, y: int, file_fallback_path: Path) -> bytes | None:
        if self.backend == "sqlite":
            conn = self._open_sqlite()
            with self._lock:
                row = conn.execute(
                    """
                    SELECT content
                    FROM tiles
                    WHERE release_id = ? AND layer_id = ? AND z = ? AND x = ? AND y = ?
                    """,
                    (self.release_id, self.layer_id, z, x, y),
                ).fetchone()
            if row is not None:
                return bytes(row[0])
            try:
                if file_fallback_path.exists():
                    content = file_fallback_path.read_bytes()
                    self.put_tile(z=z, x=x, y=y, content=content)
                    logger.debug("WAYBACK_TILE_CACHE_FILE_FALLBACK release=%s z=%s x=%s y=%s", self.release_id, z, x, y)
                    return content
            except OSError:
                return None
            return None

        try:
            return file_fallback_path.read_bytes() if file_fallback_path.exists() else None
        except OSError:
            return None

    def put_tile(self, *, z: int, x: int, y: int, content: bytes) -> None:
        if self.backend == "sqlite":
            conn = self._open_sqlite()
            now = time.time()
            with self._lock:
                conn.execute(
                    """
                    INSERT INTO tiles (
                        release_id, layer_id, z, x, y, content, byte_size, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(release_id, layer_id, z, x, y) DO UPDATE SET
                        content = excluded.content,
                        byte_size = excluded.byte_size,
                        updated_at = excluded.updated_at
                    """,
                    (self.release_id, self.layer_id, z, x, y, content, len(content), now, now),
                )
                conn.commit()
            return

        cache_path = self.settings.wayback_tile_cache_dir / _safe_part(self.release_id) / self.layer_id / str(z) / str(x) / f"{y}.tile"
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = cache_path.with_suffix(".tmp")
            tmp_path.write_bytes(content)
            tmp_path.replace(cache_path)
        except OSError:
            logger.debug("WAYBACK_TILE_CACHE_FILE_WRITE_FAILED path=%s", cache_path, exc_info=True)
