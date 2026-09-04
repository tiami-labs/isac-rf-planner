"""SQLite-backed persistence for Google-mesh ray profiles.

This store exists to ensure 3D ray/mesh intersection results are computed once
and then reused for near-instant subsequent planning calls.

The store does NOT perform mesh intersection itself. It only persists and
retrieves RayProfileSets.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ...pipeline.schemas import LatLon
from .profile_types import BearingProfile, RayProfileSet, PROFILE_VERSION
from .utils import compute_profile_key

logger = logging.getLogger(__name__)


DEFAULT_CACHE_DIR = Path.home() / ".rf_planning_cache" / "google_mesh_profiles"
DEFAULT_DB_PATH = DEFAULT_CACHE_DIR / "mesh_profiles.sqlite"
DEFAULT_EXPIRY_DAYS = 30


class MeshProfileStore:
    """SQLite-backed store for mesh ray profiles."""

    def __init__(self, db_path: Optional[Path] = None, expiry_days: int = DEFAULT_EXPIRY_DAYS):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.expiry_days = int(expiry_days)
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
                CREATE TABLE IF NOT EXISTS profile_meta (
                    key TEXT PRIMARY KEY,
                    key_str TEXT NOT NULL,
                    tx_lat REAL NOT NULL,
                    tx_lon REAL NOT NULL,
                    tx_height_m REAL NOT NULL,
                    rx_height_m REAL NOT NULL,
                    max_range_m REAL NOT NULL,
                    dr_m REAL NOT NULL,
                    dtheta_deg REAL NOT NULL,
                    version TEXT NOT NULL,
                    created_at_ms INTEGER,
                    updated_at_ms INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bearing_profiles (
                    key TEXT NOT NULL,
                    bearing_bin INTEGER NOT NULL,
                    bearing_deg REAL NOT NULL,
                    json_blob TEXT NOT NULL,
                    PRIMARY KEY (key, bearing_bin)
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_meta_updated ON profile_meta(updated_at_ms);")

    def compute_key(
        self,
        tx: LatLon,
        tx_height_m: float,
        rx_height_m: float,
        max_range_m: float,
        dr_m: float,
        dtheta_deg: float,
        version: Optional[str] = None,
    ) -> Tuple[str, str]:
        key_obj, key_hash = compute_profile_key(
            tx=tx,
            tx_height_m=tx_height_m,
            rx_height_m=rx_height_m,
            max_range_m=max_range_m,
            dr_m=dr_m,
            dtheta_deg=dtheta_deg,
            version=version or PROFILE_VERSION,
        )
        return key_hash, key_obj.to_string()

    def put(self, profile_set: RayProfileSet) -> str:
        """Insert or replace a RayProfileSet. Returns key hash."""
        tx = LatLon(lat=profile_set.tx_lat, lon=profile_set.tx_lon)
        key_hash, key_str = self.compute_key(
            tx=tx,
            tx_height_m=profile_set.tx_height_m,
            rx_height_m=profile_set.rx_height_m,
            max_range_m=profile_set.max_range_m,
            dr_m=profile_set.dr_m,
            dtheta_deg=profile_set.dtheta_deg,
            version=profile_set.version,
        )

        now_ms = int(datetime.utcnow().timestamp() * 1000)
        created_at_ms = profile_set.created_at_ms or now_ms

        with self._connect() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT OR REPLACE INTO profile_meta(
                    key, key_str, tx_lat, tx_lon, tx_height_m, rx_height_m,
                    max_range_m, dr_m, dtheta_deg, version, created_at_ms, updated_at_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    key_hash,
                    key_str,
                    profile_set.tx_lat,
                    profile_set.tx_lon,
                    profile_set.tx_height_m,
                    profile_set.rx_height_m,
                    profile_set.max_range_m,
                    profile_set.dr_m,
                    profile_set.dtheta_deg,
                    profile_set.version,
                    created_at_ms,
                    now_ms,
                ),
            )

            # Replace bearing rows
            for bp in profile_set.profiles:
                bb = int(round(bp.bearing_deg / profile_set.dtheta_deg))
                blob = bp.model_dump_json()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO bearing_profiles(key, bearing_bin, bearing_deg, json_blob)
                    VALUES(?, ?, ?, ?);
                    """,
                    (key_hash, bb, float(bp.bearing_deg), blob),
                )
            conn.execute("COMMIT")

        return key_hash

    def _is_expired(self, updated_at_ms: int) -> bool:
        if self.expiry_days <= 0:
            return False
        updated = datetime.utcfromtimestamp(updated_at_ms / 1000.0)
        return (datetime.utcnow() - updated) > timedelta(days=self.expiry_days)

    def has(
        self,
        tx: LatLon,
        tx_height_m: float,
        rx_height_m: float,
        max_range_m: float,
        dr_m: float,
        dtheta_deg: float,
        version: str,
    ) -> Tuple[bool, str]:
        key_hash, _ = self.compute_key(tx, tx_height_m, rx_height_m, max_range_m, dr_m, dtheta_deg, version)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT updated_at_ms FROM profile_meta WHERE key=?;",
                (key_hash,),
            ).fetchone()
        if row is None:
            return False, key_hash
        if self._is_expired(int(row[0])):
            return False, key_hash
        return True, key_hash

    def get(
        self,
        tx: LatLon,
        tx_height_m: float,
        rx_height_m: float,
        max_range_m: float,
        dr_m: float,
        dtheta_deg: float,
        version: str,
    ) -> Optional[RayProfileSet]:
        """Fetch RayProfileSet for the given params, or None if missing/expired."""
        key_hash, _ = self.compute_key(tx, tx_height_m, rx_height_m, max_range_m, dr_m, dtheta_deg, version)

        with self._connect() as conn:
            meta = conn.execute(
                """
                SELECT tx_lat, tx_lon, tx_height_m, rx_height_m, max_range_m, dr_m, dtheta_deg, version, created_at_ms, updated_at_ms
                FROM profile_meta WHERE key=?;
                """,
                (key_hash,),
            ).fetchone()

            if meta is None:
                return None

            updated_at_ms = int(meta[9])
            if self._is_expired(updated_at_ms):
                return None

            rows = conn.execute(
                "SELECT bearing_deg, json_blob FROM bearing_profiles WHERE key=? ORDER BY bearing_bin ASC;",
                (key_hash,),
            ).fetchall()

        profiles: List[BearingProfile] = []
        for bearing_deg, blob in rows:
            try:
                profiles.append(BearingProfile.model_validate_json(blob))
            except Exception as e:
                logger.warning(f"Failed to parse bearing profile (key={key_hash}, bearing={bearing_deg}): {e}")

        return RayProfileSet(
            tx_lat=float(meta[0]),
            tx_lon=float(meta[1]),
            tx_height_m=float(meta[2]),
            rx_height_m=float(meta[3]),
            max_range_m=float(meta[4]),
            dr_m=float(meta[5]),
            dtheta_deg=float(meta[6]),
            version=str(meta[7]),
            created_at_ms=meta[8],
            profiles=profiles,
        )

    def clear(self, older_than_days: Optional[int] = None) -> int:
        """Delete cached profiles. Returns number of meta rows deleted."""
        with self._connect() as conn:
            if older_than_days is None:
                deleted = conn.execute("DELETE FROM profile_meta;").rowcount
                conn.execute("DELETE FROM bearing_profiles;")
                return int(deleted or 0)

            cutoff = datetime.utcnow() - timedelta(days=int(older_than_days))
            cutoff_ms = int(cutoff.timestamp() * 1000)
            keys = [r[0] for r in conn.execute("SELECT key FROM profile_meta WHERE updated_at_ms < ?;", (cutoff_ms,)).fetchall()]
            if not keys:
                return 0
            for k in keys:
                conn.execute("DELETE FROM bearing_profiles WHERE key=?;", (k,))
                conn.execute("DELETE FROM profile_meta WHERE key=?;", (k,))
            return len(keys)

    def stats(self) -> Dict[str, float]:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM profile_meta;").fetchone()
            count = int(row[0]) if row else 0
        size_mb = self.db_path.stat().st_size / (1024 * 1024) if self.db_path.exists() else 0.0
        return {
            "db_path": str(self.db_path),
            "entries": float(count),
            "size_mb": float(size_mb),
        }
