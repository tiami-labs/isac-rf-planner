"""Persistent SQLite elevation cache for DEM providers."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .persistent_cache import DEM_NAMESPACE, coord_e5, coord_from_e5

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = DEM_NAMESPACE.root / "elevations.sqlite"
DEFAULT_EXPIRY_DAYS = DEM_NAMESPACE.expiry_days
_SQLITE_BATCH = 500

DATASET_OPENTOPO = "opentopodata_aster30m"
DATASET_GOOGLE = "google_elevation"
DATASET_LOCAL_RASTER = "copernicus_dem30_local"

Point = Tuple[float, float]


class ElevationCacheStore:
    """SQLite-backed L2 cache for ground elevation samples."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        expiry_days: int = DEFAULT_EXPIRY_DAYS,
        enabled: bool = True,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.expiry_days = int(expiry_days)
        self.enabled = bool(enabled)
        if self.enabled:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS elevations (
                    dataset TEXT NOT NULL,
                    lat_e5 INTEGER NOT NULL,
                    lon_e5 INTEGER NOT NULL,
                    elev REAL NOT NULL,
                    cached_at_ms INTEGER NOT NULL,
                    PRIMARY KEY (dataset, lat_e5, lon_e5)
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_elevations_cached_at ON elevations(cached_at_ms);"
            )

    def _cutoff_ms(self) -> int:
        return int((time.time() - self.expiry_days * 86400) * 1000)

    def get_many(self, dataset: str, points: Sequence[Point]) -> Dict[Point, float]:
        """Batch-read elevations for *points* under *dataset*."""
        if not self.enabled or not points:
            return {}

        unique: Dict[Tuple[int, int], Point] = {}
        for lat, lon in points:
            unique[coord_e5(lat, lon)] = (float(lat), float(lon))

        cutoff = self._cutoff_ms()
        hits: Dict[Point, float] = {}
        keys = list(unique.keys())

        with self._connect() as conn:
            for i in range(0, len(keys), _SQLITE_BATCH):
                batch = keys[i : i + _SQLITE_BATCH]
                conn.execute("CREATE TEMP TABLE IF NOT EXISTS batch_points (lat_e5 INTEGER, lon_e5 INTEGER)")
                conn.execute("DELETE FROM batch_points")
                conn.executemany(
                    "INSERT INTO batch_points (lat_e5, lon_e5) VALUES (?, ?)",
                    batch,
                )
                rows = conn.execute(
                    """
                    SELECT e.lat_e5, e.lon_e5, e.elev
                    FROM elevations AS e
                    INNER JOIN batch_points AS b
                        ON e.lat_e5 = b.lat_e5 AND e.lon_e5 = b.lon_e5
                    WHERE e.dataset = ?
                      AND e.cached_at_ms >= ?
                    """,
                    (dataset, cutoff),
                ).fetchall()
                for lat_e5, lon_e5, elev in rows:
                    lat, lon = coord_from_e5(int(lat_e5), int(lon_e5))
                    hits[(lat, lon)] = float(elev)

        if hits:
            logger.info(
                "DEM disk cache HIT: %d/%d points (dataset=%s)",
                len(hits),
                len(unique),
                dataset,
            )
        return hits

    def put_many(self, dataset: str, values: Dict[Point, float]) -> int:
        """Batch-write successful elevations; returns rows written."""
        if not self.enabled or not values:
            return 0

        now_ms = int(time.time() * 1000)
        rows: List[Tuple[str, int, int, float, int]] = []
        for (lat, lon), elev in values.items():
            lat_e5, lon_e5 = coord_e5(lat, lon)
            rows.append((dataset, lat_e5, lon_e5, float(elev), now_ms))

        written = 0
        with self._connect() as conn:
            for i in range(0, len(rows), _SQLITE_BATCH):
                batch = rows[i : i + _SQLITE_BATCH]
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO elevations
                        (dataset, lat_e5, lon_e5, elev, cached_at_ms)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    batch,
                )
                written += len(batch)

        if written:
            logger.info("DEM disk cache WRITE: %d points (dataset=%s)", written, dataset)
        return written

    def clear(self, dataset: Optional[str] = None, older_than_days: Optional[int] = None) -> int:
        """Delete cached rows; returns number of rows removed."""
        if not self.db_path.exists():
            return 0

        with self._connect() as conn:
            if dataset is None and older_than_days is None:
                cur = conn.execute("DELETE FROM elevations")
                return int(cur.rowcount)

            params: List[object] = []
            clauses: List[str] = []
            if dataset is not None:
                clauses.append("dataset = ?")
                params.append(dataset)
            if older_than_days is not None:
                cutoff = int((time.time() - int(older_than_days) * 86400) * 1000)
                clauses.append("cached_at_ms < ?")
                params.append(cutoff)

            where = " AND ".join(clauses)
            cur = conn.execute(f"DELETE FROM elevations WHERE {where}", params)
            return int(cur.rowcount)

    def stats(self) -> Dict[str, object]:
        if not self.db_path.exists():
            return {
                "cache_dir": str(self.db_path.parent),
                "db_path": str(self.db_path),
                "exists": False,
                "total_rows": 0,
                "total_size_mb": 0.0,
                "datasets": {},
            }

        with self._connect() as conn:
            total_rows = int(conn.execute("SELECT COUNT(*) FROM elevations").fetchone()[0])
            dataset_rows = conn.execute(
                "SELECT dataset, COUNT(*) FROM elevations GROUP BY dataset"
            ).fetchall()

        size_mb = self.db_path.stat().st_size / (1024 * 1024)
        return {
            "cache_dir": str(self.db_path.parent),
            "db_path": str(self.db_path),
            "exists": True,
            "total_rows": total_rows,
            "total_size_mb": round(size_mb, 3),
            "datasets": {str(ds): int(count) for ds, count in dataset_rows},
            "expiry_days": self.expiry_days,
        }


def clear_dem_cache(older_than_days: Optional[int] = None) -> int:
    store = ElevationCacheStore()
    if older_than_days is None:
        return store.clear()
    return store.clear(older_than_days=older_than_days)


def get_dem_cache_stats() -> Dict[str, object]:
    return ElevationCacheStore().stats()
