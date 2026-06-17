"""OSM-based MapProvider using Overpass API for building footprints and landcover."""

import logging
import math
import time
from typing import Any, List, Optional, Tuple, Dict

import requests

from ..pipeline.schemas import LatLon, MaterialType
from .physical_spanning import MapProvider
from .spatial_index import QuadtreeIndex, BoundingBox, compute_bbox_from_polygon
from .osm_cache import load_cached_osm_data, save_cached_osm_data

logger = logging.getLogger(__name__)

# Overpass API endpoints (public instances; tried in order)
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# Large planning regions are partitioned into exact, non-simplified Overpass
# bounding boxes. A 20 km radius becomes a 4x4 set of 10 km-wide boxes.
OSM_MAX_TILE_HALF_SIZE_M = 5000.0
OSM_TILED_PREFETCH_THRESHOLD_M = 7500.0

OVERPASS_HEADERS = {
    "User-Agent": "agentic-rf-planner/1.0 (+local)",
    "Accept": "application/json, text/plain, */*",
}


def _post_overpass(query: str, *, timeout: int, context: str) -> dict:
    """Post an Overpass query with endpoint fallback + a short retry."""
    last_exc: Optional[Exception] = None
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        for url in OVERPASS_URLS:
            try:
                response = requests.post(
                    url,
                    data={"data": query},
                    headers=OVERPASS_HEADERS,
                    timeout=timeout,
                )
                response.raise_for_status()
                return response.json()
            except requests.exceptions.RequestException as exc:
                logger.warning(
                    "%s failed via %s (attempt %s/%s): %s",
                    context,
                    url,
                    attempt,
                    max_attempts,
                    exc,
                )
                last_exc = exc
        if attempt < max_attempts:
            time.sleep(0.6)
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{context} failed without an exception")


def _estimate_osm_height_m(tags: dict) -> Optional[float]:
    """Best-effort height estimate from OSM tags.

    Returns None if no reasonable estimate can be derived.
    """
    if not isinstance(tags, dict):
        return None

    # Explicit height tags.
    for k in ("height", "building:height"):
        h = tags.get(k)
        if not h:
            continue
        try:
            token = str(h).strip().split()[0]
            token = token.replace("m", "")
            val = float(token)
            if val > 0.5:
                return val
        except Exception:
            pass

    # Floors/levels (roughly 3m per level).
    for k in ("building:levels", "levels"):
        lv = tags.get(k)
        if not lv:
            continue
        try:
            token = str(lv).strip().split()[0]
            val = float(token)
            if val > 0:
                return val * 3.0
        except Exception:
            pass

    # Heuristics based on building type.
    btype = str(tags.get("building", "") or "").lower().strip()
    # A very common pattern is building=yes with no further typing.
    # Treat it as a low/mid-rise default so height-sliced modes respond to TX height.
    if btype in ("yes", "building"):
        return 12.0
    if btype in ("house", "detached", "bungalow", "cabin"):
        return 6.0
    if btype in ("apartments", "residential", "terrace"):
        return 12.0
    if btype in ("commercial", "retail", "office"):
        return 15.0
    if btype in ("industrial", "warehouse"):
        return 10.0
    if btype in ("church", "cathedral"):
        return 20.0
    if btype in ("tower", "highrise"):
        return 40.0

    return None


def _offset_latlon(center: LatLon, east_m: float, north_m: float) -> LatLon:
    """Offset a point in a local tangent-plane approximation."""

    lat = center.lat + north_m / 111_320.0
    lon_scale = max(1.0e-6, 111_320.0 * math.cos(math.radians(center.lat)))
    lon = center.lon + east_m / lon_scale
    return LatLon(lat=lat, lon=lon)


def _tile_centers_for_square(center: LatLon, radius_m: float) -> List[Tuple[LatLon, float]]:
    """Return square tiles whose union covers the requested radius bounding square.

    The boxes are adjacent and do not simplify or resample OSM geometry. Fetching
    the complete bounding square intentionally includes a small amount of data
    outside the circular RF service area so no edge features are missed.
    """

    radius = max(1.0, float(radius_m))
    tiles_per_axis = max(1, int(math.ceil(radius / OSM_MAX_TILE_HALF_SIZE_M)))
    cell_width_m = (2.0 * radius) / tiles_per_axis
    half_size_m = cell_width_m / 2.0
    out: List[Tuple[LatLon, float]] = []
    for yi in range(tiles_per_axis):
        north_m = -radius + half_size_m + yi * cell_width_m
        for xi in range(tiles_per_axis):
            east_m = -radius + half_size_m + xi * cell_width_m
            out.append((_offset_latlon(center, east_m=east_m, north_m=north_m), half_size_m))
    return out


def _coord_key(point: dict) -> Tuple[float, float]:
    return (round(float(point.get("lat", 0.0)), 7), round(float(point.get("lon", 0.0)), 7))


def _stitch_relation_rings(segments: List[List[dict]]) -> List[List[dict]]:
    """Stitch Overpass relation-member geometry into closed outer rings."""

    remaining = [list(seg) for seg in segments if len(seg) >= 2]
    rings: List[List[dict]] = []
    while remaining:
        ring = remaining.pop(0)
        progressed = True
        while progressed and remaining and _coord_key(ring[0]) != _coord_key(ring[-1]):
            progressed = False
            head = _coord_key(ring[0])
            tail = _coord_key(ring[-1])
            for idx, seg in enumerate(remaining):
                seg_head = _coord_key(seg[0])
                seg_tail = _coord_key(seg[-1])
                if tail == seg_head:
                    ring.extend(seg[1:])
                elif tail == seg_tail:
                    ring.extend(list(reversed(seg[:-1])))
                elif head == seg_tail:
                    ring = seg[:-1] + ring
                elif head == seg_head:
                    ring = list(reversed(seg[1:])) + ring
                else:
                    continue
                remaining.pop(idx)
                progressed = True
                break
        if len(ring) >= 3:
            if _coord_key(ring[0]) != _coord_key(ring[-1]):
                ring.append(dict(ring[0]))
            rings.append(ring)
    return rings


