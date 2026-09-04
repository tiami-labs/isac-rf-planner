"""FlatTerrainSource — zero-elevation fallback, always available."""

from __future__ import annotations

from typing import List, Tuple

from ...pipeline.schemas import LatLon


class FlatTerrainSource:
    """Returns 0.0 m AMSL everywhere.  Used as the terminal fallback in the chain."""

    name = "flat"

    def available(self) -> bool:
        return True

    def elevation_m(self, lat: float, lon: float) -> float:
        return 0.0

    def profile_along_bearing(
        self,
        tx: LatLon,
        bearing_deg: float,
        max_range_m: float,
        step_m: float,
    ) -> Tuple[List[float], List[float]]:
        step = max(1.0, float(step_m))
        max_r = max(step, float(max_range_m))
        u: List[float] = []
        r = 0.0
        while r <= max_r + 1e-6:
            u.append(r)
            r += step
        return u, [0.0] * len(u)

    def prefetch(self, tx: LatLon, max_range_m: float, step_m: float, dtheta_deg: float) -> None:
        pass
