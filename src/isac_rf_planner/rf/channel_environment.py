"""Environment-aware reciprocal return-path lookup for bistatic analysis.

The forward transmitter-to-target path is already evaluated by the main planner.
For the target-to-analysis-receiver leg, this module builds a second polar world
model centered on the analysis receiver using the same OSM/terrain providers and
sampling resolution.  The result is a compact lookup of non-free-space excess
loss that can be sampled for every candidate target point.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np

from ..geo.google_mesh.utils import bearing_deg, haversine_m
from ..pipeline.schemas import LatLon, RFParams, WorldModel



PROPAGATION_MODE_CODES = {
    "unavailable": 0,
    "unknown": 0,
    "los": 1,
    "reflect": 2,
    "penetration": 3,
    "nlos_recovery": 4,
    "shadow": 5,
    "diffraction": 6,
    "terrain_diffraction": 7,
}
PROPAGATION_MODE_LEGEND = {
    0: "unavailable_or_unknown",
    1: "los",
    2: "reflect",
    3: "penetration",
    4: "nlos_recovery",
    5: "shadow",
    6: "diffraction",
    7: "terrain_diffraction",
}

@dataclass
class ReturnPathEnvironmentLookup:
    """Polar excess-loss lookup centered on one analysis receiver."""

    receiver: LatLon
    max_range_m: float
    dr_m: float
    dtheta_deg: float
    excess_loss_db: np.ndarray
    valid: np.ndarray
    penetration_loss_db: np.ndarray | None = None
    shadow_loss_db: np.ndarray | None = None
    diffraction_loss_db: np.ndarray | None = None
    terrain_loss_db: np.ndarray | None = None
    canyon_recovery_db: np.ndarray | None = None
    propagation_mode_code: np.ndarray | None = None
    source_model: str = "environmental_reciprocal_polar_grid"
    target_aoi_center: LatLon | None = None
    target_aoi_radius_m: float | None = None

    def sample(self, latitude: float, longitude: float) -> tuple[float, float, float, bool]:
        """Return ``(excess_loss_db, range_m, bearing_deg, valid)``."""

        point = LatLon(lat=float(latitude), lon=float(longitude))
        distance_m = float(haversine_m(self.receiver, point))
        azimuth_deg = float(bearing_deg(self.receiver, point)) % 360.0
        if distance_m > float(self.max_range_m) + 0.5 * float(self.dr_m):
            return 0.0, distance_m, azimuth_deg, False

        bearing_index = int(round(azimuth_deg / self.dtheta_deg)) % self.excess_loss_db.shape[0]
        range_index = int(round(distance_m / self.dr_m))
        range_index = max(0, min(range_index, self.excess_loss_db.shape[1] - 1))
        if bool(self.valid[bearing_index, range_index]):
            return (
                float(self.excess_loss_db[bearing_index, range_index]),
                distance_m,
                azimuth_deg,
                True,
            )

        # A terminated or sparse ray may leave one bin empty. Search only the
        # immediate neighborhood so the stated resolution remains meaningful.
        best: tuple[int, int] | None = None
        best_cost = 10**9
        for db in (-1, 0, 1):
            bi = (bearing_index + db) % self.excess_loss_db.shape[0]
            for dr in (-2, -1, 0, 1, 2):
                ri = range_index + dr
                if ri < 0 or ri >= self.excess_loss_db.shape[1]:
                    continue
                if not bool(self.valid[bi, ri]):
                    continue
                cost = abs(db) * 3 + abs(dr)
                if cost < best_cost:
                    best = (bi, ri)
                    best_cost = cost
        if best is None:
            return 0.0, distance_m, azimuth_deg, False
        return float(self.excess_loss_db[best]), distance_m, azimuth_deg, True

    def _sample_indices_many(
        self,
        latitudes: Any,
        longitudes: Any,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return range, bearing, selected polar indices, and validity."""

        lat = np.asarray(latitudes, dtype=np.float64)
        lon = np.asarray(longitudes, dtype=np.float64)
        if lat.shape != lon.shape:
            raise ValueError("latitude and longitude arrays must have matching shapes")

        lat1 = math.radians(float(self.receiver.lat))
        lon1 = math.radians(float(self.receiver.lon))
        lat2 = np.deg2rad(lat)
        lon2 = np.deg2rad(lon)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        h = (
            np.sin(dlat / 2.0) ** 2
            + math.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2.0) ** 2
        )
        h = np.clip(h, 0.0, 1.0)
        distance_m = 6_371_000.0 * 2.0 * np.arctan2(np.sqrt(h), np.sqrt(1.0 - h))

        y = np.sin(dlon) * np.cos(lat2)
        x = math.cos(lat1) * np.sin(lat2) - math.sin(lat1) * np.cos(lat2) * np.cos(dlon)
        azimuth_deg = np.mod(np.rad2deg(np.arctan2(y, x)) + 360.0, 360.0)

        n_bearings, n_ranges = self.excess_loss_db.shape
        selected_bi = np.rint(azimuth_deg / float(self.dtheta_deg)).astype(np.int64) % n_bearings
        selected_ri = np.rint(distance_m / float(self.dr_m)).astype(np.int64)
        selected_ri = np.clip(selected_ri, 0, n_ranges - 1)
        in_range = distance_m <= float(self.max_range_m) + 0.5 * float(self.dr_m)
        valid = in_range & self.valid[selected_bi, selected_ri]

        missing_flat = np.flatnonzero((in_range & ~valid).ravel())
        if missing_flat.size:
            bi0 = selected_bi.ravel()[missing_flat]
            ri0 = selected_ri.ravel()[missing_flat]
            best_cost = np.full(missing_flat.size, 32767, dtype=np.int16)
            best_bi = bi0.copy()
            best_ri = ri0.copy()
            found = np.zeros(missing_flat.size, dtype=np.bool_)
            for db in (-1, 0, 1):
                bi = (bi0 + db) % n_bearings
                for dr in (-2, -1, 0, 1, 2):
                    ri = ri0 + dr
                    in_bounds = (ri >= 0) & (ri < n_ranges)
                    if not np.any(in_bounds):
                        continue
                    candidate = np.zeros_like(found)
                    candidate[in_bounds] = self.valid[bi[in_bounds], ri[in_bounds]]
                    cost = abs(db) * 3 + abs(dr)
                    update = candidate & (cost < best_cost)
                    if np.any(update):
                        best_cost[update] = cost
                        best_bi[update] = bi[update]
                        best_ri[update] = ri[update]
                        found[update] = True
            if np.any(found):
                target_flat = missing_flat[found]
                selected_bi.ravel()[target_flat] = best_bi[found]
                selected_ri.ravel()[target_flat] = best_ri[found]
                valid.ravel()[target_flat] = True

        return distance_m, azimuth_deg, selected_bi, selected_ri, valid

    def sample_many(
        self,
        latitudes: Any,
        longitudes: Any,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Vectorized aggregate excess-loss lookup for a complete target grid."""

        distance_m, azimuth_deg, bi, ri, valid = self._sample_indices_many(
            latitudes, longitudes
        )
        excess = np.zeros(np.asarray(distance_m).shape, dtype=np.float32)
        if np.any(valid):
            excess[valid] = self.excess_loss_db[bi[valid], ri[valid]]
        return excess, distance_m, azimuth_deg, valid

    def sample_components_many(
        self,
        latitudes: Any,
        longitudes: Any,
    ) -> dict[str, np.ndarray]:
        """Return aggregate and decomposed point-aligned environment losses."""

        distance_m, azimuth_deg, bi, ri, valid = self._sample_indices_many(
            latitudes, longitudes
        )
        shape = np.asarray(distance_m).shape

        def numeric(source: np.ndarray | None) -> np.ndarray:
            out = np.zeros(shape, dtype=np.float32)
            if source is not None and np.any(valid):
                out[valid] = source[bi[valid], ri[valid]]
            return out

        mode_code = np.zeros(shape, dtype=np.uint8)
        if self.propagation_mode_code is not None and np.any(valid):
            mode_code[valid] = self.propagation_mode_code[bi[valid], ri[valid]]

        return {
            "excess_loss_db": numeric(self.excess_loss_db),
            "penetration_loss_db": numeric(self.penetration_loss_db),
            "shadow_loss_db": numeric(self.shadow_loss_db),
            "diffraction_loss_db": numeric(self.diffraction_loss_db),
            "terrain_loss_db": numeric(self.terrain_loss_db),
            "canyon_recovery_db": numeric(self.canyon_recovery_db),
            "propagation_mode_code": mode_code,
            "distance_m": np.asarray(distance_m, dtype=np.float64),
            "azimuth_deg": np.asarray(azimuth_deg, dtype=np.float64),
            "valid": np.asarray(valid, dtype=np.bool_),
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "model": self.source_model,
            "receiver": {"latitude": self.receiver.lat, "longitude": self.receiver.lon},
            "max_range_m": float(self.max_range_m),
            "radial_resolution_m": float(self.dr_m),
            "bearing_resolution_deg": float(self.dtheta_deg),
            "valid_samples": int(np.count_nonzero(self.valid)),
            "total_samples": int(self.valid.size),
            "propagation_mode_legend": {
                str(code): label for code, label in PROPAGATION_MODE_LEGEND.items()
            },
            "target_aoi_clip": (
                {
                    "center": {
                        "latitude": self.target_aoi_center.lat,
                        "longitude": self.target_aoi_center.lon,
                    },
                    "radius_m": float(self.target_aoi_radius_m),
                }
                if self.target_aoi_center is not None and self.target_aoi_radius_m is not None
                else None
            ),
        }


def cell_environment_excess_loss_db(cell: Any) -> float:
    """Return the non-free-space loss represented by a world-model cell."""

    penetration = float(getattr(cell, "penetration_loss_db", 0.0) or 0.0)
    shadow = float(getattr(cell, "shadow_loss_db", 0.0) or 0.0)
    diffraction = float(getattr(cell, "diffraction_loss_db", 0.0) or 0.0)
    terrain = float(getattr(cell, "terrain_loss_db", 0.0) or 0.0)
    canyon = float(getattr(cell, "canyon_recovery_db", 0.0) or 0.0)
    if penetration == shadow == diffraction == terrain == canyon == 0.0:
        return max(0.0, float(getattr(cell, "extra_loss_db", 0.0) or 0.0))
    return max(0.0, penetration + shadow + diffraction + terrain - canyon)


def lookup_from_world(
    world: WorldModel,
    *,
    receiver: LatLon,
    max_range_m: float,
    dr_m: float,
    dtheta_deg: float,
    source_model: str = "environmental_reciprocal_polar_grid",
) -> ReturnPathEnvironmentLookup:
    """Convert a receiver-centered world model into a compact polar lookup."""

    n_bearings = max(1, int(math.ceil(360.0 / float(dtheta_deg))))
    n_ranges = max(2, int(math.ceil(float(max_range_m) / float(dr_m))) + 1)
    shape = (n_bearings, n_ranges)
    excess = np.zeros(shape, dtype=np.float32)
    penetration = np.zeros(shape, dtype=np.float32)
    shadow = np.zeros(shape, dtype=np.float32)
    diffraction = np.zeros(shape, dtype=np.float32)
    terrain = np.zeros(shape, dtype=np.float32)
    canyon = np.zeros(shape, dtype=np.float32)
    propagation_mode_code = np.zeros(shape, dtype=np.uint8)
    valid = np.zeros(shape, dtype=np.bool_)

    for cell in world.cells:
        bi = int(round((float(cell.bearing_deg) % 360.0) / float(dtheta_deg))) % n_bearings
        ri = int(round(float(cell.distance_m) / float(dr_m)))
        if ri < 0 or ri >= n_ranges:
            continue
        excess[bi, ri] = np.float32(cell_environment_excess_loss_db(cell))
        penetration[bi, ri] = np.float32(float(getattr(cell, "penetration_loss_db", 0.0) or 0.0))
        shadow[bi, ri] = np.float32(float(getattr(cell, "shadow_loss_db", 0.0) or 0.0))
        diffraction[bi, ri] = np.float32(float(getattr(cell, "diffraction_loss_db", 0.0) or 0.0))
        terrain[bi, ri] = np.float32(float(getattr(cell, "terrain_loss_db", 0.0) or 0.0))
        canyon[bi, ri] = np.float32(float(getattr(cell, "canyon_recovery_db", 0.0) or 0.0))
        mode = str(getattr(cell, "propagation_mode", "unknown") or "unknown").lower()
        propagation_mode_code[bi, ri] = np.uint8(PROPAGATION_MODE_CODES.get(mode, 0))
        valid[bi, ri] = True

    # The origin has zero environmental excess loss on every ray.
    valid[:, 0] = True
    excess[:, 0] = 0.0
    clip_lat = getattr(world.rf_params, "coverage_clip_center_lat", None)
    clip_lon = getattr(world.rf_params, "coverage_clip_center_lon", None)
    clip_radius = getattr(world.rf_params, "coverage_clip_radius_m", None)
    return ReturnPathEnvironmentLookup(
        receiver=receiver,
        max_range_m=float(max_range_m),
        dr_m=float(dr_m),
        dtheta_deg=float(dtheta_deg),
        excess_loss_db=excess,
        valid=valid,
        penetration_loss_db=penetration,
        shadow_loss_db=shadow,
        diffraction_loss_db=diffraction,
        terrain_loss_db=terrain,
        canyon_recovery_db=canyon,
        propagation_mode_code=propagation_mode_code,
        source_model=str(source_model),
        target_aoi_center=(
            LatLon(lat=float(clip_lat), lon=float(clip_lon))
            if clip_lat is not None and clip_lon is not None
            else None
        ),
        target_aoi_radius_m=(float(clip_radius) if clip_radius is not None else None),
    )


def return_path_rf_params(
    rf_params: RFParams,
    *,
    receiver_site_altitude_m: float,
    receiver_antenna_height_m: float,
    target_height_m: float,
    max_range_m: float,
    target_aoi_center: LatLon,
    target_aoi_radius_m: float,
    prefetch_center: LatLon,
    prefetch_radius_m: float,
) -> RFParams:
    """Clone RF settings for geometry-only reciprocal-path world generation."""

    clone = rf_params.model_copy(deep=True)
    clone.channel_analysis = None
    clone.site_altitude_m = float(receiver_site_altitude_m)
    clone.tx_height_m = float(receiver_antenna_height_m)
    clone.rx_height_m = float(target_height_m)
    clone.max_range_m = float(max_range_m)
    clone.coverage_clip_center_lat = float(target_aoi_center.lat)
    clone.coverage_clip_center_lon = float(target_aoi_center.lon)
    clone.coverage_clip_radius_m = float(target_aoi_radius_m)
    clone.prefetch_center_lat = float(prefetch_center.lat)
    clone.prefetch_center_lon = float(prefetch_center.lon)
    clone.prefetch_radius_m = float(prefetch_radius_m)
    clone.termination_rsrp_dbm = -300.0
    clone.compact_output = False
    return clone
