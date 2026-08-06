"""Persistent local OSM geometry cache for large-area RF planning.

The normal DVT execution surface stays unchanged::

    uvicorn agentic_rf_planner.api.rest:app --reload

On the first DVT request for an area, :class:`AutoCachingOSMProvider` creates
``data/osm/rf_geometry.sqlite`` (or the configured path), downloads the missing
OSM geometry through Overpass, commits it to SQLite/R*Tree, and continues the
same planning request. Later requests whose AOI is contained by a completed
cache region use SQLite only.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
import logging
import math
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ..pipeline.schemas import LatLon
from .osm_map_provider import (
    OSMMapProvider,
    _bbox_around_point,
    _distance_m,
    _tile_centers_for_square,
)

logger = logging.getLogger(__name__)

LOCAL_OSM_SCHEMA_VERSION = "2"
SUPPORTED_LOCAL_OSM_SCHEMA_VERSIONS = {"1", LOCAL_OSM_SCHEMA_VERSION}
DEFAULT_LOCAL_OSM_PATH = Path("data/osm/rf_geometry.sqlite")
DEFAULT_ADAPTIVE_MIN_HALF_SIZE_M = 5000.0

FetchTile = Callable[[LatLon, float], Optional[Tuple[List[dict], List[dict]]]]

_DATABASE_LOCKS_GUARD = threading.Lock()
_DATABASE_LOCKS: Dict[str, threading.RLock] = {}


class LocalOSMDatabaseError(RuntimeError):
    """Raised when the local geometry database is incompatible."""


class LocalOSMAcquisitionError(RuntimeError):
    """Raised when an uncached AOI cannot be acquired from OSM."""


def _database_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _DATABASE_LOCKS_GUARD:
        lock = _DATABASE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DATABASE_LOCKS[key] = lock
        return lock


def resolve_local_osm_database(explicit_path: Optional[str] = None) -> Path:
    """Resolve the persistent DVT AOI cache database.

    Resolution order:
      1. Explicit path supplied by the caller.
      2. ``DVT_OSM_DATABASE`` environment variable.
      3. ``RF_OSM_DATABASE`` environment variable.
      4. ``data/osm/rf_geometry.sqlite`` relative to the current project.

    The returned path may not exist yet; automatic DVT acquisition creates it.
    """

    raw = (
        explicit_path
        or os.environ.get("DVT_OSM_DATABASE")
        or os.environ.get("RF_OSM_DATABASE")
    )
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        return candidate.resolve()

    candidates = [Path.cwd() / DEFAULT_LOCAL_OSM_PATH]
    try:
        import agentic_rf_planner

        current = Path(agentic_rf_planner.__file__).resolve().parent
        for _ in range(7):
            candidates.append(current / DEFAULT_LOCAL_OSM_PATH)
            parent = current.parent
            if parent == current:
                break
            current = parent
    except Exception:
        pass

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return (Path.cwd() / DEFAULT_LOCAL_OSM_PATH).resolve()


def _initialize_auto_cache_database(path: Path) -> None:
    """Create or migrate the SQLite/R*Tree database used by automatic AOI caching."""

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA busy_timeout=60000;
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS features (
                id INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('building', 'landuse')),
                tags_json TEXT NOT NULL,
                geometry_json TEXT NOT NULL,
                material TEXT,
                height_m REAL,
                min_lon REAL NOT NULL,
                min_lat REAL NOT NULL,
                max_lon REAL NOT NULL,
                max_lat REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS features_kind_idx ON features(kind);
            CREATE INDEX IF NOT EXISTS features_source_idx
                ON features(source_type, source_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS feature_rtree USING rtree(
                id,
                min_lon, max_lon,
                min_lat, max_lat
            );
            CREATE TABLE IF NOT EXISTS feature_keys (
                feature_key TEXT PRIMARY KEY,
                feature_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aoi_cache (
                id INTEGER PRIMARY KEY,
                center_lat REAL NOT NULL,
                center_lon REAL NOT NULL,
                radius_m REAL NOT NULL,
                min_lon REAL NOT NULL,
                min_lat REAL NOT NULL,
                max_lon REAL NOT NULL,
                max_lat REAL NOT NULL,
                acquired_at TEXT NOT NULL,
                source TEXT NOT NULL,
                building_count INTEGER NOT NULL,
                landuse_count INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS aoi_cache_bounds_idx
                ON aoi_cache(min_lon, min_lat, max_lon, max_lat);
            CREATE TABLE IF NOT EXISTS osm_fetch_tiles (
                tile_key TEXT PRIMARY KEY,
                center_lat REAL NOT NULL,
                center_lon REAL NOT NULL,
                half_size_m REAL NOT NULL,
                min_lon REAL NOT NULL,
                min_lat REAL NOT NULL,
                max_lon REAL NOT NULL,
                max_lat REAL NOT NULL,
                acquired_at TEXT NOT NULL,
                building_count INTEGER NOT NULL,
                landuse_count INTEGER NOT NULL
            );
            """
        )
        existing = conn.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if existing is not None and str(existing[0]) not in SUPPORTED_LOCAL_OSM_SCHEMA_VERSIONS:
            raise LocalOSMDatabaseError(
                f"Unsupported local OSM schema {existing[0]!r} in {path}"
            )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (LOCAL_OSM_SCHEMA_VERSION,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES('created_at', ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO metadata(key, value) VALUES('cache_mode', 'automatic_aoi') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row_to_feature(row: sqlite3.Row) -> Dict[str, Any]:
    tags = json.loads(row["tags_json"] or "{}")
    geometry = json.loads(row["geometry_json"] or "[]")
    feature: Dict[str, Any] = {
        "id": int(row["id"]),
        "osm_id": row["source_id"],
        "osm_type": row["source_type"] or "local",
        "tags": tags,
        "geometry": geometry,
    }
    if row["material"] is not None:
        feature["material"] = row["material"]
    if row["height_m"] is not None:
        feature["height_m"] = float(row["height_m"])
    return feature


def _bbox_values(center: LatLon, radius_m: float) -> Tuple[float, float, float, float]:
    min_lat, min_lon, max_lat, max_lon = [
        float(v) for v in _bbox_around_point(center.lat, center.lon, radius_m).split(",")
    ]
    return min_lon, min_lat, max_lon, max_lat


def _bbox_intersects_circle(
    *,
    center: LatLon,
    radius_m: float,
    min_lon: float,
    min_lat: float,
    max_lon: float,
    max_lat: float,
) -> bool:
    clamped_lat = min(max(center.lat, min_lat), max_lat)
    clamped_lon = min(max(center.lon, min_lon), max_lon)
    return _distance_m(center, LatLon(lat=clamped_lat, lon=clamped_lon)) <= radius_m


def _feature_bbox(geometry: Sequence[dict]) -> Optional[Tuple[float, float, float, float]]:
    coords: List[Tuple[float, float]] = []
    for point in geometry:
        try:
            coords.append((float(point["lon"]), float(point["lat"])))
        except (KeyError, TypeError, ValueError):
            continue
    if len(coords) < 3:
        return None
    lons = [coord[0] for coord in coords]
    lats = [coord[1] for coord in coords]
    return min(lons), min(lats), max(lons), max(lats)


def _feature_key(feature: dict, kind: str) -> str:
    source_type = str(feature.get("osm_type") or "osm")
    source_id = str(feature.get("osm_id") or feature.get("id") or "unknown")
    polygon_id = str(feature.get("id") or "0")
    return f"{source_type}:{source_id}:{kind}:{polygon_id}"


def _tile_key(center: LatLon, half_size_m: float) -> str:
    bbox = _bbox_values(center, half_size_m)
    canonical = ":".join(f"{value:.8f}" for value in bbox)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class LocalOSMProvider(OSMMapProvider):
    """OSM MapProvider backed by a prepared SQLite/R*Tree database."""

    def __init__(
        self,
        database_path: Union[str, Path],
        *,
        slice_height_m: Optional[float] = None,
        read_only: bool = True,
    ) -> None:
        super().__init__(cache_radius_m=1000.0, slice_height_m=slice_height_m)
        self.database_path = Path(database_path).expanduser().resolve()
        self.read_only = bool(read_only)
        self._validate_database()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            uri = f"file:{self.database_path.as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=60.0)
            conn.execute("PRAGMA query_only=ON")
        else:
            conn = sqlite3.connect(str(self.database_path), timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def _validate_database(self) -> None:
        if not self.database_path.exists():
            raise LocalOSMDatabaseError(f"Local OSM database not found: {self.database_path}")
        try:
            with closing(self._connect()) as conn:
                schema = conn.execute(
                    "SELECT value FROM metadata WHERE key='schema_version'"
                ).fetchone()
                if schema is None or str(schema[0]) not in SUPPORTED_LOCAL_OSM_SCHEMA_VERSIONS:
                    raise LocalOSMDatabaseError(
                        f"Unsupported local OSM schema in {self.database_path}"
                    )
                conn.execute("SELECT id FROM features LIMIT 1").fetchall()
                conn.execute("SELECT id FROM feature_rtree LIMIT 1").fetchall()
        except LocalOSMDatabaseError:
            raise
        except sqlite3.Error as exc:
            raise LocalOSMDatabaseError(
                f"Invalid local OSM database {self.database_path}: {exc}"
            ) from exc

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        radius = max(1.0, float(radius_m))
        if (
            self._prefetched
            and self._cache_center is not None
            and _distance_m(center, self._cache_center) < 1.0
            and radius <= self._prefetch_radius_m
        ):
            return

        buildings, landuse = self._query_circle(center, radius, kinds=("building", "landuse"))
        self._cached_buildings = buildings
        self._cached_landuse = landuse
        self._build_spatial_indexes(center, radius)
        self._cache_center = center
        self._prefetch_radius_m = radius
        self._prefetched = True
        logger.info(
            "Local OSM prefetch complete: database=%s radius=%.0fm buildings=%d landuse=%d",
            self.database_path,
            radius,
            len(buildings),
            len(landuse),
        )

    def _query_circle(
        self,
        center: LatLon,
        radius_m: float,
        *,
        kinds: Sequence[str],
    ) -> Tuple[List[dict], List[dict]]:
        min_lon, min_lat, max_lon, max_lat = _bbox_values(center, radius_m)
        placeholders = ",".join("?" for _ in kinds)
        sql = f"""
            SELECT
                f.id,
                f.source_id,
                f.source_type,
                f.kind,
                f.tags_json,
                f.geometry_json,
                f.material,
                f.height_m,
                f.min_lon,
                f.min_lat,
                f.max_lon,
                f.max_lat
            FROM feature_rtree AS r
            JOIN features AS f ON f.id = r.id
            WHERE f.kind IN ({placeholders})
              AND r.max_lon >= ?
              AND r.min_lon <= ?
              AND r.max_lat >= ?
              AND r.min_lat <= ?
            ORDER BY f.id
        """
        params: List[Any] = list(kinds) + [min_lon, max_lon, min_lat, max_lat]
        buildings: List[dict] = []
        landuse: List[dict] = []
        with closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()
        for row in rows:
            if not _bbox_intersects_circle(
                center=center,
                radius_m=radius_m,
                min_lon=float(row["min_lon"]),
                min_lat=float(row["min_lat"]),
                max_lon=float(row["max_lon"]),
                max_lat=float(row["max_lat"]),
            ):
                continue
            feature = _row_to_feature(row)
            if row["kind"] == "building":
                buildings.append(feature)
            else:
                landuse.append(feature)
        return buildings, landuse

    def _get_buildings_near_point(self, center: LatLon, radius_m: float) -> List[dict]:
        buildings, _ = self._query_circle(center, max(1.0, float(radius_m)), kinds=("building",))
        return buildings

    def _get_landuse_near_point(self, center: LatLon, radius_m: float) -> List[dict]:
        _, landuse = self._query_circle(center, max(1.0, float(radius_m)), kinds=("landuse",))
        return landuse

    @property
    def source_metadata(self) -> Dict[str, str]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT key, value FROM metadata ORDER BY key").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}


class AutoCachingOSMProvider(LocalOSMProvider):
    """Local SQLite provider that automatically acquires missing DVT AOIs.

    Acquisition behavior for a missing AOI:

    1. Try one Overpass request for the complete AOI bounding square.
    2. If that request fails, use the existing bounded 5 km-half-size tile layout.
    3. Persist every successful tile immediately, so a failed/interrupted run
       resumes without re-downloading completed tiles.
    4. Mark the complete AOI only after every required tile succeeds.
    """

    def __init__(
        self,
        database_path: Union[str, Path],
        *,
        slice_height_m: Optional[float] = None,
        fetch_tile: Optional[FetchTile] = None,
        min_half_size_m: float = DEFAULT_ADAPTIVE_MIN_HALF_SIZE_M,
    ) -> None:
        path = Path(database_path).expanduser().resolve()
        with _database_lock(path):
            _initialize_auto_cache_database(path)
        super().__init__(path, slice_height_m=slice_height_m, read_only=False)
        self._network_provider = OSMMapProvider(cache_radius_m=1000.0, slice_height_m=None)
        self._fetch_tile: FetchTile = fetch_tile or self._network_provider._fetch_combined_osm_tile
        # Retained as a constructor compatibility parameter. The fallback layout
        # uses OSM_MAX_TILE_HALF_SIZE_M through _tile_centers_for_square.
        self.min_half_size_m = max(250.0, float(min_half_size_m))
        self.last_acquisition: Dict[str, Any] = {
            "cache_hit": None,
            "logical_overpass_queries": 0,
            "resumed_tiles": 0,
            "inserted_buildings": 0,
            "inserted_landuse": 0,
        }

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        radius = max(1.0, float(radius_m))
        if (
            self._prefetched
            and self._cache_center is not None
            and _distance_m(center, self._cache_center) < 1.0
            and radius <= self._prefetch_radius_m
        ):
            return

        lock = _database_lock(self.database_path)
        with lock:
            cache_hit = self._aoi_is_cached(center, radius)
            self.last_acquisition = {
                "cache_hit": cache_hit,
                "logical_overpass_queries": 0,
                "resumed_tiles": 0,
                "inserted_buildings": 0,
                "inserted_landuse": 0,
            }
            if cache_hit:
                logger.info(
                    "DVT OSM AOI cache hit: center=(%.6f, %.6f) radius=%.0fm database=%s",
                    center.lat,
                    center.lon,
                    radius,
                    self.database_path,
                )
            else:
                logger.info(
                    "DVT OSM AOI cache miss: acquiring center=(%.6f, %.6f) radius=%.0fm",
                    center.lat,
                    center.lon,
                    radius,
                )
                self._acquire_aoi(center, radius)

        super().prefetch_all_data(center, radius)

    def _aoi_is_cached(self, center: LatLon, radius_m: float) -> bool:
        min_lon, min_lat, max_lon, max_lat = _bbox_values(center, radius_m)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT id
                FROM aoi_cache
                WHERE min_lon <= ?
                  AND min_lat <= ?
                  AND max_lon >= ?
                  AND max_lat >= ?
                ORDER BY ((max_lon - min_lon) * (max_lat - min_lat)) ASC
                LIMIT 1
                """,
                (min_lon, min_lat, max_lon, max_lat),
            ).fetchone()
        return row is not None

    def _tile_is_cached(self, center: LatLon, half_size_m: float) -> bool:
        key = _tile_key(center, half_size_m)
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT tile_key FROM osm_fetch_tiles WHERE tile_key=?",
                (key,),
            ).fetchone()
        return row is not None

    def _acquire_aoi(self, center: LatLon, radius_m: float) -> None:
        try:
            full_result = self._acquire_tile(center, radius_m)
            if full_result is None:
                building_total = 0
                landuse_total = 0
                tiles = _tile_centers_for_square(center, radius_m)
                logger.info(
                    "Full DVT OSM AOI request failed; falling back to %d persistent tiles",
                    len(tiles),
                )
                for tile_center, half_size_m in tiles:
                    result = self._acquire_tile(tile_center, half_size_m)
                    if result is None:
                        raise LocalOSMAcquisitionError(
                            "Automatic OSM acquisition failed for fallback tile "
                            f"bbox={_bbox_around_point(tile_center.lat, tile_center.lon, half_size_m)}"
                        )
                    b_count, l_count = result
                    building_total += b_count
                    landuse_total += l_count
                buildings, landuse = building_total, landuse_total
            else:
                buildings, landuse = full_result
        except LocalOSMAcquisitionError:
            raise
        except Exception as exc:
            raise LocalOSMAcquisitionError(
                f"Automatic OSM acquisition failed for {radius_m:.0f} m around "
                f"({center.lat:.6f}, {center.lon:.6f}): {exc}"
            ) from exc

        min_lon, min_lat, max_lon, max_lat = _bbox_values(center, radius_m)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO aoi_cache(
                    center_lat, center_lon, radius_m,
                    min_lon, min_lat, max_lon, max_lat,
                    acquired_at, source, building_count, landuse_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    center.lat,
                    center.lon,
                    radius_m,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    now,
                    "overpass_adaptive",
                    buildings,
                    landuse,
                ),
            )
            conn.execute(
                "INSERT INTO metadata(key, value) VALUES('last_acquired_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (now,),
            )
            conn.commit()
        logger.info(
            "DVT OSM AOI acquisition complete: logical_overpass_queries=%d resumed_tiles=%d "
            "inserted_buildings=%d inserted_landuse=%d",
            self.last_acquisition["logical_overpass_queries"],
            self.last_acquisition["resumed_tiles"],
            self.last_acquisition["inserted_buildings"],
            self.last_acquisition["inserted_landuse"],
        )

    def _acquire_tile(
        self, center: LatLon, half_size_m: float
    ) -> Optional[Tuple[int, int]]:
        if self._tile_is_cached(center, half_size_m):
            self.last_acquisition["resumed_tiles"] += 1
            return 0, 0

        self.last_acquisition["logical_overpass_queries"] += 1
        fetched = self._fetch_tile(center, half_size_m)
        if fetched is None:
            return None
        buildings, landuse = fetched
        inserted_buildings, inserted_landuse = self._commit_tile(
            center,
            half_size_m,
            buildings,
            landuse,
        )
        self.last_acquisition["inserted_buildings"] += inserted_buildings
        self.last_acquisition["inserted_landuse"] += inserted_landuse
        return inserted_buildings, inserted_landuse

    def _commit_tile(
        self,
        center: LatLon,
        half_size_m: float,
        buildings: Sequence[dict],
        landuse: Sequence[dict],
    ) -> Tuple[int, int]:
        key = _tile_key(center, half_size_m)
        min_lon, min_lat, max_lon, max_lat = _bbox_values(center, half_size_m)
        now = datetime.now(timezone.utc).isoformat()
        inserted = {"building": 0, "landuse": 0}

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM osm_fetch_tiles WHERE tile_key=?", (key,)
            ).fetchone():
                conn.rollback()
                self.last_acquisition["resumed_tiles"] += 1
                return 0, 0

            for kind, features in (("building", buildings), ("landuse", landuse)):
                for feature in features:
                    geometry = feature.get("geometry") or []
                    bbox = _feature_bbox(geometry)
                    if bbox is None:
                        continue
                    feature_key = _feature_key(feature, kind)
                    source_id = str(feature.get("osm_id") or feature.get("id") or "unknown")
                    source_type = str(feature.get("osm_type") or "osm")
                    geometry_json = json.dumps(geometry, separators=(",", ":"))
                    keyed = conn.execute(
                        "SELECT feature_id FROM feature_keys WHERE feature_key=?",
                        (feature_key,),
                    ).fetchone()
                    if keyed:
                        continue
                    # Schema-v1/offline-seeded databases did not have feature_keys.
                    # Match the stable OSM identity plus exact unsimplified geometry
                    # before inserting, then backfill the key for later requests.
                    existing = conn.execute(
                        """
                        SELECT id FROM features
                        WHERE source_type=? AND source_id=? AND kind=? AND geometry_json=?
                        LIMIT 1
                        """,
                        (source_type, source_id, kind, geometry_json),
                    ).fetchone()
                    if existing:
                        conn.execute(
                            "INSERT OR IGNORE INTO feature_keys(feature_key, feature_id) VALUES (?, ?)",
                            (feature_key, int(existing[0])),
                        )
                        continue

                    tags = dict(feature.get("tags") or {})
                    material = feature.get("material") if kind == "building" else None
                    height_m = feature.get("height_m") if kind == "building" else None
                    feature_min_lon, feature_min_lat, feature_max_lon, feature_max_lat = bbox
                    cursor = conn.execute(
                        """
                        INSERT INTO features(
                            source_id, source_type, kind,
                            tags_json, geometry_json, material, height_m,
                            min_lon, min_lat, max_lon, max_lat
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            source_type,
                            kind,
                            json.dumps(tags, separators=(",", ":"), sort_keys=True),
                            geometry_json,
                            material,
                            float(height_m) if height_m is not None else None,
                            feature_min_lon,
                            feature_min_lat,
                            feature_max_lon,
                            feature_max_lat,
                        ),
                    )
                    feature_id = int(cursor.lastrowid)
                    conn.execute(
                        "INSERT INTO feature_rtree VALUES (?, ?, ?, ?, ?)",
                        (
                            feature_id,
                            feature_min_lon,
                            feature_max_lon,
                            feature_min_lat,
                            feature_max_lat,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO feature_keys(feature_key, feature_id) VALUES (?, ?)",
                        (feature_key, feature_id),
                    )
                    inserted[kind] += 1

            conn.execute(
                """
                INSERT INTO osm_fetch_tiles(
                    tile_key, center_lat, center_lon, half_size_m,
                    min_lon, min_lat, max_lon, max_lat,
                    acquired_at, building_count, landuse_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    center.lat,
                    center.lon,
                    half_size_m,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    now,
                    inserted["building"],
                    inserted["landuse"],
                ),
            )
            conn.commit()

        return inserted["building"], inserted["landuse"]

    @property
    def acquisition_metadata(self) -> Dict[str, Any]:
        return dict(self.last_acquisition)
