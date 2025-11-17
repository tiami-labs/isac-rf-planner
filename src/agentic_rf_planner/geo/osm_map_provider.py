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


class OSMMapProvider(MapProvider):
    """
    Real MapProvider using OSM Overpass API.
    
    Fetches building footprints and landcover data to:
    - Count buildings along LOS rays
    - Detect forest/vegetation
    - Classify clutter (open/suburban/urban/dense_urban)
    """

    def __init__(self, cache_radius_m: float = 1000.0):
        """
        Args:
            cache_radius_m: Radius around last query to cache (simple in-memory cache)
        """
        self.cache_radius_m = cache_radius_m
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
        if self._prefetched and self._cache_center and _distance_m(center, self._cache_center) < 100.0 and radius_m <= self._prefetch_radius_m:
            logger.debug(f"Using existing prefetched data (radius={self._prefetch_radius_m}m)")
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
                # Save to persistent cache
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
        
        try:
            response = requests.post(OVERPASS_URL, data={"data": query}, timeout=30)
            response.raise_for_status()
            data = response.json()
            
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
                    buildings.append(building)
            
            return buildings
            
        except Exception as e:
            logger.warning(f"Failed to fetch buildings from OSM: {e}")
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
        # Phase 2: Use quadtree if available
        if self._prefetched and self._building_quadtree is not None:
            # Query quadtree for candidate building IDs
            candidate_ids = self._building_quadtree.query_ray(start, end)
            
            # Only test polygons that quadtree says might intersect
            count = 0
            for building_id in candidate_ids:
                # Fast O(1) lookup by ID
                building = self._building_by_id.get(building_id)
                if building and _ray_intersects_polygon(start, end, building.get("geometry", [])):
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
            if _ray_intersects_polygon(start, end, building.get("geometry", [])):
                count += 1
        
        return count
    
    def get_buildings_along_ray(self, start: LatLon, end: LatLon) -> List[dict]:
        """
        Get list of building dicts that intersect the LOS ray, in order along the ray.
        
        Returns buildings with material information for material-aware path loss.
        """
        # Phase 2: Use quadtree if available
        if self._prefetched and self._building_quadtree is not None:
            candidate_ids = self._building_quadtree.query_ray(start, end)
            buildings = []
            for building_id in candidate_ids:
                building = self._building_by_id.get(building_id)
                if building and _ray_intersects_polygon(start, end, building.get("geometry", [])):
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
            if _ray_intersects_polygon(start, end, building.get("geometry", [])):
                result.append(building)
        
        return result
 
    def is_forest_between(self, start: LatLon, end: LatLon) -> bool:
        """
        Check if LOS passes through forest/vegetation landuse.
        
        Uses quadtree spatial index if available for fast queries.
        Falls back to linear search if no index.
        """
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