def _element_polygons(element: dict) -> List[Tuple[int, List[dict]]]:
    """Extract way or multipolygon outer rings with stable integer IDs."""

    element_type = str(element.get("type") or "")
    element_id = int(element.get("id") or 0)
    geometry = element.get("geometry")
    if element_type == "way" and isinstance(geometry, list) and len(geometry) >= 3:
        return [(element_id, geometry)]
    if element_type != "relation":
        return []

    outer_segments: List[List[dict]] = []
    for member in element.get("members", []) or []:
        role = str(member.get("role") or "outer").strip().lower()
        member_geometry = member.get("geometry")
        if role in ("", "outer") and isinstance(member_geometry, list) and len(member_geometry) >= 2:
            outer_segments.append(member_geometry)
    rings = _stitch_relation_rings(outer_segments)
    return [(-(element_id * 1000 + idx + 1), ring) for idx, ring in enumerate(rings)]


def _parse_overpass_elements(data: dict) -> Tuple[List[dict], List[dict]]:
    buildings: List[dict] = []
    landuse: List[dict] = []
    for element in data.get("elements", []) or []:
        tags = element.get("tags", {}) or {}
        polygons = _element_polygons(element)
        for feature_id, geometry in polygons:
            base = {
                "id": feature_id,
                "osm_id": element.get("id"),
                "osm_type": element.get("type"),
                "tags": tags,
                "geometry": geometry,
            }
            if "building" in tags:
                building = dict(base)
                building["material"] = _extract_building_material(building)
                building["height_m"] = _estimate_osm_height_m(tags)
                buildings.append(building)
            if "landuse" in tags or "natural" in tags:
                landuse.append(dict(base))
    return buildings, landuse


