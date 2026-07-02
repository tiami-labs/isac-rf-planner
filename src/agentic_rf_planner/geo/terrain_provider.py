"""TerrainProvider — ordered fallback chain over TerrainHeightSource implementations.

The provider itself contains no terrain logic.  It delegates every query to
the first source in its chain that reports available(), then falls through to
the next source if the preferred one is unavailable.

Factory functions build the chain for the two standard configurations:

  create_terrain_provider(rf_params)
      Standard chain: Copernicus raster → OpenTopoData → Flat.
      Drop-in replacement for the previous monolithic TerrainProvider.

  create_terrain_provider_with_mesh(rf_params, profile_set)
      Extended chain: Google Mesh → Copernicus → Flat.
      Used when a RayProfileSet with terrain_heights is available; gives
      sub-5 m terrain resolution instead of 30 m Copernicus.

Both factories return Optional[TerrainProvider]; None means terrain is
disabled (terrain_enabled=False in rf_params).

External callers depend only on TerrainProvider's public methods:
  elevation_m, profile_along_bearing, prefetch_for_polar_grid,
  ensure_raster_window, provider_used.
Nothing else in the codebase should import a concrete source class.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from ..pipeline.schemas import LatLon, RFParams
from .dem_cache import ElevationCacheStore
from .google_mesh.profile_types import RayProfileSet
from .terrain_source import TerrainHeightSource
from .terrain_sources import (
    CopernicusTerrainSource,
    FlatTerrainSource,
    GoogleElevationTerrainSource,
    GoogleMeshTerrainSource,
)

logger = logging.getLogger(__name__)

# Re-export for any callers that imported this constant from terrain_provider.
from .dem_cache import DATASET_LOCAL_RASTER  # noqa: F401


class TerrainProvider:
    """Ordered fallback chain over TerrainHeightSource implementations.

    Instantiate via the factory functions below rather than directly.
    """

    def __init__(self, sources: List[TerrainHeightSource]) -> None:
        if not sources:
            raise ValueError("TerrainProvider requires at least one source")
        self._sources = sources
        self._last_used: str = "none"

    # ------------------------------------------------------------------
    # Public API (unchanged from the previous monolithic implementation)
    # ------------------------------------------------------------------

    @property
    def provider_used(self) -> str:
        """Name of the source that last served a query."""
        return self._last_used

    def elevation_m(self, lat: float, lon: float) -> float:
        src = self._first_available()
        self._last_used = src.name
        return src.elevation_m(lat, lon)

    def profile_along_bearing(
        self,
        tx: LatLon,
        bearing_deg: float,
        max_range_m: float,
        step_m: float,
    ) -> Tuple[List[float], List[float]]:
        src = self._first_available()
        self._last_used = src.name
        return src.profile_along_bearing(tx, bearing_deg, max_range_m, step_m)

    def prefetch_for_polar_grid(
        self,
        tx: LatLon,
        max_range_m: float,
        step_m: float,
        dtheta_deg: float,
    ) -> None:
        """Warm the active source's cache for all coverage-grid samples."""
        src = self._first_available()
        self._last_used = src.name
        src.prefetch(tx, max_range_m, step_m, dtheta_deg)

    def ensure_raster_window(self, lat: float, lon: float, radius_m: float) -> bool:
        """Load the Copernicus raster window if it is in the chain.

        Returns False when no Copernicus source is present (e.g. mesh-only chain).
        """
        for src in self._sources:
            if isinstance(src, CopernicusTerrainSource):
                return src.ensure_raster_window(lat, lon, radius_m)
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _first_available(self) -> TerrainHeightSource:
        for src in self._sources:
            if src.available():
                return src
        # FlatTerrainSource is always last and always available; this is unreachable
        # in practice but satisfies the type checker.
        raise RuntimeError("No TerrainHeightSource is available — chain is empty or all unavailable")


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def create_terrain_provider(
    rf_params: RFParams,
    elevation_store: Optional[ElevationCacheStore] = None,
) -> Optional["TerrainProvider"]:
    """Standard chain: Copernicus raster → Flat.

    Drop-in replacement for the previous create_terrain_provider.  Returns
    None when terrain_enabled is False.
    """
    if not bool(getattr(rf_params, "terrain_enabled", True)):
        return None

    max_r = getattr(rf_params, "max_range_m", None)

    copernicus = CopernicusTerrainSource(
        elevation_store=elevation_store,
        max_range_m=float(max_r) if max_r is not None else None,
    )

    sources: List[TerrainHeightSource] = [copernicus, FlatTerrainSource()]

    # Honour explicit dem_source=google / google_elevation if set.
    dem_source = str(getattr(rf_params, "dem_source", "") or "").strip().lower()
    if dem_source in ("google", "google_elevation"):
        google_elev = GoogleElevationTerrainSource(elevation_store=elevation_store)
        if google_elev.available():
            sources = [google_elev, copernicus, FlatTerrainSource()]
        else:
            logger.warning(
                "dem_source=google requested but GOOGLE_MAPS_API_KEY not set; "
                "falling back to Copernicus"
            )

    provider = TerrainProvider(sources=sources)
    logger.info(
        "TerrainProvider created: chain=[%s]",
        ", ".join(s.name for s in sources),
    )
    return provider


def create_terrain_provider_with_mesh(
    rf_params: RFParams,
    profile_set: RayProfileSet,
    elevation_store: Optional[ElevationCacheStore] = None,
) -> Optional["TerrainProvider"]:
    """Extended chain: Google Mesh → Copernicus → Flat.

    Used when a RayProfileSet with terrain_heights is available.  The mesh
    source provides sub-5 m resolution; Copernicus is the automatic fallback
    for bearings or areas where mesh profiles are absent or not yet populated.
    Returns None when terrain_enabled is False.
    """
    if not bool(getattr(rf_params, "terrain_enabled", True)):
        return None

    max_r = getattr(rf_params, "max_range_m", None)

    mesh = GoogleMeshTerrainSource(profile_set)
    copernicus = CopernicusTerrainSource(
        elevation_store=elevation_store,
        max_range_m=float(max_r) if max_r is not None else None,
    )
    sources: List[TerrainHeightSource] = [mesh, copernicus, FlatTerrainSource()]

    provider = TerrainProvider(sources=sources)
    logger.info(
        "TerrainProvider (with mesh) created: chain=[%s], mesh_profiles_with_heights=%d",
        ", ".join(s.name for s in sources),
        sum(1 for p in profile_set.profiles if p.terrain_heights),
    )
    return provider
