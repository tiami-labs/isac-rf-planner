"""MapProvider implementation backed by persisted Google-mesh ray profiles.

Design constraints:
  - This provider answers the *same* queries used by the 2D pipeline
    (get_buildings_along_ray / is_forest_between / count_buildings_between).
  - It does not implement RF math; it only supplies geometry/obstacle facts.
  - Mesh intersection work is assumed to be performed elsewhere (e.g., a Cesium
    frontend), and the results are persisted via MeshProfileStore.

If profiles are missing for a TX/config, this provider raises a ValueError with
a deterministic cache key that should be generated and uploaded.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..osm_map_provider import OSMMapProvider
from ..physical_spanning import MapProvider
from ...pipeline.schemas import LatLon

from .profile_store_sqlite import MeshProfileStore
from .profile_types import RayProfileSet, PROFILE_VERSION
from .utils import bearing_bin, bearing_deg, haversine_m

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MissingMeshProfiles(Exception):
    """Raised when a required mesh profile set is not present on disk."""

    key: str
    message: str

    def __str__(self) -> str:
        return f"{self.message} (key={self.key})"


class GoogleMeshOSMMapProvider(MapProvider):
    """3D ray propagation provider using persisted mesh occlusion segments.

    It can optionally wrap an OSMMapProvider for clutter classification and
    optional OSM prefetch.
    """

    def __init__(
        self,
        profile_store: Optional[MeshProfileStore] = None,
        osm_provider: Optional[OSMMapProvider] = None,
        *,
        tx_height_m: float = 0.0,
        rx_height_m: float = 1.5,
        max_range_m: float = 2000.0,
        dr_m: float = 5.0,
        dtheta_deg: float = 5.0,
        version: str = PROFILE_VERSION,
    ):
        self.store = profile_store or MeshProfileStore()
        self.osm = osm_provider
        self.tx_height_m = float(tx_height_m)
        self.rx_height_m = float(rx_height_m)
        self.max_range_m = float(max_range_m)
        self.dr_m = float(dr_m)
        self.dtheta_deg = float(dtheta_deg)
        self.version = version

        self._tx: Optional[LatLon] = None
        self._profiles_by_bin: Dict[int, List[Dict[str, Any]]] = {}
        self._tree_segments_by_bin: Dict[int, List[Dict[str, Any]]] = {}
        self._ready: bool = False
        self._key: Optional[str] = None

    @property
    def key(self) -> Optional[str]:
        """The current loaded profile key (hash), if ready."""
        return self._key

    def _require_ready(self) -> None:
        if not self._ready:
            msg = "Mesh ray profiles not loaded. Call prefetch_all_data(...) or ensure profiles exist."
            raise ValueError(msg)

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        """Load mesh profiles from disk and optionally prefetch OSM."""
        # Keep OSM prefetch separate; it may be useful for clutter.
        if self.osm is not None:
            try:
                # Ensure OSM data is cached; use the radius that the caller requested.
                self.osm.prefetch_all_data(center, radius_m)
            except Exception as e:
                logger.warning(f"OSM prefetch failed (non-critical for mesh provider): {e}")

        prof = self.store.get(
            tx=center,
            tx_height_m=self.tx_height_m,
            rx_height_m=self.rx_height_m,
            max_range_m=self.max_range_m,
            dr_m=self.dr_m,
            dtheta_deg=self.dtheta_deg,
            version=self.version,
        )

        ok, key_hash = self.store.has(
            tx=center,
            tx_height_m=self.tx_height_m,
            rx_height_m=self.rx_height_m,
            max_range_m=self.max_range_m,
            dr_m=self.dr_m,
            dtheta_deg=self.dtheta_deg,
            version=self.version,
        )
        self._key = key_hash

        if prof is None:
            self._ready = False
            raise MissingMeshProfiles(
                key=key_hash,
                message="Missing persisted mesh ray profiles for this TX/config",
            )

        self._load_profiles(center, prof)

    def _load_profiles(self, tx: LatLon, profile_set: RayProfileSet) -> None:
        self._tx = tx
        self._profiles_by_bin = {}
        self._tree_segments_by_bin = {}

        # Pre-expand into simple lists of obstacle dicts per bearing bin for fast queries.
        for bp in profile_set.profiles:
            b = bearing_bin(bp.bearing_deg, profile_set.dtheta_deg)
            obstacles: List[Dict[str, Any]] = []
            tree_segments: List[Dict[str, Any]] = []

            # Sort by distance to keep deterministic ordering.
            segments = sorted(bp.segments, key=lambda s: (s.r0_m, s.r1_m))
            for seg in segments:
                kind = (seg.kind or "unknown").lower()
                material = seg.material or "unknown"

                if kind in ("trees", "tree", "forest", "wood"):
                    tree_segments.append(
                        {
                            "r0_m": float(seg.r0_m),
                            "r1_m": float(seg.r1_m),
                            "kind": "trees",
                            "material": material,
                        }
                    )
                    continue

                # Treat any non-tree segment as a "building-like" obstacle.
                obstacles.append(
                    {
                        "kind": kind,
                        "material": material,
                        "r0_m": float(seg.r0_m),
                        "r1_m": float(seg.r1_m),
                        "osm_id": seg.osm_id,
                        "osm_tags": seg.osm_tags,
                    }
                )

            self._profiles_by_bin[b] = obstacles
            self._tree_segments_by_bin[b] = tree_segments

        self._ready = True
        logger.info(
            f"Loaded mesh profiles: {len(self._profiles_by_bin)} bearing bins (key={self._key})"
        )

    def get_buildings_along_ray(self, start: LatLon, end: LatLon) -> List[Dict[str, Any]]:
        self._require_ready()
        assert self._tx is not None

        brg = bearing_deg(start, end)
        b = bearing_bin(brg, self.dtheta_deg)
        r_end = haversine_m(start, end)

        obstacles = self._profiles_by_bin.get(b, [])
        if not obstacles:
            return []

        # Return obstacles whose segment starts before the endpoint.
        # Deduplicate by osm_id if provided, otherwise keep all segments.
        result: List[Dict[str, Any]] = []
        seen_ids = set()
        for obs in obstacles:
            if obs.get("r0_m", 0.0) <= r_end:
                oid = obs.get("osm_id")
                if oid is not None:
                    if oid in seen_ids:
                        continue
                    seen_ids.add(oid)
                result.append(obs)
        return result

    def count_buildings_between(self, start: LatLon, end: LatLon) -> int:
        return len(self.get_buildings_along_ray(start, end))

    def is_forest_between(self, start: LatLon, end: LatLon) -> bool:
        self._require_ready()
        brg = bearing_deg(start, end)
        b = bearing_bin(brg, self.dtheta_deg)
        r_end = haversine_m(start, end)
        for seg in self._tree_segments_by_bin.get(b, []):
            if seg.get("r0_m", 0.0) <= r_end:
                return True
        return False

    def get_clutter_type(self, center: LatLon, radius_m: float = 200.0) -> str:
        if self.osm is None:
            return "unknown"
        return self.osm.get_clutter_type(center, radius_m=radius_m)