def _dedupe_osm_features(features: List[dict]) -> List[dict]:
    out: List[dict] = []
    seen: set[Tuple[Any, ...]] = set()
    for feature in features:
        geometry = feature.get("geometry") or []
        first = _coord_key(geometry[0]) if geometry else (0.0, 0.0)
        key = (
            feature.get("osm_type"),
            feature.get("osm_id", feature.get("id")),
            feature.get("id"),
            first,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(feature)
    return out


class OSMMapProvider(MapProvider):
    """
    Real MapProvider using OSM Overpass API.
    
    Fetches building footprints and landcover data to:
    - Count buildings along LOS rays
    - Detect forest/vegetation
    - Classify clutter (open/suburban/urban/dense_urban)
    """

    def __init__(self, cache_radius_m: float = 1000.0, slice_height_m: Optional[float] = None):
        """
        Args:
            cache_radius_m: Radius around last query to cache (simple in-memory cache)
        """
        self.cache_radius_m = cache_radius_m
        # Optional height slice (meters above local ground).
        # If set, buildings/vegetation whose estimated height is BELOW this value
        # are ignored for obstacle queries. This enables "3D (OSM only)" behavior
        # without changing the fast 2.5D ray-march model.
        self.slice_height_m = float(slice_height_m) if slice_height_m is not None else None
        if self.slice_height_m is not None:
            logger.info(f"OSMMapProvider: enabled height slice at {self.slice_height_m:.2f} m")
        self._cache_center: Optional[LatLon] = None
        self._cached_buildings: List[dict] = []
        self._cached_landuse: List[dict] = []
        self._prefetched: bool = False  # Track if we've done bulk prefetch
        self._prefetch_radius_m: float = 0.0  # Radius of prefetched data
        
        # Phase 2: Spatial indexing
        self._building_quadtree: Optional[QuadtreeIndex] = None
        self._landuse_quadtree: Optional[QuadtreeIndex] = None
        self._building_bboxes: Dict[int, BoundingBox] = {}  # polygon_id -> bbox
        self._landuse_bboxes: Dict[int, BoundingBox] = {}  # polygon_id -> bbox
        self._building_by_id: Dict[int, dict] = {}  # polygon_id -> building dict (for fast lookup)
        self._landuse_by_id: Dict[int, dict] = {}  # polygon_id -> landuse dict (for fast lookup)

    def prefetch_all_data(self, center: LatLon, radius_m: float) -> None:
        """
        Pre-fetch ALL buildings and landuse data in a large radius.
        This should be called ONCE before processing many cells.
        
        Args:
            center: Center point for prefetch
            radius_m: Radius to prefetch (should cover all cells)
        """
        logger.info("="*60)
        logger.info(f"OSM PREFETCH CALLED: center=({center.lat:.6f}, {center.lon:.6f}), radius={radius_m}m")
        logger.info(f"  _prefetched: {self._prefetched}")
        logger.info(f"  _cache_center: {self._cache_center}")
        logger.info(f"  _prefetch_radius_m: {self._prefetch_radius_m}")
        if self._cache_center:
            dist = _distance_m(center, self._cache_center)
            logger.info(f"  Distance from cache center: {dist:.1f}m")
        logger.info("="*60)
        
        if self._prefetched and self._cache_center and _distance_m(center, self._cache_center) < 100.0 and radius_m <= self._prefetch_radius_m:
            logger.info(f"Using existing prefetched data (radius={self._prefetch_radius_m}m)")
            logger.info(f"  Existing cache: {len(self._cached_buildings)} buildings, {len(self._cached_landuse)} landuse")
            if len(self._cached_buildings) == 0:
                logger.warning(f"⚠ WARNING: Existing cache has 0 buildings! This is likely the problem!")
                logger.warning(f"  The cache was likely populated with empty data from a previous query")
                logger.warning(f"  Solution: Clear the OSM cache using POST /api/clear-cache with clear_osm=true")
                logger.warning(f"  OR: The location truly has no OSM building data")
                logger.warning(f"  Forcing re-fetch to verify...")
                # Force re-fetch even if cache exists (to verify if location has data)
                self._prefetched = False
                self._cache_center = None
            else:
                # Cache has data, use it
                return
        
        logger.info(f"Pre-fetching OSM data for radius {radius_m}m around ({center.lat}, {center.lon})...")
        
        # Try to load from persistent cache first
        cached_buildings = load_cached_osm_data(center, radius_m, 'buildings')
        cached_landuse = load_cached_osm_data(center, radius_m, 'landuse')
        
        # Handle cache hits/misses
        cache_hit_buildings = cached_buildings is not None
        cache_hit_landuse = cached_landuse is not None

        if radius_m > OSM_TILED_PREFETCH_THRESHOLD_M and not (cache_hit_buildings and cache_hit_landuse):
            logger.info(
                "Large OSM region (%.0f m): using exact tiled prefetch instead of one giant Overpass bbox",
                radius_m,
            )
            tiled = self._fetch_tiled_osm_data(center, radius_m)
            if tiled is not None:
                cached_buildings, cached_landuse = tiled
                cache_hit_buildings = True
                cache_hit_landuse = True

        if cache_hit_buildings and cache_hit_landuse:
            logger.info(f"✓ Cache HIT (both): Loaded from persistent cache: {len(cached_buildings)} buildings, {len(cached_landuse)} landuse areas")
            self._cached_buildings = cached_buildings
            self._cached_landuse = cached_landuse
            if len(cached_buildings) == 0:
                logger.warning(f"⚠ WARNING: Cache returned 0 buildings - this may indicate:")
                logger.warning(f"  1. Previous fetch returned 0 buildings (location has no OSM data)")
                logger.warning(f"  2. Cache file is corrupted or empty")
                logger.warning(f"  Consider clearing cache and re-fetching")
        else:
            # Partial or full cache miss - fetch missing data
            if not cache_hit_buildings and not cache_hit_landuse:
                logger.info("  Cache miss (both) - fetching from OSM API...")
            elif not cache_hit_buildings:
                logger.info("  Cache miss (buildings only) - fetching buildings from OSM API...")
            else:
                logger.info("  Cache miss (landuse only) - fetching landuse from OSM API...")
            
            # Fetch buildings if not cached
            if not cache_hit_buildings:
                fetched_buildings = self._fetch_buildings_from_osm(center, radius_m)
                if fetched_buildings is None:
                    logger.warning("  Building fetch failed; continuing without seeding an empty cache entry")
                    self._cached_buildings = []
                else:
                    self._cached_buildings = fetched_buildings
                    logger.info(f"  Pre-fetched {len(self._cached_buildings)} buildings")
                    # Save to persistent cache (even if empty - this is valid if location has no buildings)
                    # But warn if we got 0 buildings from a fresh fetch
                    if len(self._cached_buildings) == 0:
                        logger.warning(f"⚠ WARNING: Fresh OSM API query returned 0 buildings")
                        logger.warning(f"  This means the location ({center.lat:.6f}, {center.lon:.6f}) has no building data in OSM")
                        logger.warning(f"  Check https://www.openstreetmap.org/ to verify building coverage at this location")
                    save_cached_osm_data(center, radius_m, 'buildings', self._cached_buildings)
            else:
                self._cached_buildings = cached_buildings
                logger.info(f"  Using cached buildings: {len(self._cached_buildings)}")
            
            # Fetch landuse if not cached
            if not cache_hit_landuse:
                fetched_landuse = self._fetch_landuse_from_osm(center, radius_m)
                if fetched_landuse is None:
                    logger.warning("  Landuse fetch failed; continuing without seeding an empty cache entry")
                    self._cached_landuse = []
                else:
                    self._cached_landuse = fetched_landuse
                    logger.info(f"  Pre-fetched {len(self._cached_landuse)} landuse areas")
                    # Save to persistent cache
                    save_cached_osm_data(center, radius_m, 'landuse', self._cached_landuse)
            else:
                self._cached_landuse = cached_landuse
                logger.info(f"  Using cached landuse: {len(self._cached_landuse)}")
        
        # Phase 2: Build spatial indexes (quadtree) for fast queries
        logger.info("  Building spatial indexes...")
        self._build_spatial_indexes(center, radius_m)
        
        # Update cache metadata
        self._cache_center = center
        self._prefetch_radius_m = radius_m
        self._prefetched = True
        logger.info("✓ Pre-fetch complete, all subsequent queries will use cache and spatial index")
    
    def _build_spatial_indexes(self, center: LatLon, radius_m: float) -> None:
        """
        Build quadtree spatial indexes for buildings and landuse.
        
        This dramatically speeds up ray-polygon intersection queries.
        """
        # Compute overall bounding box for the quadtree
        # _bbox_around_point returns "min_lat,min_lon,max_lat,max_lon"
        bbox_str = _bbox_around_point(center.lat, center.lon, radius_m)
        parts = bbox_str.split(',')
        overall_bbox = BoundingBox(
            min_lat=float(parts[0]),
            min_lon=float(parts[1]),
            max_lat=float(parts[2]),
            max_lon=float(parts[3])
        )
        
        # Build building quadtree
        self._building_quadtree = QuadtreeIndex(overall_bbox, max_depth=8, max_items=10)
        self._building_bboxes = {}
        self._building_by_id = {}
        
        for building in self._cached_buildings:
            building_id = building.get("id", 0)
            # Ensure height is available (may be missing for cached payloads).
            if "height_m" not in building:
                try:
                    building["height_m"] = _estimate_osm_height_m(building.get("tags", {}))
                except Exception:
                    building["height_m"] = None
            geometry = building.get("geometry", [])
            bbox = compute_bbox_from_polygon(geometry)
            if bbox:
                self._building_bboxes[building_id] = bbox
                self._building_by_id[building_id] = building  # Fast O(1) lookup
                self._building_quadtree.insert(building_id, bbox)
        
        logger.info(f"  Built building quadtree with {len(self._building_bboxes)} polygons")
        
        # Build landuse quadtree
        self._landuse_quadtree = QuadtreeIndex(overall_bbox, max_depth=8, max_items=10)
        self._landuse_bboxes = {}
        self._landuse_by_id = {}
        
        for area in self._cached_landuse:
            area_id = area.get("id", 0)
            geometry = area.get("geometry", [])
            bbox = compute_bbox_from_polygon(geometry)
            if bbox:
                self._landuse_bboxes[area_id] = bbox
                self._landuse_by_id[area_id] = area  # Fast O(1) lookup
                self._landuse_quadtree.insert(area_id, bbox)
        
        logger.info(f"  Built landuse quadtree with {len(self._landuse_bboxes)} polygons")
    
    def _fetch_tiled_osm_data(
        self, center: LatLon, radius_m: float
    ) -> Optional[Tuple[List[dict], List[dict]]]:
        """Fetch a large region as cached, exact Overpass tiles."""

        all_buildings: List[dict] = []
        all_landuse: List[dict] = []
        tiles = _tile_centers_for_square(center, radius_m)
        logger.info("OSM tiled prefetch: %d tiles", len(tiles))

        failed_tiles = 0
        for index, (tile_center, half_size_m) in enumerate(tiles, start=1):
            tile_buildings = load_cached_osm_data(tile_center, half_size_m, "buildings")
            tile_landuse = load_cached_osm_data(tile_center, half_size_m, "landuse")
            if tile_buildings is None or tile_landuse is None:
                fetched = self._fetch_combined_osm_tile(tile_center, half_size_m)
                if fetched is None:
                    failed_tiles += 1
                    logger.error("OSM tile %d/%d failed", index, len(tiles))
                    continue
                fetched_buildings, fetched_landuse = fetched
                if tile_buildings is None:
                    tile_buildings = fetched_buildings
                    save_cached_osm_data(tile_center, half_size_m, "buildings", tile_buildings)
                if tile_landuse is None:
                    tile_landuse = fetched_landuse
                    save_cached_osm_data(tile_center, half_size_m, "landuse", tile_landuse)
            all_buildings.extend(tile_buildings or [])
            all_landuse.extend(tile_landuse or [])
            logger.info(
                "OSM tile %d/%d complete: %d buildings, %d landuse",
                index,
                len(tiles),
                len(tile_buildings or []),
                len(tile_landuse or []),
            )

        if failed_tiles:
            # Partial geometry would silently lower planning quality. Do not mark
            # an incomplete large-area fetch as a successful prefetch.
            logger.error("OSM tiled prefetch incomplete: %d/%d tiles failed", failed_tiles, len(tiles))
            return None
        return _dedupe_osm_features(all_buildings), _dedupe_osm_features(all_landuse)

    def _fetch_combined_osm_tile(
        self, center: LatLon, half_size_m: float
    ) -> Optional[Tuple[List[dict], List[dict]]]:
        bbox = _bbox_around_point(center.lat, center.lon, half_size_m)
        query = f"""
        [out:json][timeout:60];
        (
          way["building"]({bbox});
          relation["building"]({bbox});
          way["landuse"]({bbox});
          way["natural"]({bbox});
          relation["landuse"]({bbox});
          relation["natural"]({bbox});
        );
        out geom;
        """
        try:
            data = _post_overpass(query, timeout=75, context="tiled OSM fetch")
            return _parse_overpass_elements(data)
        except Exception as exc:
            logger.error("Failed tiled OSM fetch for bbox %s: %s", bbox, exc)
            return None

    def _fetch_buildings_from_osm(self, center: LatLon, radius_m: float) -> Optional[List[dict]]:
        """Fetch buildings directly from OSM (bypasses cache check)."""
        bbox = _bbox_around_point(center.lat, center.lon, radius_m)
        query = f"""
        [out:json][timeout:25];
        (
          way["building"]({bbox});
          relation["building"]({bbox});
        );
        out geom;
        """
        
        logger.debug(f"OSM query bbox: {bbox}")
        logger.debug(f"OSM query URLs: {OVERPASS_URLS}")
        
        try:
            logger.info(f"Fetching buildings from OSM Overpass API (bbox: {bbox})...")
            data = _post_overpass(query, timeout=30, context="building fetch")
            
            logger.debug(f"OSM API response: {len(data.get('elements', []))} elements")
            
            buildings, _ = _parse_overpass_elements(data)
            buildings = _dedupe_osm_features(buildings)
            
            logger.info(f"Successfully fetched {len(buildings)} buildings from OSM")
            if len(buildings) == 0:
                logger.warning(f"⚠ No buildings found in OSM for bbox {bbox}")
                logger.warning(f"  This may be normal if the location has no building data in OSM")
                logger.warning(f"  Check https://www.openstreetmap.org/ to verify building coverage")
            
            return buildings
            
        except requests.exceptions.RequestException as e:
            logger.error(f"✗ Network error fetching buildings from OSM: {e}")
            logger.error(f"  URLs tried: {OVERPASS_URLS}")
            logger.error(f"  Query bbox: {bbox}")
            return None
        except Exception as e:
            logger.error(f"✗ Error fetching buildings from OSM: {e}", exc_info=True)
            return None
    
    def _fetch_landuse_from_osm(self, center: LatLon, radius_m: float) -> Optional[List[dict]]:
        """Fetch landuse directly from OSM (bypasses cache check)."""
        bbox = _bbox_around_point(center.lat, center.lon, radius_m)
        query = f"""
        [out:json][timeout:25];
        (
          way["landuse"]({bbox});
          way["natural"]({bbox});
          relation["landuse"]({bbox});
          relation["natural"]({bbox});
        );
        out geom;
        """
        
        try:
            data = _post_overpass(query, timeout=30, context="landuse fetch")
            
            _, areas = _parse_overpass_elements(data)
            return _dedupe_osm_features(areas)
            
        except Exception as e:
            logger.warning(f"Failed to fetch landuse from OSM: {e}")
            return None

    def count_buildings_between(self, start: LatLon, end: LatLon) -> int:
        """
        Count building polygons that intersect the LOS ray.
        
        Uses quadtree spatial index if available for fast queries.
        Falls back to linear search if no index.
        """
        slice_h = self.slice_height_m

        # Phase 2: Use quadtree if available
        if self._prefetched and self._building_quadtree is not None:
            # Query quadtree for candidate building IDs
            candidate_ids = self._building_quadtree.query_ray(start, end)
            
            # Only test polygons that quadtree says might intersect
            count = 0
            for building_id in candidate_ids:
                # Fast O(1) lookup by ID
                building = self._building_by_id.get(building_id)
                if not building:
                    continue

                if slice_h is not None:
                    h = building.get("height_m")
                    if h is None:
                        h = _estimate_osm_height_m(building.get("tags", {}))
                        building["height_m"] = h
                    if h is not None and h < slice_h:
                        continue

                if _ray_intersects_polygon(start, end, building.get("geometry", [])):
                    count += 1
            
            return count
        
        # Fallback: linear search (old behavior)
        if self._prefetched and self._cached_buildings:
            buildings = self._cached_buildings
        else:
            # Fallback to old behavior (makes API call)
            buildings = self._get_buildings_near_line(start, end)
        
        count = 0
        for building in buildings:
            if slice_h is not None:
                h = building.get("height_m")
                if h is None:
                    h = _estimate_osm_height_m(building.get("tags", {}))
                    building["height_m"] = h
                if h is not None and h < slice_h:
                    continue
            if _ray_intersects_polygon(start, end, building.get("geometry", [])):
                count += 1
        
        return count
    
    def get_buildings_along_ray(self, start: LatLon, end: LatLon) -> List[dict]:
        """
        Get list of building dicts that intersect the LOS ray, in order along the ray.
        
        Returns buildings with material information for material-aware path loss.
        """
        slice_h = self.slice_height_m

        # Phase 2: Use quadtree if available
        if self._prefetched and self._building_quadtree is not None:
            candidate_ids = self._building_quadtree.query_ray(start, end)
            buildings = []
            for building_id in candidate_ids:
                building = self._building_by_id.get(building_id)
                if not building:
                    continue

                if slice_h is not None:
                    h = building.get("height_m")
                    if h is None:
                        h = _estimate_osm_height_m(building.get("tags", {}))
                        building["height_m"] = h
                    if h is not None and h < slice_h:
                        continue

                if _ray_intersects_polygon(start, end, building.get("geometry", [])):
                    buildings.append(building)
            return buildings
        
        # Fallback: linear search
        if self._prefetched and self._cached_buildings:
            buildings = self._cached_buildings
        else:
            buildings = self._get_buildings_near_line(start, end)
            # Ensure material is extracted for fallback buildings
            for building in buildings:
                if "material" not in building:
                    building["material"] = _extract_building_material(building)
        
        result = []
        for building in buildings:
            if slice_h is not None:
                h = building.get("height_m")
                if h is None:
                    h = _estimate_osm_height_m(building.get("tags", {}))
                    building["height_m"] = h
                if h is not None and h < slice_h:
                    continue
            if _ray_intersects_polygon(start, end, building.get("geometry", [])):
                result.append(building)
        
        return result
 
    def is_forest_between(self, start: LatLon, end: LatLon) -> bool:
        """
        Check if LOS passes through forest/vegetation landuse.
        
        Uses quadtree spatial index if available for fast queries.
        Falls back to linear search if no index.
        """
        # If we're in a height-sliced mode and the slice is above typical vegetation,
        # treat foliage as not intersecting the ray.
        slice_h = self.slice_height_m
        if slice_h is not None:
            # Conservative default: 8m canopy height.
            if slice_h > 8.0:
                return False

        # Phase 2: Use quadtree if available
        if self._prefetched and self._landuse_quadtree is not None:
            # Query quadtree for candidate area IDs
            candidate_ids = self._landuse_quadtree.query_ray(start, end)
            
            # Only test areas that quadtree says might intersect
            for area_id in candidate_ids:
                # Fast O(1) lookup by ID
                area = self._landuse_by_id.get(area_id)
                if area:
                    landuse_type = area.get("tags", {}).get("landuse", "").lower()
                    natural_type = area.get("tags", {}).get("natural", "").lower()
                    
                    # Check for forest/vegetation
                    if landuse_type in ("forest", "wood", "meadow") or natural_type in ("wood", "forest", "tree_row"):
                        if _ray_intersects_polygon(start, end, area.get("geometry", [])):
                            return True
            
            return False
        
        # Fallback: linear search (old behavior)
        if self._prefetched and self._cached_landuse:
            landuse = self._cached_landuse
        else:
            # Fallback to old behavior (makes API call)
            landuse = self._get_landuse_near_line(start, end)
        
        for area in landuse:
            landuse_type = area.get("tags", {}).get("landuse", "").lower()
            natural_type = area.get("tags", {}).get("natural", "").lower()
            
            # Check for forest/vegetation
            if landuse_type in ("forest", "wood", "meadow") or natural_type in ("wood", "forest", "tree_row"):
                if _ray_intersects_polygon(start, end, area["geometry"]):
                    return True
        
        return False

    def get_clutter_type(self, center: LatLon, radius_m: float = 200.0) -> str:
        """
        Classify area clutter type: open, suburban, urban, dense_urban.
        
        Based on:
        - Building density
        - Building heights (if available)
        - Road width
        - Landuse
        """
        buildings = self._get_buildings_near_point(center, radius_m)
        landuse = self._get_landuse_near_point(center, radius_m)
        
        # Count buildings in area
        building_count = len(buildings)
        area_km2 = math.pi * (radius_m / 1000.0) ** 2
        building_density = building_count / area_km2 if area_km2 > 0 else 0
        
        # Estimate average building height (from OSM tags)
        total_height = 0.0
        height_count = 0
        for building in buildings:
            tags = building.get("tags", {})
            # Try to get height from building:levels or building:height
            levels = tags.get("building:levels")
            if levels:
                try:
                    # Assume ~3m per floor
                    total_height += float(levels) * 3.0
                    height_count += 1
                except (ValueError, TypeError):
                    pass
            height_str = tags.get("height")
            if height_str:
                try:
                    # Parse height (might be "15 m" or "15")
                    height_val = float(height_str.split()[0])
                    total_height += height_val
                    height_count += 1
                except (ValueError, TypeError, IndexError):
                    pass
        
        avg_height = total_height / height_count if height_count > 0 else 0.0
        
        # Check for open areas (parks, water)
        is_open = False
        for area in landuse:
            landuse_type = area.get("tags", {}).get("landuse", "").lower()
            if landuse_type in ("park", "recreation_ground", "cemetery", "meadow"):
                is_open = True
                break
        
        # Classify
        if is_open or building_density < 5:
            return "open"
        elif building_density < 50:
            return "suburban"
        elif avg_height > 20.0 or building_density > 200:
            return "dense_urban"
        else:
            return "urban"

    def get_building_area_sqm(self, center: LatLon, radius_m: float) -> float:
        """Total building footprint area (m²) within radius_m of center.

        Uses prefetched buildings if available; otherwise returns 0.
        """
        if not self._prefetched or not self._cached_buildings:
            return 0.0
        total = 0.0
        for building in self._cached_buildings:
            geom = building.get("geometry", [])
            if len(geom) < 3:
                continue
            centroid = _polygon_centroid(geom)
            if centroid is None:
                continue
            if _distance_m(center, centroid) > radius_m:
                continue
            total += _polygon_area_sqm(geom, center.lat)
        return total

    def find_buildings_within_radius(
        self,
        center: LatLon,
        radius_m: float = 50.0,
        max_count: Optional[int] = None,
    ) -> List[dict]:
        """Return OSM buildings whose footprint contains or lies within radius_m of center."""
        search_radius_m = max(1.0, float(radius_m))

        try:
            self.prefetch_all_data(center, search_radius_m + 15.0)
            buildings = self._cached_buildings or []
        except Exception:
            buildings = self._get_buildings_near_point(center, search_radius_m + 15.0)

        matches: List[dict] = []
        for building in buildings:
            geom = building.get("geometry", [])
            if len(geom) < 3:
                continue

            contains = _polygon_contains_point(geom, center)
            dist_m = 0.0 if contains else _distance_point_to_polygon_m(center, geom)
            if (not contains) and dist_m > search_radius_m:
                continue

            centroid = _polygon_centroid(geom)
            area_sqm = _polygon_area_sqm(geom, center.lat)
            material = building.get("material") or _extract_building_material(building)
            height_m = building.get("height_m")
            if height_m is None:
                height_m = _estimate_osm_height_m(building.get("tags", {}))

            matches.append({
                "id": building.get("id"),
                "tags": building.get("tags", {}),
                "geometry": geom,
                "material": material,
                "height_m": height_m,
                "centroid": centroid.model_dump() if centroid is not None else None,
                "area_sqm": area_sqm,
                "distance_to_point_m": float(dist_m),
                "match_type": "contains" if contains else "nearby",
            })

        matches.sort(key=lambda b: (
            0 if b.get("match_type") == "contains" else 1,
            float(b.get("distance_to_point_m") or 0.0),
            -float(b.get("area_sqm") or 0.0),
            int(b.get("id") or 0),
        ))
        if max_count is not None:
            try:
                limit = max(1, int(max_count))
                matches = matches[:limit]
            except Exception:
                pass
        return matches

    def find_building_at_point(self, center: LatLon, radius_m: float = 50.0) -> Optional[dict]:
        """Return the best matching building for a selected point."""
        matches = self.find_buildings_within_radius(center, radius_m=radius_m, max_count=1)
        if not matches:
            return None
        match = dict(matches[0])
        if match.get("match_type") != "contains":
            match["match_type"] = "nearest"
        return match

    def _get_buildings_near_point(self, center: LatLon, radius_m: float) -> List[dict]:
        """Fetch building polygons near a point."""
        # Check cache - if prefetched, use it if point is within prefetch radius
        if self._prefetched and self._cache_center:
            dist_from_center = _distance_m(center, self._cache_center)
            if dist_from_center + radius_m <= self._prefetch_radius_m:
                logger.debug(f"Using prefetched buildings cache (distance from center: {dist_from_center:.1f}m)")
                return self._cached_buildings
        # Fallback to old cache check
        elif self._cache_center and _distance_m(center, self._cache_center) < self.cache_radius_m:
            return self._cached_buildings
        
        # Query OSM
        bbox = _bbox_around_point(center.lat, center.lon, radius_m)
        query = f"""
        [out:json][timeout:25];
        (
          way["building"]({bbox});
          relation["building"]({bbox});
        );
        out geom;
        """
        
        try:
            data = _post_overpass(query, timeout=30, context="building near-point fetch")
            
            buildings = []
            for element in data.get("elements", []):
                if element.get("type") == "way" and "geometry" in element:
                    buildings.append({
                        "id": element.get("id"),
                        "tags": element.get("tags", {}),
                        "geometry": element.get("geometry", []),
                    })
            
            # Update cache
            self._cache_center = center
            self._cached_buildings = buildings
            
            logger.debug(f"Fetched {len(buildings)} buildings near ({center.lat}, {center.lon})")
            return buildings
            
        except Exception as e:
            logger.warning(f"Failed to fetch buildings from OSM: {e}")
            return []

    def _get_buildings_near_line(self, start: LatLon, end: LatLon) -> List[dict]:
        """Fetch buildings near a line segment."""
        # Use midpoint and distance to determine search radius
        mid_lat = (start.lat + end.lat) / 2.0
        mid_lon = (start.lon + end.lon) / 2.0
        distance = _distance_m(start, end)
        radius_m = distance / 2.0 + 50.0  # Add buffer
        
        return self._get_buildings_near_point(LatLon(lat=mid_lat, lon=mid_lon), radius_m)

    def _get_landuse_near_point(self, center: LatLon, radius_m: float) -> List[dict]:
        """Fetch landuse polygons near a point."""
        # Check cache - if prefetched, use it if point is within prefetch radius
        if self._prefetched and self._cache_center:
            dist_from_center = _distance_m(center, self._cache_center)
            if dist_from_center + radius_m <= self._prefetch_radius_m:
                logger.debug(f"Using prefetched landuse cache (distance from center: {dist_from_center:.1f}m)")
                return self._cached_landuse
        # Fallback to old cache check
        elif self._cache_center and _distance_m(center, self._cache_center) < self.cache_radius_m:
            return self._cached_landuse
        
        # Query OSM
        bbox = _bbox_around_point(center.lat, center.lon, radius_m)
        query = f"""
        [out:json][timeout:25];
        (
          way["landuse"]({bbox});
          way["natural"]({bbox});
          relation["landuse"]({bbox});
          relation["natural"]({bbox});
        );
        out geom;
        """
        
        try:
            data = _post_overpass(query, timeout=30, context="landuse near-point fetch")
            
            areas = []
            for element in data.get("elements", []):
                if element.get("type") == "way" and "geometry" in element:
                    areas.append({
                        "id": element.get("id"),
                        "tags": element.get("tags", {}),
                        "geometry": element.get("geometry", []),
                    })
            
            # Update cache
            self._cached_landuse = areas
            
            logger.debug(f"Fetched {len(areas)} landuse areas near ({center.lat}, {center.lon})")
            return areas
            
        except Exception as e:
            logger.warning(f"Failed to fetch landuse from OSM: {e}")
            return []

    def _get_landuse_near_line(self, start: LatLon, end: LatLon) -> List[dict]:
        """Fetch landuse near a line segment."""
        mid_lat = (start.lat + end.lat) / 2.0
        mid_lon = (start.lon + end.lon) / 2.0
        distance = _distance_m(start, end)
        radius_m = distance / 2.0 + 50.0
        
        return self._get_landuse_near_point(LatLon(lat=mid_lat, lon=mid_lon), radius_m)


def _extract_building_material(building: dict) -> str:
    """
    Extract material type from OSM building tags.
    
    Returns: 'wood', 'concrete', 'brick', 'metal', 'glass', 'unknown'
    
    Logic:
    - Direct material tag (building:material)
    - Infer from building type (US context: residential=wood, commercial=concrete)
    - Infer from construction type
    - Default: 'unknown' (will use frequency-dependent defaults)
    """
    tags = building.get("tags", {})
    
    # Direct material tag
    material = tags.get("building:material", "").lower()
    if material in ("wood", "timber", "log"):
        return "wood"
    elif material in ("concrete", "cinder_block", "cinderblock"):
        return "concrete"
    elif material in ("brick", "stone", "masonry"):
        return "brick"
    elif material in ("metal", "steel", "corrugated_steel", "tin"):
        return "metal"
    elif material in ("glass", "glazing"):
        return "glass"
    
    # Infer from building type (US context)
    building_type = tags.get("building", "").lower()
    if building_type in ("residential", "house", "detached", "semi", "terrace", "bungalow"):
        return "wood"  # Typical US residential construction
    elif building_type in ("apartments", "commercial", "retail", "office", "hotel"):
        return "concrete"  # Typical commercial construction
    elif building_type in ("industrial", "warehouse", "factory", "hangar"):
        return "metal"  # Typical industrial construction
    
    # Infer from construction type
    construction = tags.get("building:construction", "").lower()
    if "wood" in construction or "timber" in construction or "frame" in construction:
        return "wood"
    elif "concrete" in construction or "cinder" in construction:
        return "concrete"
    elif "brick" in construction or "masonry" in construction:
        return "brick"
    elif "metal" in construction or "steel" in construction:
        return "metal"
    
    # Default: unknown (will use frequency-dependent defaults)
    return "unknown"


def _bbox_around_point(lat: float, lon: float, radius_m: float) -> str:
    """Generate OSM bbox string around a point."""
    dlat = radius_m / 111000.0
    dlon = radius_m / (111000.0 * abs(math.cos(math.radians(lat))))
    min_lon = lon - dlon
    max_lon = lon + dlon
    min_lat = lat - dlat
    max_lat = lat + dlat
    return f"{min_lat},{min_lon},{max_lat},{max_lon}"


def _distance_m(p1: LatLon, p2: LatLon) -> float:
    """Haversine distance in meters."""
    R = 6371000.0
    lat1_rad = math.radians(p1.lat)
    lat2_rad = math.radians(p2.lat)
    dlat = math.radians(p2.lat - p1.lat)
    dlon = math.radians(p2.lon - p1.lon)
    
    a = math.sin(dlat / 2.0) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2.0) ** 2
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


