from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ...pipeline.schemas import LatLon
from ..google_mesh.utils import haversine_m
from ..osm_map_provider import _estimate_osm_height_m, _extract_building_material
from .path_types import RayPath, RayPoint, RayTraceResult

XY = Tuple[float, float]


def _meters_per_deg(lat_deg: float) -> Tuple[float, float]:
    lat_rad = math.radians(lat_deg)
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * max(1e-6, math.cos(lat_rad))
    return m_per_deg_lon, m_per_deg_lat


def ll_to_xy(origin: LatLon, p: LatLon) -> XY:
    m_per_deg_lon, m_per_deg_lat = _meters_per_deg(origin.lat)
    return ((p.lon - origin.lon) * m_per_deg_lon, (p.lat - origin.lat) * m_per_deg_lat)


def xy_to_ll(origin: LatLon, xy: XY) -> LatLon:
    m_per_deg_lon, m_per_deg_lat = _meters_per_deg(origin.lat)
    return LatLon(lat=origin.lat + (xy[1] / m_per_deg_lat), lon=origin.lon + (xy[0] / m_per_deg_lon))


def _dot(a: XY, b: XY) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _sub(a: XY, b: XY) -> XY:
    return (a[0] - b[0], a[1] - b[1])


def _add(a: XY, b: XY) -> XY:
    return (a[0] + b[0], a[1] + b[1])


def _mul(a: XY, s: float) -> XY:
    return (a[0] * s, a[1] * s)


def _length(a: XY) -> float:
    return math.hypot(a[0], a[1])


def _cross(a: XY, b: XY) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _line_reflect_point(p: XY, a: XY, b: XY) -> Optional[XY]:
    ab = _sub(b, a)
    denom = _dot(ab, ab)
    if denom <= 1e-9:
        return None
    ap = _sub(p, a)
    proj = _add(a, _mul(ab, _dot(ap, ab) / denom))
    return _sub(_mul(proj, 2.0), p)


def _segment_intersection(p: XY, p2: XY, q: XY, q2: XY) -> Optional[Tuple[XY, float, float]]:
    r = _sub(p2, p)
    s = _sub(q2, q)
    denom = _cross(r, s)
    if abs(denom) < 1e-9:
        return None
    qp = _sub(q, p)
    t = _cross(qp, s) / denom
    u = _cross(qp, r) / denom
    if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= u <= 1 + 1e-9:
        pt = _add(p, _mul(r, t))
        return pt, t, u
    return None


def _point_in_polygon(point: XY, poly: Sequence[XY]) -> bool:
    x, y = point
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi)
        if cond:
            inside = not inside
        j = i
    return inside


def _segment_hits_polygon(a: XY, b: XY, poly: Sequence[XY]) -> bool:
    if _point_in_polygon(a, poly) or _point_in_polygon(b, poly):
        return True
    n = len(poly)
    for i in range(n):
        p0 = poly[i]
        p1 = poly[(i + 1) % n]
        hit = _segment_intersection(a, b, p0, p1)
        if hit is not None:
            return True
    return False


def _path_clear(a: XY, b: XY, polygons: List[Tuple[int, Sequence[XY]]], ignore_id: Optional[int] = None) -> bool:
    for pid, poly in polygons:
        if ignore_id is not None and pid == ignore_id:
            continue
        if _segment_hits_polygon(a, b, poly):
            return False
    return True


def _building_height(building: Dict[str, Any]) -> float:
    h = building.get('height_m')
    if h is None:
        h = _estimate_osm_height_m(building.get('tags', {}))
        if h is None:
            h = 12.0
        building['height_m'] = h
    return float(h)


def _building_kind(building: Dict[str, Any]) -> str:
    tags = building.get('tags', {}) or {}
    b = str(tags.get('building', '') or '').lower()
    if b in ('house', 'detached', 'bungalow', 'residential'):
        return 'house'
    if b in ('commercial', 'office', 'industrial', 'warehouse', 'retail', 'apartments'):
        return 'large_structure'
    return 'building'


