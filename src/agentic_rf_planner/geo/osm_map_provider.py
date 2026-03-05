"""OSM-based MapProvider using Overpass API for building footprints and landcover."""

import logging
import math
from typing import List, Optional, Tuple, Dict

import requests

from ..pipeline.schemas import LatLon, MaterialType
from .physical_spanning import MapProvider
from .spatial_index import QuadtreeIndex, BoundingBox, compute_bbox_from_polygon
from .osm_cache import load_cached_osm_data, save_cached_osm_data

logger = logging.getLogger(__name__)

# Overpass API endpoint (public instance)
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


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
                self._cached_buildings = self._fetch_buildings_from_osm(center, radius_m)
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
                self._cached_landuse = self._fetch_landuse_from_osm(center, radius_m)
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
    
    def _fetch_buildings_from_osm(self, center: LatLon, radius_m: float) -> List[dict]:
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
        logger.debug(f"OSM query URL: {OVERPASS_URL}")
        
        try:
            logger.info(f"Fetching buildings from OSM Overpass API (bbox: {bbox})...")
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            logger.debug(f"OSM API response: {len(data.get('elements', []))} elements")
            
            buildings = []
            for element in data.get("elements", []):
                if element.get("type") == "way" and "geometry" in element:
                    building = {
                        "id": element.get("id"),
                        "tags": element.get("tags", {}),
                        "geometry": element.get("geometry", []),
                    }
                    # Extract material from OSM tags
                    building["material"] = _extract_building_material(building)
                    building["height_m"] = _estimate_osm_height_m(building.get("tags", {}))
                    buildings.append(building)
            
            logger.info(f"Successfully fetched {len(buildings)} buildings from OSM")
            if len(buildings) == 0:
                logger.warning(f"⚠ No buildings found in OSM for bbox {bbox}")
                logger.warning(f"  This may be normal if the location has no building data in OSM")
                logger.warning(f"  Check https://www.openstreetmap.org/ to verify building coverage")
            
            return buildings
            
        except requests.exceptions.RequestException as e:
            logger.error(f"✗ Network error fetching buildings from OSM: {e}")
            logger.error(f"  URL: {OVERPASS_URL}")
            logger.error(f"  Query bbox: {bbox}")
            return []
        except Exception as e:
            logger.error(f"✗ Error fetching buildings from OSM: {e}", exc_info=True)
            return []
    
    def _fetch_landuse_from_osm(self, center: LatLon, radius_m: float) -> List[dict]:
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
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            areas = []
            for element in data.get("elements", []):
                if element.get("type") == "way" and "geometry" in element:
                    areas.append({
                        "id": element.get("id"),
                        "tags": element.get("tags", {}),
                        "geometry": element.get("geometry", []),
                    })
            
            return areas
            
        except Exception as e:
            logger.warning(f"Failed to fetch landuse from OSM: {e}")
            return []

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
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
            response.raise_for_status()
            data = response.json()
            
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
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
            response.raise_for_status()
            data = response.json()
            
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