def _polygon_area_sqm(geometry: List[dict], ref_lat: float) -> float:
    """Compute polygon area in m² using Shoelace formula with equirectangular projection.

    At ref_lat, 1 deg lat ≈ 111320 m, 1 deg lon ≈ 111320*cos(ref_lat) m.
    """
    if len(geometry) < 3:
        return 0.0
    points = []
    for node in geometry:
        if "lat" in node and "lon" in node:
            points.append((float(node["lat"]), float(node["lon"])))
    if len(points) < 3:
        return 0.0
    lat0_rad = math.radians(ref_lat)
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(lat0_rad)
    xs = [(p[1] * m_per_deg_lon) for p in points]
    ys = [(p[0] * m_per_deg_lat) for p in points]
    n = len(xs)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += xs[i] * ys[j] - xs[j] * ys[i]
    return abs(area) * 0.5


def _polygon_centroid(geometry: List[dict]) -> Optional[LatLon]:
    """Return centroid of polygon for distance check."""
    if len(geometry) < 3:
        return None
    lats = []
    lons = []
    for node in geometry:
        if "lat" in node and "lon" in node:
            lats.append(float(node["lat"]))
            lons.append(float(node["lon"]))
    if not lats or not lons:
        return None
    return LatLon(lat=sum(lats) / len(lats), lon=sum(lons) / len(lons))


