#!/usr/bin/env python3
"""Standalone 3D demo: OSM-aware RF + 3D Buildings/Forests styling (Milestone A)

This is intentionally NOT integrated into the main app/UI.

Pipeline:
  - Fetch OSM buildings + landuse/natural polygons (forests/wood/parks, etc.)
  - Build WorldModel using the repo's existing OSMMapProvider + RF pipeline
  - Compute an attenuation grid (RSRP per cell)
  - Assign each building and forest polygon a representative RSRP by sampling
    the nearest RF cell to the polygon centroid.
  - Generate an HTML file that:
      * loads Google Photorealistic 3D Tiles (background)
      * extrudes OSM buildings into 3D prisms
      * extrudes forests into canopy volumes
      * colors these volumes by computed RSRP

Why this approach:
  - Google photorealistic tiles are a textured mesh with little/no per-building metadata.
  - For "building interaction" you need a semantic layer (OSM) that the RF model uses.
  - We render that semantic layer in 3D and style it by the computed RF results.

Run:
  python3 scripts/test_google_maps_3d_osm_buildings.py \
    --api-key YOUR_KEY \
    --lat 37.7749 --lon -122.4194 \
    --max-range-m 500 --step-m 5 \
    --freq-mhz 3500 --tx-power-dbm 43 \
    --output ./osm_rf_3d.html

Serve:
  python3 -m http.server 8000
  open http://localhost:8000/osm_rf_3d.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

# Allow running as a standalone script from repo root.
import sys

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from isac_rf_planner.geo.osm_map_provider import OSMMapProvider  # noqa: E402
from isac_rf_planner.pipeline.schemas import LatLon, RFParams  # noqa: E402
from isac_rf_planner.pipeline.world_builder import build_world_model  # noqa: E402
from isac_rf_planner.rf.attenuation_models import compute_attenuation_grid  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# -------------------------
# Google tileset caching
# -------------------------


class GoogleMaps3DTilesCache:
    """Cache manager for Google Maps 3D Tiles root.json to reduce API churn."""

    def __init__(self, cache_dir: str = "./cache/google_maps_3d"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Session tokens are multi-hour; keep cache aligned.
        self.cache_ttl_hours = 3

    def _get_cache_key(self, api_key: str) -> str:
        return hashlib.md5(api_key.encode()).hexdigest()

    def _get_cache_path(self, cache_key: str) -> Path:
        return self.cache_dir / f"tileset_{cache_key}.json"

    def get_cached_tileset(self, api_key: str) -> Optional[Dict[str, Any]]:
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        if not cache_path.exists():
            return None
        try:
            cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
            cached_time = datetime.fromisoformat(cache_data["timestamp"])
            if datetime.now() - cached_time > timedelta(hours=self.cache_ttl_hours):
                return None
            logger.info(f"Using cached tileset (age: {datetime.now() - cached_time})")
            return cache_data["tileset"]
        except Exception as e:
            logger.warning(f"Error reading Google tileset cache: {e}")
            return None

    def cache_tileset(self, api_key: str, tileset_data: Dict[str, Any]) -> None:
        cache_key = self._get_cache_key(api_key)
        cache_path = self._get_cache_path(cache_key)
        try:
            cache_data = {"timestamp": datetime.now().isoformat(), "tileset": tileset_data}
            cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Error writing Google tileset cache: {e}")


def fetch_tileset_with_cache(api_key: str, cache_dir: str, use_cache: bool) -> Dict[str, Any]:
    cache = GoogleMaps3DTilesCache(cache_dir)
    if use_cache:
        cached = cache.get_cached_tileset(api_key)
        if cached:
            return cached

    url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    logger.info("Fetching Google 3D tileset root.json…")
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    tileset_data = r.json()
    if use_cache:
        cache.cache_tileset(api_key, tileset_data)
    return tileset_data
# -------------------------
# Cesium static assets (self-host)
# -------------------------

def ensure_cesium_static_dir(target_dir: str, *, copy_from_node_modules: bool = True) -> None:
    """Ensure Cesium static assets exist at target_dir.

    The generated HTML expects:
      <target_dir>/Cesium.js
      <target_dir>/Assets/*
      <target_dir>/Widgets/*
      <target_dir>/Workers/*

    If missing and copy_from_node_modules=True, we try to copy from:
      ./node_modules/cesium/Build/Cesium
    """
    target = Path(target_dir)
    if (target / "Cesium.js").exists():
        return

    if not copy_from_node_modules:
        logger.warning(f"Cesium.js not found at {target}. Page will not load unless Cesium is served there.")
        return

    nm = Path("node_modules") / "cesium" / "Build" / "Cesium"
    if not (nm / "Cesium.js").exists():
        logger.warning(
            "Cesium.js not found at the expected node_modules path. "
            "Install Cesium with: npm i cesium@1.111, then copy Build/Cesium into your web root as ./Cesium"
        )
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(nm, target, dirs_exist_ok=True)
    logger.info(f"Copied Cesium static assets to: {target}")



# -------------------------
# OSM geometry helpers
# -------------------------


def _parse_height_m(tags: Dict[str, Any], default_m: float = 12.0) -> float:
    """Parse building height from OSM tags (Option A).

    Priority:
      1) height (meters or with units)
      2) building:levels (levels * 3m)
      3) fallback default
    """

    def _to_float(s: str) -> Optional[float]:
        try:
            return float(s)
        except Exception:
            return None

    # height tag
    h = tags.get("height")
    if isinstance(h, str) and h.strip():
        hs = h.strip().lower()
        # Common variants: "12", "12m", "12 m", "35 ft"
        # Strip non-numeric except dot and minus
        unit = "m"
        if "ft" in hs or "feet" in hs:
            unit = "ft"
        # Extract first number
        num = ""
        for ch in hs:
            if ch.isdigit() or ch in ".-":
                num += ch
            elif num:
                break
        hv = _to_float(num)
        if hv is not None and hv > 0:
            if unit == "ft":
                return hv * 0.3048
            return hv

    # building:levels tag
    levels = tags.get("building:levels") or tags.get("levels")
    if isinstance(levels, str) and levels.strip():
        # levels can be "3" or "3;4". Take first.
        part = levels.strip().split(";")[0].strip()
        lv = _to_float(part)
        if lv is not None and lv > 0:
            return max(3.0, lv * 3.0)

    return float(default_m)


def _is_forest_like(tags: Dict[str, Any]) -> bool:
    landuse = (tags.get("landuse") or "").lower()
    natural = (tags.get("natural") or "").lower()
    leisure = (tags.get("leisure") or "").lower()

    if natural in {"wood", "scrub", "tree_row"}:
        return True
    if landuse in {"forest", "orchard", "vineyard"}:
        return True
    # Parks can be tree-heavy; render them as canopy volumes for demo
    if leisure in {"park", "garden"}:
        return True
    return False


def _polygon_vertices_lonlat(geometry: List[dict]) -> List[Tuple[float, float]]:
    """OSM geometry list -> list[(lon,lat)]."""
    pts: List[Tuple[float, float]] = []
    for p in geometry:
        try:
            pts.append((float(p["lon"]), float(p["lat"])))
        except Exception:
            continue
    # OSM ways often repeat the first point at the end. Keep it; Cesium tolerates.
    return pts


def _centroid_lonlat(pts_lonlat: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """Planar polygon centroid on lon/lat (good enough for small areas)."""
    if not pts_lonlat:
        return (0.0, 0.0)
    # Ensure closed ring for centroid formula.
    pts = list(pts_lonlat)
    if pts[0] != pts[-1]:
        pts.append(pts[0])

    # Polygon centroid formula in 2D (x=lon,y=lat)
    a = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-12:
        # Fallback: average points
        sx = sum(p[0] for p in pts_lonlat)
        sy = sum(p[1] for p in pts_lonlat)
        n = max(1, len(pts_lonlat))
        return (sx / n, sy / n)
    a *= 0.5
    cx /= (6.0 * a)
    cy /= (6.0 * a)
    return (cx, cy)


def _to_local_xy_m(lon: float, lat: float, origin_lon: float, origin_lat: float) -> Tuple[float, float]:
    """Equirectangular projection around origin (meters)."""
    # meters per degree
    lat_rad = math.radians(origin_lat)
    m_per_deg_lat = 110540.0
    m_per_deg_lon = 111320.0 * math.cos(lat_rad)
    x = (lon - origin_lon) * m_per_deg_lon
    y = (lat - origin_lat) * m_per_deg_lat
    return (x, y)



def _bbox_around_point(lat: float, lon: float, radius_m: float) -> str:
    # Return Overpass bbox string: south,west,north,east.
    dlat = radius_m / 110540.0
    dlon = radius_m / (111320.0 * max(0.1, math.cos(math.radians(lat))))
    return f"{lat - dlat},{lon - dlon},{lat + dlat},{lon + dlon}"


OVERPASS_URL = "https://overpass-api.de/api/interpreter"


def _point_in_poly(x_lon: float, y_lat: float, poly: Sequence[Tuple[float, float]]) -> bool:
    # Ray casting point-in-polygon on lon/lat.
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        denom = (yj - yi) if (yj - yi) != 0 else 1e-12
        intersect = ((yi > y_lat) != (yj > y_lat)) and (x_lon < (xj - xi) * (y_lat - yi) / denom + xi)
        if intersect:
            inside = not inside
        j = i
    return inside


def fetch_osm_road_samples(center_lat: float, center_lon: float, radius_m: float, step_m: float = 25.0) -> List[Tuple[float, float]]:
    # Fetch OSM highway ways via Overpass and return sampled (lon,lat) points along roads.
    bbox = _bbox_around_point(center_lat, center_lon, radius_m)
    query = f"""
[out:json][timeout:25];
(
  way[\"highway\"]({bbox});
);
out geom;
"""
    try:
        resp = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"OSM roads fetch failed (continuing without road grounding): {e}")
        return []

    samples: List[Tuple[float, float]] = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        geom = el.get("geometry") or []
        pts = [(float(p["lon"]), float(p["lat"])) for p in geom if "lon" in p and "lat" in p]
        if len(pts) < 2:
            continue
        for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:]):
            x1, y1 = _to_local_xy_m(lon1, lat1, center_lon, center_lat)
            x2, y2 = _to_local_xy_m(lon2, lat2, center_lon, center_lat)
            dist = math.hypot(x2 - x1, y2 - y1)
            n = max(1, int(dist // max(1.0, step_m)))
            for ii in range(n + 1):
                t = ii / n
                samples.append((lon1 + (lon2 - lon1) * t, lat1 + (lat2 - lat1) * t))

    # cheap de-dupe
    dedup: List[Tuple[float, float]] = []
    seen = set()
    for lon, lat in samples:
        key = (round(lon, 6), round(lat, 6))
        if key in seen:
            continue
        seen.add(key)
        dedup.append((lon, lat))
    return dedup


def k_nearest_road_candidates(
    centroid_lon: float,
    centroid_lat: float,
    footprint: Sequence[Tuple[float, float]],
    road_samples: Sequence[Tuple[float, float]],
    origin_lon: float,
    origin_lat: float,
    k: int = 8,
    max_dist_m: float = 220.0,
) -> List[Tuple[float, float]]:
    # Return up to k nearest road sample points outside the footprint.
    if not road_samples:
        return []
    cx, cy = _to_local_xy_m(centroid_lon, centroid_lat, origin_lon, origin_lat)
    max_d2 = max_dist_m * max_dist_m
    scored: List[Tuple[float, Tuple[float, float]]] = []
    for lon, lat in road_samples:
        if _point_in_poly(lon, lat, footprint):
            continue
        x, y = _to_local_xy_m(lon, lat, origin_lon, origin_lat)
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 > max_d2:
            continue
        scored.append((d2, (lon, lat)))
    scored.sort(key=lambda t: t[0])
    return [p for _, p in scored[: max(0, k)]]




def _nearest_rsrp_dbm(
    grid_lonlat_rsrp: Sequence[Tuple[float, float, float]],
    q_lon: float,
    q_lat: float,
    origin_lon: float,
    origin_lat: float,
) -> float:
    """Find nearest grid cell's RSRP (fast enough for demo sizes)."""
    qx, qy = _to_local_xy_m(q_lon, q_lat, origin_lon, origin_lat)
    best = None
    best_d2 = float("inf")
    for lon, lat, rsrp in grid_lonlat_rsrp:
        x, y = _to_local_xy_m(lon, lat, origin_lon, origin_lat)
        d2 = (x - qx) * (x - qx) + (y - qy) * (y - qy)
        if d2 < best_d2:
            best_d2 = d2
            best = rsrp
    return float(best if best is not None else -150.0)


# -------------------------
# HTML generation
# -------------------------


@dataclass
class BuildingFeature:
    osm_id: int
    height_m: float
    centroid_lon: float
    centroid_lat: float
    vertices: List[Tuple[float, float]]
    rf_dbm: float
    ground_lon: float
    ground_lat: float
    ground_cands: List[Tuple[float, float]]


@dataclass
class ForestFeature:
    osm_id: int
    canopy_m: float
    centroid_lon: float
    centroid_lat: float
    vertices: List[Tuple[float, float]]
    rf_dbm: float
    kind: str
    ground_lon: float
    ground_lat: float


def _color_ramp_js() -> str:
    """Return JS helpers for RSRP -> color."""
    # Avoid template literals to prevent f-string brace issues.
    return r"""
function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// Map RSRP dBm [-120..-60] to a blue->red ramp.
function colorForRsrp(rsrpDbm, alpha) {
  // normalize: -120 -> 0, -60 -> 1
  const t = clamp((rsrpDbm + 120.0) / 60.0, 0.0, 1.0);
  // simple HSV-ish ramp: blue(240deg) to red(0deg)
  const hue = (1.0 - t) * 240.0;
  const c = Cesium.Color.fromHsl(hue / 360.0, 1.0, 0.5, alpha);
  return c;
}
"""

def _interactive_click_js() -> str:
    """JS block: click-to-set TX and reproject heat (uses OSM polygons for interaction)."""
    return r"""
// === Interactive TX click + 3D heat projection (Milestone A - interactive) ===

function metersPerDegLat() { return 111320.0; }
function metersPerDegLon(latDeg) { return 111320.0 * Math.cos(latDeg * Math.PI / 180.0); }

function lonLatToOffsetMeters(originLon, originLat, lon, lat) {
  const dx = (lon - originLon) * metersPerDegLon(originLat);
  const dy = (lat - originLat) * metersPerDegLat();
  return [dx, dy];
}

function offsetMetersToLonLat(originLon, originLat, dx, dy) {
  const lon = originLon + dx / metersPerDegLon(originLat);
  const lat = originLat + dy / metersPerDegLat();
  return [lon, lat];
}

function haversineMeters(lat1, lon1, lat2, lon2) {
  const R = 6371000.0;
  const dLat = (lat2 - lat1) * Math.PI / 180.0;
  const dLon = (lon2 - lon1) * Math.PI / 180.0;
  const a = Math.sin(dLat/2.0)*Math.sin(dLat/2.0) +
            Math.cos(lat1*Math.PI/180.0)*Math.cos(lat2*Math.PI/180.0)*
            Math.sin(dLon/2.0)*Math.sin(dLon/2.0);
  const c = 2.0 * Math.atan2(Math.sqrt(a), Math.sqrt(1.0 - a));
  return R * c;
}

function fsplDb(distM, freqMHz) {
  const dKm = Math.max(0.001, distM / 1000.0); // >= 1m
  return 32.44 + 20.0 * Math.log10(freqMHz) + 20.0 * Math.log10(dKm);
}

function _orient(ax, ay, bx, by, cx, cy) {
  // cross((b-a),(c-a))
  return (bx-ax)*(cy-ay) - (by-ay)*(cx-ax);
}

function _onSeg(ax, ay, bx, by, cx, cy) {
  return Math.min(ax, bx) <= cx + 1e-12 && cx <= Math.max(ax, bx) + 1e-12 &&
         Math.min(ay, by) <= cy + 1e-12 && cy <= Math.max(ay, by) + 1e-12;
}

function _segIntersects(ax, ay, bx, by, cx, cy, dx, dy) {
  const o1 = _orient(ax, ay, bx, by, cx, cy);
  const o2 = _orient(ax, ay, bx, by, dx, dy);
  const o3 = _orient(cx, cy, dx, dy, ax, ay);
  const o4 = _orient(cx, cy, dx, dy, bx, by);

  if ((o1 > 0 && o2 < 0 || o1 < 0 && o2 > 0) && (o3 > 0 && o4 < 0 || o3 < 0 && o4 > 0)) return true;

  if (Math.abs(o1) < 1e-12 && _onSeg(ax, ay, bx, by, cx, cy)) return true;
  if (Math.abs(o2) < 1e-12 && _onSeg(ax, ay, bx, by, dx, dy)) return true;
  if (Math.abs(o3) < 1e-12 && _onSeg(cx, cy, dx, dy, ax, ay)) return true;
  if (Math.abs(o4) < 1e-12 && _onSeg(cx, cy, dx, dy, bx, by)) return true;
  return false;
}

function segmentIntersectsPolygon(ax, ay, bx, by, verts) {
  const v = _dropDuplicateClosingVertex(verts);
  if (!v || v.length < 3) return false;

  // If endpoint is inside polygon, count as intersection.
  if (_pointInPoly(ax, ay, v) || _pointInPoly(bx, by, v)) return true;

  for (let i = 0; i < v.length; i++) {
    const j = (i + 1) % v.length;
    const cx = v[i][0], cy = v[i][1];
    const dx = v[j][0], dy = v[j][1];
    if (_segIntersects(ax, ay, bx, by, cx, cy, dx, dy)) return true;
  }
  return false;
}

function bboxOfVerts(verts) {
  const v = _dropDuplicateClosingVertex(verts);
  let minLon = Infinity, minLat = Infinity, maxLon = -Infinity, maxLat = -Infinity;
  for (let i = 0; i < v.length; i++) {
    const lon = v[i][0], lat = v[i][1];
    if (lon < minLon) minLon = lon;
    if (lat < minLat) minLat = lat;
    if (lon > maxLon) maxLon = lon;
    if (lat > maxLat) maxLat = lat;
  }
  return [minLon, minLat, maxLon, maxLat];
}

function bboxOverlap(a, b) {
  // [minLon,minLat,maxLon,maxLat]
  return !(a[2] < b[0] || a[0] > b[2] || a[3] < b[1] || a[1] > b[3]);
}

function makeSpatialIndex(features, cellLonDeg, cellLatDeg) {
  // Simple spatial hash on feature bbox coverage. Good enough for ~1k polys.
  // Returns {cellLonDeg, cellLatDeg, buckets: Map<string, int[]>, bboxes: Array<[..]>}
  const buckets = new Map();
  const bboxes = [];
  let globalMinLon = Infinity, globalMinLat = Infinity;

  for (let i = 0; i < features.length; i++) {
    const bb = bboxOfVerts(features[i].verts);
    bboxes.push(bb);
    if (bb[0] < globalMinLon) globalMinLon = bb[0];
    if (bb[1] < globalMinLat) globalMinLat = bb[1];
  }

  function key(ix, iy) { return ix + "," + iy; }

  for (let i = 0; i < features.length; i++) {
    const bb = bboxes[i];
    const ix0 = Math.floor((bb[0] - globalMinLon) / cellLonDeg);
    const ix1 = Math.floor((bb[2] - globalMinLon) / cellLonDeg);
    const iy0 = Math.floor((bb[1] - globalMinLat) / cellLatDeg);
    const iy1 = Math.floor((bb[3] - globalMinLat) / cellLatDeg);

    for (let ix = ix0; ix <= ix1; ix++) {
      for (let iy = iy0; iy <= iy1; iy++) {
        const k = key(ix, iy);
        const arr = buckets.get(k);
        if (arr) arr.push(i);
        else buckets.set(k, [i]);
      }
    }
  }

  return { cellLonDeg: cellLonDeg, cellLatDeg: cellLatDeg, buckets: buckets, bboxes: bboxes, minLon: globalMinLon, minLat: globalMinLat };
}

function querySpatial(index, rayBbox) {
  const out = new Set();
  const ix0 = Math.floor((rayBbox[0] - index.minLon) / index.cellLonDeg);
  const ix1 = Math.floor((rayBbox[2] - index.minLon) / index.cellLonDeg);
  const iy0 = Math.floor((rayBbox[1] - index.minLat) / index.cellLatDeg);
  const iy1 = Math.floor((rayBbox[3] - index.minLat) / index.cellLatDeg);
  for (let ix = ix0; ix <= ix1; ix++) {
    for (let iy = iy0; iy <= iy1; iy++) {
      const k = ix + "," + iy;
      const arr = index.buckets.get(k);
      if (!arr) continue;
      for (let t = 0; t < arr.length; t++) out.add(arr[t]);
    }
  }
  return out;
}

const _IDX_CELL_M = 140.0;
const _IDX_LON_DEG = _IDX_CELL_M / metersPerDegLon(CENTER_LAT);
const _IDX_LAT_DEG = _IDX_CELL_M / metersPerDegLat();

const _buildingIndex = makeSpatialIndex(buildings, _IDX_LON_DEG, _IDX_LAT_DEG);
const _forestIndex = makeSpatialIndex(forests, _IDX_LON_DEG, _IDX_LAT_DEG);

// Precompute RX sample offsets relative to initial center, so we can reuse the same pattern around any TX.
const _gridOffsets = (function() {
  const out = [];
  for (let i = 0; i < grid.length; i++) {
    const o = lonLatToOffsetMeters(CENTER_LON, CENTER_LAT, grid[i].lon, grid[i].lat);
    out.push(o);
  }
  return out;
})();

function obstructionCounts(txLon, txLat, rxLon, rxLat) {
  const rayBbox = [
    Math.min(txLon, rxLon), Math.min(txLat, rxLat),
    Math.max(txLon, rxLon), Math.max(txLat, rxLat)
  ];
  const buildCandidates = querySpatial(_buildingIndex, rayBbox);
  let buildHits = 0;
  for (const idx of buildCandidates) {
    const bb = _buildingIndex.bboxes[idx];
    if (!bboxOverlap(rayBbox, bb)) continue;
    const poly = buildings[idx].verts;
    if (segmentIntersectsPolygon(txLon, txLat, rxLon, rxLat, poly)) buildHits++;
  }

  const forestCandidates = querySpatial(_forestIndex, rayBbox);
  let forestHits = 0;
  for (const idx of forestCandidates) {
    const bb = _forestIndex.bboxes[idx];
    if (!bboxOverlap(rayBbox, bb)) continue;
    const poly = forests[idx].verts;
    if (segmentIntersectsPolygon(txLon, txLat, rxLon, rxLat, poly)) forestHits++;
  }
  return [buildHits, forestHits];
}

function computeRsrpDbm(txLon, txLat, rxLon, rxLat) {
  const d = haversineMeters(txLat, txLon, rxLat, rxLon);
  const pl = fsplDb(d, RF_FREQ_MHZ);

  const hits = obstructionCounts(txLon, txLat, rxLon, rxLat);
  const bHits = hits[0];
  const fHits = hits[1];

  // Very rough loss model (demo): each building crossing ~8 dB, vegetation ~4 dB.
  // Cap so the map doesn't go completely dark.
  const bLoss = Math.min(35.0, bHits * 8.0);
  const fLoss = Math.min(20.0, fHits * 4.0);

  const rsrp = TX_POWER_DBM - pl - bLoss - fLoss;
  return rsrp;
}

let _heatPoints = null;
let _heatBuilt = false;
let _heatPrims = [];
let _heatLonLat = [];
let _txMarker = null;
let _txPlanePrim = null;
let _txPlaneGeomInstance = null;
let _txPlaneOutline = null;
let _txLon = CENTER_LON;
let _txLat = CENTER_LAT;
let _recomputeBusy = false;
let _pendingTx = null;

// Allow clicks while overlays are still building.
// If build is in progress, we queue the most recent TX and apply it once overlays finish.
window.__buildBusy = false;
window.__queuedTxLonLat = null;


function buildPlaneFullCircle(txLon, txLat) {
  // Always a full circle. Never "stops" or cuts for Google mesh or OSM.
  const numRays = 128;
  const metersPerDegLat = 111320.0;
  const cosLat = Math.cos(Cesium.Math.toRadians(txLat));
  const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);

  const wavePoints = [];
  for (let i = 0; i < numRays; i++) {
    const ang = (i / numRays) * Math.PI * 2.0;
    const east = Math.cos(ang) * MAX_RANGE_M;
    const north = Math.sin(ang) * MAX_RANGE_M;
    wavePoints.push(
      txLon + (east / metersPerDegLon),
      txLat + (north / metersPerDegLat)
    );
  }

  return {
    hierarchy: new Cesium.PolygonHierarchy(Cesium.Cartesian3.fromDegreesArray(wavePoints)),
    wavePoints: wavePoints
  };
}

async function computeInteractionFlags(txLon, txLat, planeHeight) {
  // Classification only affects COLOR; geometry stays full-circle.
  // Colors requested:
  //   - Google mesh hit => BLUE
  //   - OSM footprint hit => PINK
  //   - Both => WHITE
  //   - Neither => YELLOW
  const samples = 96;
  const radii = [0.4, 0.75, 1.0];
  const eps = 0.25; // meters

  const metersPerDegLat = 111320.0;
  const cosLat = Math.cos(Cesium.Math.toRadians(txLat));
  const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);

  const lonlats = [];
  for (let rIdx = 0; rIdx < radii.length; rIdx++) {
    const rr = radii[rIdx] * MAX_RANGE_M;
    for (let i = 0; i < samples; i++) {
      const ang = (i / samples) * Math.PI * 2.0;
      const east = Math.cos(ang) * rr;
      const north = Math.sin(ang) * rr;
      lonlats.push([
        txLon + (east / metersPerDegLon),
        txLat + (north / metersPerDegLat)
      ]);
    }
  }

  // OSM collision: any sample inside any OSM building footprint
  let hitOsm = false;
  if (buildings && buildings.length) {
    outer:
    for (let p = 0; p < lonlats.length; p++) {
      const qLon = lonlats[p][0], qLat = lonlats[p][1];
      for (let b = 0; b < buildings.length; b++) {
        const bb = buildings[b];
        if (!bb.verts || bb.verts.length < 3) continue;
        if (_pointInPoly(qLon, qLat, bb.verts)) { hitOsm = true; break outer; }
      }
    }
  }

  // Google collision: any clamped mesh height above the plane
  let hitGoogle = false;
  try {
    const probes = [];
    for (let p = 0; p < lonlats.length; p++) {
      const ll = lonlats[p];
      probes.push(Cesium.Cartesian3.fromDegrees(ll[0], ll[1], 200.0));
    }
    const clampPromise = viewer.scene.clampToHeightMostDetailed(probes);
    const clamped = await Promise.race([
      clampPromise,
      new Promise((resolve) => setTimeout(() => resolve(null), 800))
    ]);
    if (clamped && clamped.length) {
      for (let i = 0; i < clamped.length; i++) {
        const c = clamped[i];
        if (!c) continue;
        const ch = Cesium.Cartographic.fromCartesian(c).height;
        if (ch > (planeHeight + eps)) { hitGoogle = true; break; }
      }
    }
  } catch (e) {
    // ignore
  }

  let baseColor = Cesium.Color.YELLOW;
  if (hitGoogle && hitOsm) baseColor = Cesium.Color.WHITE;
  else if (hitGoogle) baseColor = Cesium.Color.BLUE;
  else if (hitOsm) baseColor = new Cesium.Color(1.0, 0.0, 1.0, 1.0); // pink/magenta

  return { hitGoogle, hitOsm, baseColor };
}



async function setTxMarker(lon, lat) {
  const h = await clampHeightAt(lon, lat);

  // Place TX slightly above the clamped surface so it's visible.
  const TX_HEIGHT_M = 10.0;
  const pos = Cesium.Cartesian3.fromDegrees(lon, lat, h + TX_HEIGHT_M);

  // Small center marker (for precise TX location)
  if (!_txMarker) {
    _txMarker = viewer.entities.add({
      name: 'tx',
      point: {
        pixelSize: 8,
        color: Cesium.Color.YELLOW,
        outlineColor: Cesium.Color.BLACK,
        outlineWidth: 2,
        // Always visible above the mesh
        disableDepthTestDistance: 0.0
      },
      position: pos
    });
  } else {
    _txMarker.position = pos;
  }

  // TX emission plane: full circle, never clipped/stopped.
  const planeHeight = (isFinite(h) ? h : 0.0) + 0.15;

  const result = buildPlaneFullCircle(lon, lat);
  const hierarchy = result.hierarchy;
  const wavePoints = result.wavePoints;

  const flags = await computeInteractionFlags(lon, lat, planeHeight);
  const baseColor = flags.baseColor;

  // --- Base TX plane fill rendered as a Primitive ---
  // Reason: Entities + depth test can make the plane appear to "stop" at the Google mesh.
  // This primitive disables depth testing so it always renders through the mesh.
  const alpha = 0.35;
  const instanceColor = new Cesium.Color(baseColor.red, baseColor.green, baseColor.blue, alpha);

  const geom = new Cesium.PolygonGeometry({
    polygonHierarchy: hierarchy,
    height: planeHeight,
    vertexFormat: Cesium.PerInstanceColorAppearance.VERTEX_FORMAT
  });

  _txPlaneGeomInstance = new Cesium.GeometryInstance({
    geometry: geom,
    attributes: {
      color: Cesium.ColorGeometryInstanceAttribute.fromColor(instanceColor)
    }
  });

  if (_txPlanePrim) {
    viewer.scene.primitives.remove(_txPlanePrim);
    _txPlanePrim = null;
  }

  _txPlanePrim = viewer.scene.primitives.add(new Cesium.Primitive({
    geometryInstances: _txPlaneGeomInstance,
    appearance: new Cesium.PerInstanceColorAppearance({
      translucent: true,
      closed: false
    }),
    asynchronous: false
  }));

  // Never get occluded by the Google mesh
  _txPlanePrim.appearance.renderState = Cesium.RenderState.fromCache({
    depthTest: { enabled: false },
    depthMask: false,
    blending: Cesium.BlendingState.ALPHA_BLEND
  });

  // Boundary outline (debug + always visible)
  if (_txPlaneOutline) {
    viewer.entities.remove(_txPlaneOutline);
    _txPlaneOutline = null;
  }
  const outlinePts = wavePoints.slice();
  // close the loop
  outlinePts.push(wavePoints[0], wavePoints[1]);

  _txPlaneOutline = viewer.entities.add({
    name: "tx_plane_outline",
    polyline: {
      positions: Cesium.Cartesian3.fromDegreesArray(outlinePts),
      width: 2,
      material: Cesium.Color.CYAN,
      clampToGround: false,
      disableDepthTestDistance: 0.0
    }
  });
}



// Build the heat overlay *once* at fixed world locations (the precomputed grid around CENTER).
// When TX moves we only update colors. This avoids the "overlay disappears/rebuilds" behavior.
async function buildHeatOverlayOnce() {
  if (!_heatPoints || _heatBuilt) return;

  // Precompute probe positions at the grid lon/lat (not relative to TX).
  const probes = [];
  _heatLonLat = [];
  for (let i = 0; i < grid.length; i++) {
    const lon = grid[i].lon;
    const lat = grid[i].lat;
    _heatLonLat.push([lon, lat]);
    probes.push(Cesium.Cartesian3.fromDegrees(lon, lat, 200.0));
  }

  // Clamp in batches (Cesium can get unhappy with huge arrays)
  const BATCH = 400;
  _heatPrims = new Array(probes.length);
  for (let i = 0; i < probes.length; i += BATCH) {
    const slice = probes.slice(i, i + BATCH);
    let clamped = null;
    try {
      clamped = await viewer.scene.clampToHeightMostDetailed(slice);
    } catch (e) {
      clamped = null;
    }
    for (let j = 0; j < slice.length; j++) {
      const k = i + j;
      const pos = (clamped && clamped[j]) ? clamped[j] : slice[j];
      const p = _heatPoints.add({
        position: pos,
        color: Cesium.Color.TRANSPARENT,
        pixelSize: 6,
        disableDepthTestDistance: 0.0,
        show: false
      });
      _heatPrims[k] = p;
    }
  }

  _heatBuilt = true;
  // Apply the current TX immediately after building.
  updateHeatOverlayColors();
}

function updateHeatOverlayColors() {
  if (!_heatPoints || !_heatBuilt) return;
  const on = document.getElementById('toggleHeat').checked;
  const alpha = parseFloat(document.getElementById('alpha').value || '0.55');

  for (let i = 0; i < _heatPrims.length; i++) {
    const p = _heatPrims[i];
    if (!p) continue;
    if (!on) {
      p.show = false;
      continue;
    }
    p.show = true;
    const ll = _heatLonLat[i];
    const rf = computeRsrpDbm(_txLon, _txLat, ll[0], ll[1]);
    p.color = colorForRsrp(rf, alpha);
  }
}

function applyRfToEntities() {
  const alpha = parseFloat(document.getElementById('alpha').value || '0.55');

  // Buildings
  for (let i = 0; i < buildingEntities.length; i++) {
    const ent = buildingEntities[i];
    const b = buildings[i];
    const lon = b.centroid[0];
    const lat = b.centroid[1];
    const rf = computeRsrpDbm(_txLon, _txLat, lon, lat);
    b.rf_dbm = rf;
    ent.properties.rf_dbm = rf;
    const c = colorForRsrp(rf, alpha);
    ent.polygon.material = c;
  }

  // Forests (mix green with RF ramp)
  for (let i = 0; i < forestEntities.length; i++) {
    const ent = forestEntities[i];
    const f = forests[i];
    const lon = f.centroid[0];
    const lat = f.centroid[1];
    const rf = computeRsrpDbm(_txLon, _txLat, lon, lat);
    f.rf_dbm = rf;
    ent.properties.rf_dbm = rf;

    const rfColor = colorForRsrp(rf, alpha);
    const green = new Cesium.Color(0.1, 0.8, 0.2, alpha);
    const mix = Cesium.Color.lerp(green, rfColor, 0.65, new Cesium.Color());
    ent.polygon.material = mix;
  }
}

async function recomputeAll(lon, lat) {
  _txLon = lon;
  _txLat = lat;
  statusEl.textContent = 'Recomputing RF (click TX) …';
  // Don't block RF updates on the (async) height clamp.
  setTxMarker(lon, lat).catch(function(e){ console.warn('setTxMarker failed', e); });
  applyRfToEntities();
  updateHeatOverlayColors();
  statusEl.textContent = 'Ready.';
}

async function requestRecompute(lon, lat) {
  _pendingTx = [lon, lat];
  if (_recomputeBusy) return;
  _recomputeBusy = true;
  while (_pendingTx) {
    const p = _pendingTx;
    _pendingTx = null;
    await recomputeAll(p[0], p[1]);
  }
  _recomputeBusy = false;
}

function setupInteractiveClickHandler() {
  // Replace any existing handler by just creating a new one (Cesium allows multiple, but we only use one in this demo).
  const handler = new Cesium.ScreenSpaceEventHandler(viewer.scene.canvas);
  handler.setInputAction(function(click) {
    // Always show pick info if an entity was clicked
    const picked = viewer.scene.pick(click.position);
    if (Cesium.defined(picked) && picked.id && picked.id.properties) {
      const props = picked.id.properties;
      const kind = props.kind ? props.kind.getValue() : 'unknown';
      const rf = props.rf_dbm ? props.rf_dbm.getValue() : null;
      const id = props.osm_id ? props.osm_id.getValue() : '';
      const msg = 'Picked ' + kind + ' ' + id + (rf !== null ? (' | RSRP ' + rf.toFixed(1) + ' dBm') : '');
      pickedEl.textContent = msg;
    } else {
      pickedEl.textContent = '';
    }

    if (!document.getElementById('toggleClick').checked) return;

    let cartesian = null;
    if (viewer.scene.pickPositionSupported) {
      cartesian = viewer.scene.pickPosition(click.position);
    }
    if (!Cesium.defined(cartesian)) {
      cartesian = viewer.camera.pickEllipsoid(click.position, Cesium.Ellipsoid.WGS84);
    }
    if (!Cesium.defined(cartesian)) return;

    const carto = Cesium.Cartographic.fromCartesian(cartesian);
    const lon = Cesium.Math.toDegrees(carto.longitude);
    const lat = Cesium.Math.toDegrees(carto.latitude);

    // If overlays are still building, queue the TX but still show the marker immediately.
    if (window.__buildBusy) {
      window.__queuedTxLonLat = [lon, lat];
      statusEl.textContent = 'TX set (queued) — overlays still loading…';
      // Fire-and-forget marker update
      setTxMarker(lon, lat);
      return;
    }

    requestRecompute(lon, lat);
  }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
}

async function initInteractiveMilestoneA() {
  // Guard: this function can be called multiple times (e.g., after rebuilding entities).
  // We want to re-render, but not stack event listeners / handlers endlessly.
  if (!_heatPoints) {
    _heatPoints = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());
  }

  // Heat overlay geometry is built once and then recolored on TX changes.
  // If the user clicks before this finishes, colors will apply after build completes.
  if (!_heatBuilt) {
    buildHeatOverlayOnce();
  }

  // Entities get wiped by viewer.entities.removeAll() during rebuilds.
  // Force marker to be recreated on the next setTxMarker().
  _txMarker = null;
  if (_txPlanePrim) { viewer.scene.primitives.remove(_txPlanePrim); _txPlanePrim = null; }
  _txPlaneGeomInstance = null;
  if (_txPlaneOutline) { viewer.entities.remove(_txPlaneOutline); _txPlaneOutline = null; }

  // Bind UI listeners only once.
  if (!window.__rfInteractiveBound) {
    window.__rfInteractiveBound = true;

    setupInteractiveClickHandler();

    document.getElementById('toggleHeat').addEventListener('change', function() {
      // No rebuild; just show/hide + recolor.
      updateHeatOverlayColors();
    });

    // Alpha slider is wired outside this block to avoid double listeners.
  }

  // Apply queued TX (if the user clicked while things were loading), otherwise recompute at current TX.
  const q = window.__queuedTxLonLat;
  if (q && q.length === 2) {
    window.__queuedTxLonLat = null;
    await requestRecompute(q[0], q[1]);
  } else {
    await requestRecompute(_txLon, _txLat);
  }
}
// === End interactive block ===
"""


def create_osm_rf_3d_html(
    api_key: str,
    center_lat: float,
    center_lon: float,
    buildings: List[BuildingFeature],
    forests: List[ForestFeature],
    grid_points: List[Tuple[float, float, float]],
    output_path: str,
    freq_mhz: float,
    tx_power_dbm: float,
    max_range_m: float,
    step_m: float,
    cesium_base_url: str,
    show_google_tiles: bool = True,
) -> str:
    tileset_url = f"https://tile.googleapis.com/v1/3dtiles/root.json?key={api_key}"

    buildings_json = json.dumps(
        [
            {
                "id": b.osm_id,
                "height_m": b.height_m,
                "centroid": [b.centroid_lon, b.centroid_lat],
                "ground": [b.ground_lon, b.ground_lat],
                "ground_cands": b.ground_cands,
                "rf_dbm": b.rf_dbm,
                "verts": b.vertices,
            }
            for b in buildings
        ]
    )
    forests_json = json.dumps(
        [
            {
                "id": f.osm_id,
                "canopy_m": f.canopy_m,
                "centroid": [f.centroid_lon, f.centroid_lat],
                "ground": [f.ground_lon, f.ground_lat],
                "rf_dbm": f.rf_dbm,
                "kind": f.kind,
                "verts": f.vertices,
            }
            for f in forests
        ]
    )
    grid_json = json.dumps(
        [{"lon": lon, "lat": lat, "rf_dbm": rsrp} for lon, lat, rsrp in grid_points]
    )

    # Important: avoid JS template literals with ${} inside a Python f-string.
    html = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>OSM-aware RF in 3D (Buildings + Forest)</title>
  <script>window.CESIUM_BASE_URL = {json.dumps(cesium_base_url)};</script>
  <script src="{cesium_base_url}Cesium.js"></script>
  <link href="{cesium_base_url}Widgets/widgets.css" rel="stylesheet" />
  <style>
    html, body, #cesiumContainer {{ width: 100%; height: 100%; margin: 0; padding: 0; overflow: hidden; }}
    .panel {{
      position: absolute; background: rgba(30,30,30,0.90); color: #fff;
      padding: 10px 12px; border-radius: 8px; font-family: Arial, sans-serif;
      font-size: 12px; z-index: 10; max-width: 360px;
    }}
    #info {{ top: 10px; left: 10px; }}
    #controls {{ top: 10px; right: 10px; }}
    .row {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin: 6px 0; }}
    input[type=range] {{ width: 180px; }}
    .small {{ opacity: 0.85; font-size: 11px; }}
    .badge {{ display:inline-block; padding: 2px 6px; border-radius: 999px; background: rgba(255,255,255,0.12); }}
    button {{ cursor:pointer; }}
  </style>
</head>
<body>
  <div id=\"cesiumContainer\"></div>

  <div id=\"info\" class=\"panel\">
    <div style=\"font-weight:700; font-size:13px;\">OSM-aware RF in 3D</div>
    <div class=\"small\">Center: ({center_lat:.6f}, {center_lon:.6f})</div>
    <div class=\"small\">Buildings: <span class=\"badge\">{len(buildings)}</span> &nbsp; Forest/veg: <span class=\"badge\">{len(forests)}</span></div>
    <div id=\"status\" class=\"small\" style=\"margin-top:6px;\">Loading…</div>
    <div id=\"picked\" class=\"small\" style=\"margin-top:6px;\"></div>
  </div>

  <div id=\"controls\" class=\"panel\">
    <div style=\"font-weight:700; font-size:13px;\">Controls</div>
    <div class=\"row\"><label><input id=\"toggleGoogle\" type=\"checkbox\" {('checked' if show_google_tiles else '')}/> Google 3D Tiles</label></div>
    <div class=\"row\"><label><input id=\"toggleBuildings\" type=\"checkbox\" checked /> OSM Buildings</label></div>
    <div class=\"row\"><label><input id=\"toggleForests\" type=\"checkbox\" checked /> OSM Forest/Vegetation</label></div>
    <div class=\"row\"><label><input id=\"toggleHeat\" type=\"checkbox\" checked /> Ground heat overlay</label></div>
    <div class=\"row\"><label><input id=\"toggleClick\" type=\"checkbox\" checked /> Click sets TX + recompute</label></div>
    <div class=\"row\">
      <label for=\"alpha\">Overlay alpha</label>
      <input id=\"alpha\" type=\"range\" min=\"0.05\" max=\"0.95\" step=\"0.05\" value=\"0.55\" />
    </div>
    <div class=\"small\">Tip: click anywhere to move the transmitter. Buildings/trees + heat overlay update.</div>
  </div>

  <script>
    const tilesetUrl = {json.dumps(tileset_url)};
    const buildings = {buildings_json};
    const forests = {forests_json};
    const grid = {grid_json};

const CENTER_LON = {center_lon};
const CENTER_LAT = {center_lat};
const RF_FREQ_MHZ = {freq_mhz};
const TX_POWER_DBM = {tx_power_dbm};
const MAX_RANGE_M = {max_range_m};
const GRID_STEP_M = {step_m};


    const statusEl = document.getElementById('status');
    const pickedEl = document.getElementById('picked');

    { _color_ramp_js() }

    // Cesium viewer (no default globe)
    const viewer = new Cesium.Viewer('cesiumContainer', {{
      imageryProvider: false,
      baseLayerPicker: false,
      geocoder: false,
      timeline: false,
      animation: false,
      sceneModePicker: true,
      navigationHelpButton: true,
      homeButton: true,
      globe: false,
    }});

    // Recommended for tile.googleapis.com concurrency
    Cesium.RequestScheduler.requestsByServer['tile.googleapis.com:443'] = 18;

    let googleTileset = null;
    let buildingEntities = [];
    let forestEntities = [];
    
    function setEntitiesVisible(ents, visible) {{
      for (const e of ents) e.show = visible;
    }}

    function clearEntities(ents) {{
      for (const e of ents) viewer.entities.remove(e);
      ents.length = 0;
    }}

    async function loadGoogleTiles() {{
      statusEl.textContent = 'Loading Google photorealistic tiles…';
      googleTileset = await Cesium.Cesium3DTileset.fromUrl(tilesetUrl, {{ showCreditsOnScreen: true }});
      viewer.scene.primitives.add(googleTileset);
      // Progress event: use loadProgress (tileLoadProgressEvent is not in Cesium 1.111)
      googleTileset.loadProgress.addEventListener(function(pending, processing) {{
        if (pending === 0 && processing === 0) {{
          statusEl.textContent = 'Tiles loaded. Building overlays…';
        }}
      }});
      return googleTileset;
    }}

    function flyToCenter() {{
      viewer.camera.setView({{
        destination: Cesium.Cartesian3.fromDegrees({center_lon}, {center_lat}, 1300),
        orientation: {{ heading: 0.0, pitch: Cesium.Math.toRadians(-40), roll: 0.0 }}
      }});
    }}

    async function clampHeightAt(lon, lat) {{
      // Clamp against the Google photorealistic tileset (or any loaded 3D Tiles).
      // NOTE: This often returns *roof* height when the (lon,lat) is inside a building footprint.
      const probe = Cesium.Cartesian3.fromDegrees(lon, lat, 200.0);
      try {{
        const clamped = await viewer.scene.clampToHeightMostDetailed([probe]);
        if (clamped && clamped[0]) {{
          const carto = Cesium.Cartographic.fromCartesian(clamped[0]);
          return carto.height;
        }}
      }} catch (e) {{
        // ignore
      }}
      return 0.0;
    }}

    function _dropDuplicateClosingVertex(verts) {{
      if (!verts || verts.length < 3) return verts || [];
      const a = verts[0];
      const b = verts[verts.length - 1];
      if (a && b && a.length === 2 && b.length === 2 && a[0] === b[0] && a[1] === b[1]) {{
        return verts.slice(0, verts.length - 1);
      }}
      return verts;
    }}

    function _outwardProbeLonLats(vertsIn, centroidLonLat, offsetM) {{
      // Build probe points just outside the footprint so clamp-to-height is more likely to hit ground/street
      // instead of the roof. Uses ENU frame at centroid.
      const verts = _dropDuplicateClosingVertex(vertsIn);
      if (!verts || verts.length < 3) return [];

      const cCarto = Cesium.Cartographic.fromDegrees(centroidLonLat[0], centroidLonLat[1], 0.0);
      const cPos = Cesium.Ellipsoid.WGS84.cartographicToCartesian(cCarto);
      const enu = Cesium.Transforms.eastNorthUpToFixedFrame(cPos);
      const inv = Cesium.Matrix4.inverse(enu, new Cesium.Matrix4());

      const probes = [];
      const n = verts.length;
      const maxSamples = 6;
      const step = Math.max(1, Math.floor(n / maxSamples));

      for (let i = 0; i < n; i += step) {{
        const v = verts[i];
        const vCarto = Cesium.Cartographic.fromDegrees(v[0], v[1], 0.0);
        const vPos = Cesium.Ellipsoid.WGS84.cartographicToCartesian(vCarto);
        const vLocal = Cesium.Matrix4.multiplyByPoint(inv, vPos, new Cesium.Cartesian3());

        // Direction from centroid (0,0) to vertex in local EN plane
        const dx = vLocal.x;
        const dy = vLocal.y;
        const len = Math.hypot(dx, dy);
        if (!isFinite(len) || len < 0.01) continue;

        const ux = dx / len;
        const uy = dy / len;

        // Push outward beyond the vertex by offsetM meters
        const outLocal = new Cesium.Cartesian3(dx + ux * offsetM, dy + uy * offsetM, 0.0);
        const outWorld = Cesium.Matrix4.multiplyByPoint(enu, outLocal, new Cesium.Cartesian3());
        const outCarto = Cesium.Cartographic.fromCartesian(outWorld);

        probes.push([
          Cesium.Math.toDegrees(outCarto.longitude),
          Cesium.Math.toDegrees(outCarto.latitude),
        ]);
      }}

      return probes;
    }}

    function _pointInPoly(lon, lat, vertsIn) {{
      const verts = _dropDuplicateClosingVertex(vertsIn);
      if (!verts || verts.length < 3) return false;
      let inside = false;
      for (let i = 0, j = verts.length - 1; i < verts.length; j = i++) {{
        const xi = verts[i][0], yi = verts[i][1];
        const xj = verts[j][0], yj = verts[j][1];
        // Ray-cast intersection test
        const denom = (yj - yi);
        const xInt = (xj - xi) * (lat - yi) / (Math.abs(denom) < 1e-12 ? 1e-12 : denom) + xi;
        const intersect = ((yi > lat) !== (yj > lat)) && (lon < xInt);
        if (intersect) inside = !inside;
      }}
      return inside;
    }}

    function _isLonLatInsideAnyBuilding(lon, lat, selfOsmId) {{
      for (let i = 0; i < buildings.length; i++) {{
        const b = buildings[i];
        if (selfOsmId && b.id === selfOsmId) continue;
        if (_pointInPoly(lon, lat, b.verts)) return true;
      }}
      return false;
    }}

    function _radialProbeLonLats(centroidLonLat, radiusM, num) {{
      const probes = [];
      const lon0 = centroidLonLat[0];
      const lat0 = centroidLonLat[1];
      const metersPerDegLat = 111320.0;
      const cosLat = Math.cos(Cesium.Math.toRadians(lat0));
      const metersPerDegLon = metersPerDegLat * Math.max(0.1, cosLat);
      for (let k = 0; k < num; k++) {{
        const ang = (k / num) * Math.PI * 2.0;
        const east = Math.cos(ang) * radiusM;
        const north = Math.sin(ang) * radiusM;
        const lon = lon0 + (east / metersPerDegLon);
        const lat = lat0 + (north / metersPerDegLat);
        probes.push([lon, lat]);
      }}
      return probes;
    }}

    async function estimateBaseHeightForFeature(feature, objectHeightM) {{
      // Goal: estimate true ground/base elevation for an OSM footprint.
      // With Google photorealistic tiles, clamping inside a footprint often hits ROOFS.
      // We therefore:
      //  1) try many "ground candidates" (OSM road samples + radial probes outside footprints)
      //  2) take the LOWEST clamped height (ground tends to be the minimum)
      //  3) if everything still hits roofs, fall back to (roofH - objectHeightM)
      const centroid = feature.centroid;
      const selfId = feature.id || null;

      // roof-ish height at centroid (may be null if not loaded yet)
      const roofH = await clampHeightAt(centroid[0], centroid[1]);

      let candidates = [];
      if (feature.ground_cands && feature.ground_cands.length) {{
        // Keep only candidates that are not inside any building polygon (avoids roof hits)
        for (const p of feature.ground_cands) {{
          const lon = p[0], lat = p[1];
          if (_pointInPoly(lon, lat, feature.verts)) continue; // shouldn't happen, but be safe
          if (_isLonLatInsideAnyBuilding(lon, lat, selfId)) continue;
          candidates.push(p);
        }}
      }} else if (feature.ground && feature.ground.length === 2) {{
        candidates.push(feature.ground);
      }}

      // Add radial probes at increasing radii; reject anything inside self footprint or other buildings.
      const radii = [25, 45, 75, 110, 160];
      for (let r of radii) {{
        const probes = _radialProbeLonLats(centroid, r, 14);
        for (let p of probes) {{
          const lon = p[0], lat = p[1];
          if (_pointInPoly(lon, lat, feature.verts)) continue; // outside self
          if (_isLonLatInsideAnyBuilding(lon, lat, selfId)) continue; // avoid other roofs
          candidates.push(p);
        }}
      }}

      // Always include centroid last as a fallback (may hit roof).
      candidates.push(centroid);

      // Clamp all candidates; choose the minimum height
      let best = null;
      for (let i = 0; i < candidates.length; i++) {{
        const p = candidates[i];
        const h = await clampHeightAt(p[0], p[1]);
        if (h == null || !isFinite(h)) continue;
        if (best == null || h < best) best = h;
      }}

      // If we couldn't clamp anything, give up to a stable fallback.
      if (best == null) {{
        if (roofH != null && isFinite(roofH)) return roofH - objectHeightM;
        return 0.0;
      }}

      // If the "best" is suspiciously close to the roof, it means all candidates hit roofs.
      // In that case use roofH - objectHeightM (OSM height-based grounding).
      if (roofH != null && isFinite(roofH) && objectHeightM != null && isFinite(objectHeightM) && objectHeightM > 1.0) {{
        // treat within 2m of roof as roof-hit
        if (best > roofH - 2.0) {{
          return roofH - objectHeightM;
        }}
        // Also don't allow base to be too close to roof (guards against mis-clamps)
        const derived = roofH - objectHeightM;
        // If derived is significantly lower than best, prefer derived (we want ground).
        if (derived < best - 0.5) {{
          return derived;
        }}
        // Otherwise keep the best ground candidate, but cap it slightly below roof.
        return Math.min(best, roofH - 0.75);
      }}

      // No roofH available; just use minimum candidate height.
      return best;
    }}

async function buildBuildingsAndForests() {{
      const alpha = parseFloat(document.getElementById('alpha').value);
      const BASE_BIAS_M = -0.75; // sink volumes slightly to avoid z-fighting

      window.__buildBusy = true;

      async function clampCentroidHeights(features) {{
        const out = new Array(features.length).fill(0.0);
        if (!features || features.length === 0) return out;

        const probes = [];
        for (let i = 0; i < features.length; i++) {{
          const f = features[i];
          probes.push(Cesium.Cartesian3.fromDegrees(f.centroid[0], f.centroid[1], 200.0));
        }}

        const BATCH = 400;
        for (let i = 0; i < probes.length; i += BATCH) {{
          const slice = probes.slice(i, i + BATCH);
          let clamped = null;
          try {{
            const clampPromise = viewer.scene.clampToHeightMostDetailed(slice);
            clamped = await Promise.race([
              clampPromise,
              new Promise((resolve) => setTimeout(() => resolve(null), 800))
            ]);
          }} catch (e) {{
            clamped = null;
          }}
          for (let j = 0; j < slice.length; j++) {{
            const k = i + j;
            const pos = (clamped && clamped[j]) ? clamped[j] : null;
            if (!pos) continue;
            const carto = Cesium.Cartographic.fromCartesian(pos);
            out[k] = carto.height;
          }}
        }}
        return out;
      }}

      // Clamp centroids once (batched) to avoid thousands of async clamps.
      statusEl.textContent = 'Clamping feature centroids (batched)…';
      const bHeights = await clampCentroidHeights(buildings);
      const fHeights = await clampCentroidHeights(forests);

      // Buildings
      statusEl.textContent = 'Creating OSM buildings…';
      for (let i = 0; i < buildings.length; i++) {{
        const b = buildings[i];
        const degArray = [];
        for (const p of b.verts) {{
          degArray.push(p[0], p[1]);
        }}

        let baseH = 0.0;
        const roofH = bHeights[i];
        if (roofH && isFinite(roofH) && b.height_m && isFinite(b.height_m)) {{
          baseH = Math.max(0.0, roofH - b.height_m + BASE_BIAS_M);
        }}

        const color = colorForRsrp(b.rf_dbm, alpha);
        const ent = viewer.entities.add({{
          name: 'building:' + b.id,
          properties: {{ rf_dbm: b.rf_dbm, osm_id: b.id, kind: 'building', height_m: b.height_m }},
          polygon: {{
            hierarchy: Cesium.Cartesian3.fromDegreesArray(degArray),
            height: baseH,
            extrudedHeight: baseH + b.height_m,
            material: color,
            outline: false,
          }}
        }});
        buildingEntities.push(ent);
      }}

      // Forests
      statusEl.textContent = 'Creating vegetation volumes…';
      for (let i = 0; i < forests.length; i++) {{
        const f = forests[i];
        const degArray = [];
        for (const p of f.verts) {{
          degArray.push(p[0], p[1]);
        }}

        let baseH = 0.0;
        const roofH = fHeights[i];
        if (roofH && isFinite(roofH) && f.canopy_m && isFinite(f.canopy_m)) {{
          baseH = Math.max(0.0, roofH - f.canopy_m + BASE_BIAS_M);
        }}

        const rfColor = colorForRsrp(f.rf_dbm, alpha);
        const green = new Cesium.Color(0.1, 0.8, 0.2, alpha);
        const mix = Cesium.Color.lerp(green, rfColor, 0.65, new Cesium.Color());

        const ent = viewer.entities.add({{
          name: 'forest:' + f.id,
          properties: {{ rf_dbm: f.rf_dbm, osm_id: f.id, kind: f.kind, canopy_m: f.canopy_m }},
          polygon: {{
            hierarchy: Cesium.Cartesian3.fromDegreesArray(degArray),
            height: baseH,
            extrudedHeight: baseH + f.canopy_m,
            material: mix,
            outline: false,
          }}
        }});
        forestEntities.push(ent);
      }}

      window.__buildBusy = false;

      // Apply queued TX (if user clicked while loading), otherwise refresh current TX.
      const q = window.__queuedTxLonLat;
      if (q && q.length === 2) {{
        window.__queuedTxLonLat = null;
        await requestRecompute(q[0], q[1]);
      }} else {{
        await requestRecompute(_txLon, _txLat);
      }}

      statusEl.textContent = 'Ready.';
    }}

    async function rebuildAll() {{
      clearEntities(buildingEntities);
      clearEntities(forestEntities);
      await buildBuildingsAndForests();
      await initInteractiveMilestoneA();
    }}
    // Picking + interactive TX click
    {_interactive_click_js()}

    // UI wiring
    document.getElementById('toggleGoogle').addEventListener('change', function() {{
      if (!googleTileset) return;
      googleTileset.show = this.checked;
    }});
    document.getElementById('toggleBuildings').addEventListener('change', function() {{
      setEntitiesVisible(buildingEntities, this.checked);
    }});
    document.getElementById('toggleForests').addEventListener('change', function() {{
      setEntitiesVisible(forestEntities, this.checked);
    }});
    document.getElementById('alpha').addEventListener('input', function() {{
      // Recolor using current TX + alpha without rebuilding geometry.
      requestRecompute(_txLon, _txLat);
    }});

    (async function main() {{
      flyToCenter();
      // Bind click handler + create heat overlay immediately so clicks work even while overlays load.
      await initInteractiveMilestoneA();
      const tiles = await loadGoogleTiles();
      // Ensure toggle matches initial state
      tiles.show = document.getElementById('toggleGoogle').checked;
      // Wait a beat so clampToHeight works better
      setTimeout(async function() {{
        await buildBuildingsAndForests();
      }}, 800);
    }})();
  </script>
</body>
</html>
"""

    out_dir = os.path.dirname(output_path) or "."
    os.makedirs(out_dir, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    return output_path


# -------------------------
# Main
# -------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="OSM-aware RF 3D demo (Buildings + Forest)")
    parser.add_argument("--api-key", required=True, help="Google Maps Platform API key (Map Tiles API enabled)")
    parser.add_argument("--lat", type=float, default=37.7749, help="Center latitude")
    parser.add_argument("--lon", type=float, default=-122.4194, help="Center longitude")

    parser.add_argument("--freq-mhz", type=float, default=3500.0, help="TX frequency (MHz)")
    parser.add_argument("--tx-power-dbm", type=float, default=43.0, help="TX power (dBm)")
    parser.add_argument("--max-range-m", type=float, default=500.0, help="Coverage max range (m)")
    parser.add_argument("--step-m", type=float, default=5.0, help="Coverage radial step (m)")

    parser.add_argument("--osm-radius-m", type=float, default=None, help="OSM fetch radius (m); default=max-range+100")
    parser.add_argument("--road-sample-step-m", type=float, default=25.0, help="Road sampling step for ground anchoring (m)")
    parser.add_argument("--default-building-height-m", type=float, default=12.0, help="Fallback building height when OSM lacks height/levels")
    parser.add_argument("--default-canopy-height-m", type=float, default=15.0, help="Fallback canopy height for forests/parks")

    parser.add_argument("--cache-dir", default="./cache/google_maps_3d", help="Cache directory for Google tileset root.json")
    parser.add_argument("--no-cache", action="store_true", help="Disable Google tileset caching")
    parser.add_argument("--output", default="./public/osm_rf_3d.html", help="Output HTML (recommended under your web root)")
    parser.add_argument("--cesium-dir", default=None, help="Directory to serve Cesium static assets from (default: <output_dir>/Cesium)")
    parser.add_argument("--no-copy-cesium", action="store_true", help="Do not auto-copy Cesium from node_modules into --cesium-dir")
    parser.add_argument("--no-google", action="store_true", help="Start with Google tiles hidden (OSM overlay only)")

    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("OSM-aware RF in 3D (Milestone A)")
    logger.info("=" * 60)
    logger.info(f"Center: ({args.lat}, {args.lon})")
    logger.info(f"RF: freq={args.freq_mhz} MHz, tx={args.tx_power_dbm} dBm")
    logger.info(f"Grid: max_range={args.max_range_m}m, step={args.step_m}m")

    # 1) Validate Google tileset fetch (helps catch API key issues early)
    use_cache = not args.no_cache
    try:
        _ = fetch_tileset_with_cache(args.api_key, args.cache_dir, use_cache)
        logger.info("✓ Google tileset root fetch OK")
    except Exception as e:
        logger.error(f"Google tileset root fetch failed: {e}")
        return 1

    # 2) Fetch OSM data
    tx = LatLon(lat=args.lat, lon=args.lon)
    osm_radius_m = float(args.osm_radius_m) if args.osm_radius_m is not None else float(args.max_range_m + 100.0)
    osm = OSMMapProvider(cache_radius_m=osm_radius_m)
    osm.prefetch_all_data(tx, osm_radius_m)
    buildings_raw: List[dict] = getattr(osm, "_cached_buildings", [])
    landuse_raw: List[dict] = getattr(osm, "_cached_landuse", [])
    logger.info(f"✓ OSM fetched: buildings={len(buildings_raw)}, landuse/natural={len(landuse_raw)}")

    # Road samples (OSM highways) for ground anchoring
    road_samples = fetch_osm_road_samples(args.lat, args.lon, osm_radius_m, step_m=float(args.road_sample_step_m))
    logger.info(f"✓ OSM road samples: {len(road_samples)}")


    # 3) Run RF pipeline (this is the 'interaction' part: buildings/trees attenuate)
    rf_params = RFParams(
        freq_mhz=float(args.freq_mhz),
        tx_power_dbm=float(args.tx_power_dbm),
        max_range_m=float(args.max_range_m),
        step_m=float(args.step_m),
    )

    world = build_world_model(tx=tx, rf_params=rf_params, views=[], map_provider=osm)
    grid = compute_attenuation_grid(world)
    logger.info(f"✓ RF computed: cells={len(grid.cell_lat)}")

    grid_lonlat_rsrp: List[Tuple[float, float, float]] = list(zip(grid.cell_lon, grid.cell_lat, grid.rsrp_dbm))
    # For now we don't render a full grid overlay in 3D; keep this empty.
    grid_points: List[Tuple[float, float, float]] = []

    # 4) Build building + forest features with sampled RF values
    buildings: List[BuildingFeature] = []
    for b in buildings_raw:
        geom = b.get("geometry", [])
        pts = _polygon_vertices_lonlat(geom)
        if len(pts) < 3:
            continue
        cx, cy = _centroid_lonlat(pts)
        height_m = _parse_height_m(b.get("tags", {}) or {}, default_m=float(args.default_building_height_m))
        rf_dbm = _nearest_rsrp_dbm(grid_lonlat_rsrp, cx, cy, origin_lon=args.lon, origin_lat=args.lat)

        cands = k_nearest_road_candidates(cx, cy, pts, road_samples, origin_lon=args.lon, origin_lat=args.lat, k=8, max_dist_m=220.0)
        if cands:
            ground_lon, ground_lat = cands[0]
        else:
            ground_lon, ground_lat = cx, cy

        buildings.append(
            BuildingFeature(
                osm_id=int(b.get("id", 0) or 0),
                height_m=float(height_m),
                centroid_lon=float(cx),
                centroid_lat=float(cy),
                vertices=pts,
                rf_dbm=float(rf_dbm),
                ground_lon=float(ground_lon),
                ground_lat=float(ground_lat),
                ground_cands=cands,
            )
        )

    forests: List[ForestFeature] = []
    for a in landuse_raw:
        tags = a.get("tags", {}) or {}
        if not _is_forest_like(tags):
            continue
        geom = a.get("geometry", [])
        pts = _polygon_vertices_lonlat(geom)
        if len(pts) < 3:
            continue
        cx, cy = _centroid_lonlat(pts)

        canopy_m = float(args.default_canopy_height_m)
        rf_dbm = _nearest_rsrp_dbm(grid_lonlat_rsrp, cx, cy, origin_lon=args.lon, origin_lat=args.lat)

        kind = "forest"
        if tags.get("natural") in ("wood", "tree_row"):
            kind = str(tags.get("natural"))
        if tags.get("landuse") in ("forest", "meadow", "grass", "orchard"):
            kind = str(tags.get("landuse"))

        forests.append(
            ForestFeature(
                osm_id=int(a.get("id", 0) or 0),
                canopy_m=float(canopy_m),
                centroid_lon=float(cx),
                centroid_lat=float(cy),
                vertices=pts,
                rf_dbm=float(rf_dbm),
                kind=kind,
                ground_lon=float(cx),
                ground_lat=float(cy),
            )
        )



    # 5) Cesium self-host: ensure static assets are available and compute base URL relative to the HTML file
    out_dir = os.path.dirname(args.output) or "."
    cesium_dir = args.cesium_dir or os.path.join(out_dir, "Cesium")
    ensure_cesium_static_dir(cesium_dir, copy_from_node_modules=(not args.no_copy_cesium))

    # Make a browser-friendly relative URL (with trailing slash)
    rel = os.path.relpath(cesium_dir, out_dir).replace(os.sep, "/")
    if rel == ".":
        rel = "Cesium"
    if not (rel.startswith(".") or rel.startswith("/")):
        rel = "./" + rel
    if not rel.endswith("/"):
        rel += "/"
    cesium_base_url = rel
    out = create_osm_rf_3d_html(
        api_key=args.api_key,
        center_lat=args.lat,
        center_lon=args.lon,
        buildings=buildings,
        forests=forests,
        grid_points=grid_points,
        output_path=args.output,
        freq_mhz=args.freq_mhz,
        tx_power_dbm=args.tx_power_dbm,
        max_range_m=args.max_range_m,
        step_m=args.step_m,
        cesium_base_url=cesium_base_url,
        show_google_tiles=not args.no_google,
    )

    logger.info("=" * 60)
    logger.info("✓ HTML generated")
    logger.info(out)
    logger.info("Serve with: python3 -m http.server 8000")
    logger.info(f"Open: http://localhost:8000/{str(Path(out)).replace(os.sep, '/').lstrip('./')}")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())