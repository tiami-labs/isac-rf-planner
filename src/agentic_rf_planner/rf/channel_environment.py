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


@dataclass
class ReturnPathEnvironmentLookup:
    """Polar excess-loss lookup centered on one analysis receiver."""

    receiver: LatLon
    max_range_m: float
    dr_m: float
    dtheta_deg: float
    excess_loss_db: np.ndarray
    valid: np.ndarray
    source_model: str = "environmental_reciprocal_polar_grid"

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

    def sample_many(
        self,
        latitudes: Any,
        longitudes: Any,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Vectorized lookup for a complete target grid.

        Returns ``(excess_loss_db, range_m, bearing_deg, valid)`` arrays.  The
        immediate-neighbour fallback matches :meth:`sample` but is evaluated in
        NumPy batches instead of constructing a ``LatLon`` object per point.
        """

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
        bearing_index = np.rint(azimuth_deg / float(self.dtheta_deg)).astype(np.int64) % n_bearings
        range_index = np.rint(distance_m / float(self.dr_m)).astype(np.int64)
        range_index = np.clip(range_index, 0, n_ranges - 1)
        in_range = distance_m <= float(self.max_range_m) + 0.5 * float(self.dr_m)

        valid = np.zeros(lat.shape, dtype=np.bool_)
        excess = np.zeros(lat.shape, dtype=np.float32)
        direct_valid = in_range & self.valid[bearing_index, range_index]
        if np.any(direct_valid):
            excess[direct_valid] = self.excess_loss_db[
                bearing_index[direct_valid], range_index[direct_valid]
            ]
            valid[direct_valid] = True

        missing_flat = np.flatnonzero((in_range & ~direct_valid).ravel())
        if missing_flat.size:
            bi0 = bearing_index.ravel()[missing_flat]
            ri0 = range_index.ravel()[missing_flat]
            best_cost = np.full(missing_flat.size, 32767, dtype=np.int16)
            best_bi = np.zeros(missing_flat.size, dtype=np.int64)
            best_ri = np.zeros(missing_flat.size, dtype=np.int64)
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
                excess.ravel()[target_flat] = self.excess_loss_db[
                    best_bi[found], best_ri[found]
                ]
                valid.ravel()[target_flat] = True

        return excess, distance_m, azimuth_deg, valid

    def metadata(self) -> dict[str, Any]:
        return {
            "model": self.source_model,
            "receiver": {"latitude": self.receiver.lat, "longitude": self.receiver.lon},
            "max_range_m": float(self.max_range_m),
            "radial_resolution_m": float(self.dr_m),
            "bearing_resolution_deg": float(self.dtheta_deg),
            "valid_samples": int(np.count_nonzero(self.valid)),
            "total_samples": int(self.valid.size),
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
) -> ReturnPathEnvironmentLookup:
    """Convert a receiver-centered world model into a compact polar lookup."""

    n_bearings = max(1, int(math.ceil(360.0 / float(dtheta_deg))))
    n_ranges = max(2, int(math.ceil(float(max_range_m) / float(dr_m))) + 1)
    excess = np.zeros((n_bearings, n_ranges), dtype=np.float32)
    valid = np.zeros((n_bearings, n_ranges), dtype=np.bool_)

    for cell in world.cells:
        bi = int(round((float(cell.bearing_deg) % 360.0) / float(dtheta_deg))) % n_bearings
        ri = int(round(float(cell.distance_m) / float(dr_m)))
        if ri < 0 or ri >= n_ranges:
            continue
        excess[bi, ri] = np.float32(cell_environment_excess_loss_db(cell))
        valid[bi, ri] = True

    # The origin has zero environmental excess loss on every ray.
    valid[:, 0] = True
    excess[:, 0] = 0.0
    return ReturnPathEnvironmentLookup(
        receiver=receiver,
        max_range_m=float(max_range_m),
        dr_m=float(dr_m),
        dtheta_deg=float(dtheta_deg),
        excess_loss_db=excess,
        valid=valid,
    )


def return_path_rf_params(
    rf_params: RFParams,
    *,
    receiver_antenna_height_m: float,
    target_height_m: float,
    max_range_m: float,
) -> RFParams:
    """Clone RF settings for geometry-only reciprocal-path world generation."""

    clone = rf_params.model_copy(deep=True)
    clone.channel_analysis = None
    clone.tx_height_m = float(receiver_antenna_height_m)
    clone.rx_height_m = float(target_height_m)
    clone.max_range_m = float(max_range_m)
    clone.termination_rsrp_dbm = -300.0
    clone.compact_output = False
    return clone