def _polygon_contains_point(geometry: List[dict], point: LatLon) -> bool:
    """Return True when a point lies inside the polygon footprint."""
    pts = []
    for node in geometry:
        if "lat" in node and "lon" in node:
            pts.append((float(node["lat"]), float(node["lon"])))
    if len(pts) < 3:
        return False

    inside = False
    x = float(point.lon)
    y = float(point.lat)
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i][0], pts[i][1]
        yj, xj = pts[j][0], pts[j][1]
        intersects = (yi > y) != (yj > y)
        if intersects:
            denom = (yj - yi) if abs(yj - yi) > 1e-12 else 1e-12
            x_cross = (xj - xi) * (y - yi) / denom + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _project_local_xy(ref: LatLon, node: dict) -> Optional[Tuple[float, float]]:
    if "lat" not in node or "lon" not in node:
        return None
    lat = float(node["lat"])
    lon = float(node["lon"])
    lat0 = math.radians(ref.lat)
    north = math.radians(lat - ref.lat) * 6371000.0
    east = math.radians(lon - ref.lon) * 6371000.0 * math.cos(lat0)
    return east, north


def _distance_point_to_polygon_m(point: LatLon, geometry: List[dict]) -> float:
    """Minimum horizontal distance from a point to a polygon boundary."""
    if _polygon_contains_point(geometry, point):
        return 0.0

    pts = []
    for node in geometry:
        xy = _project_local_xy(point, node)
        if xy is not None:
            pts.append(xy)
    if len(pts) < 2:
        return float("inf")

    if pts[0] != pts[-1]:
        pts.append(pts[0])

    best = float("inf")
    for i in range(len(pts) - 1):
        d = _distance_point_to_segment_xy(0.0, 0.0, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
        if d < best:
            best = d
    return best


def _distance_point_to_segment_xy(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    vv = vx * vx + vy * vy
    if vv < 1e-12:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / vv))
    cx = ax + t * vx
    cy = ay + t * vy
    return math.hypot(px - cx, py - cy)