class OSMRayTraceEngine:
    def __init__(self, tx_height_m: float = 10.0, rx_height_m: float = 1.5):
        self.tx_height_m = float(tx_height_m)
        self.rx_height_m = float(rx_height_m)

    def _filtered_buildings(self, buildings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        slice_h = min(self.tx_height_m, self.rx_height_m)
        for b in buildings:
            h = _building_height(b)
            if h + 1e-6 >= slice_h:
                if 'material' not in b:
                    b['material'] = _extract_building_material(b)
                out.append(b)
        return out

    def _as_polygons(self, origin: LatLon, buildings: List[Dict[str, Any]]) -> List[Tuple[int, List[XY], Dict[str, Any]]]:
        polys = []
        for idx, b in enumerate(buildings):
            geom = b.get('geometry', [])
            pts = [ll_to_xy(origin, LatLon(lat=p['lat'], lon=p['lon'])) for p in geom if 'lat' in p and 'lon' in p]
            if len(pts) >= 3:
                polys.append((int(b.get('id') or idx), pts, b))
        return polys

    def trace(self, tx: LatLon, rx: LatLon, buildings: List[Dict[str, Any]], max_reflections: int = 1) -> RayTraceResult:
        buildings = self._filtered_buildings(buildings)
        polys_full = self._as_polygons(tx, buildings)
        polys = [(pid, poly) for pid, poly, _ in polys_full]
        tx_xy = (0.0, 0.0)
        rx_xy = ll_to_xy(tx, rx)
        tx_pt = RayPoint(lat=tx.lat, lon=tx.lon, height_m=self.tx_height_m)
        rx_pt = RayPoint(lat=rx.lat, lon=rx.lon, height_m=self.rx_height_m)
        paths: List[RayPath] = []

        direct_blocked = not _path_clear(tx_xy, rx_xy, polys)
        direct_length = haversine_m(tx, rx)
        paths.append(RayPath(path_type='direct', points=[tx_pt, rx_pt], total_length_m=direct_length, path_gain_db=-direct_length, blocked=direct_blocked, metadata=None))

        if max_reflections <= 0:
            return RayTraceResult(tx=tx_pt, rx=rx_pt, paths=paths)

        best_reflection: Optional[RayPath] = None
        best_length = float('inf')
        for pid, poly, b in polys_full:
            n = len(poly)
            material = b.get('material') or _extract_building_material(b)
            refl_penalty = {
                'glass': 4.0,
                'metal': 2.5,
                'concrete': 6.0,
                'brick': 5.0,
                'wood': 7.0,
                'unknown': 6.5,
            }.get(str(material), 6.5)
            for i in range(n):
                a = poly[i]
                c = poly[(i + 1) % n]
                mirrored = _line_reflect_point(rx_xy, a, c)
                if mirrored is None:
                    continue
                hit = _segment_intersection(tx_xy, mirrored, a, c)
                if hit is None:
                    continue
                refl_xy, _, u = hit
                if u < -1e-6 or u > 1 + 1e-6:
                    continue
                if _length(_sub(refl_xy, tx_xy)) < 1.0 or _length(_sub(refl_xy, rx_xy)) < 1.0:
                    continue
                if not _path_clear(tx_xy, refl_xy, polys, ignore_id=pid):
                    continue
                if not _path_clear(refl_xy, rx_xy, polys, ignore_id=pid):
                    continue
                p0 = xy_to_ll(tx, refl_xy)
                path_len = _length(_sub(tx_xy, refl_xy)) + _length(_sub(rx_xy, refl_xy))
                gain = -(path_len + refl_penalty)
                cand = RayPath(
                    path_type='reflection',
                    points=[tx_pt, RayPoint(lat=p0.lat, lon=p0.lon, height_m=max(self.tx_height_m, self.rx_height_m)), rx_pt],
                    total_length_m=path_len,
                    path_gain_db=gain,
                    blocked=False,
                    metadata={'building_id': pid, 'material': material, 'kind': _building_kind(b)},
                )
                if path_len < best_length:
                    best_length = path_len
                    best_reflection = cand
        if best_reflection is not None:
            paths.append(best_reflection)
        return RayTraceResult(tx=tx_pt, rx=rx_pt, paths=paths)