def _ray_intersects_polygon(start: LatLon, end: LatLon, polygon: List[dict]) -> bool:
    """
    Check if ray from start to end intersects polygon.
    
    Simple approach: check if ray crosses polygon edges (ray casting).
    """
    if len(polygon) < 3:
        return False
    
    # Convert polygon to list of (lat, lon) tuples
    points = []
    for node in polygon:
        if "lat" in node and "lon" in node:
            points.append((node["lat"], node["lon"]))
    
    if len(points) < 3:
        return False
    
    # Ray casting algorithm
    # Count intersections of ray with polygon edges
    intersections = 0
    n = len(points)
    
    for i in range(n):
        p1 = points[i]
        p2 = points[(i + 1) % n]
        
        if _segments_intersect(start, end, LatLon(lat=p1[0], lon=p1[1]), LatLon(lat=p2[0], lon=p2[1])):
            intersections += 1
    
    # Odd number of intersections = inside polygon
    return intersections % 2 == 1


def _segments_intersect(p1: LatLon, p2: LatLon, p3: LatLon, p4: LatLon) -> bool:
    """
    Check if line segments (p1-p2) and (p3-p4) intersect.
    
    Uses cross product test.
    """
    def ccw(A: LatLon, B: LatLon, C: LatLon) -> bool:
        return (C.lat - A.lat) * (B.lon - A.lon) > (B.lat - A.lat) * (C.lon - A.lon)
    
    return ccw(p1, p3, p4) != ccw(p2, p3, p4) and ccw(p1, p2, p3) != ccw(p1, p2, p4)

