"""Spatial indexing for efficient ray-polygon intersection queries."""

import math
from typing import List, Tuple, Optional, Set
from dataclasses import dataclass

from ..pipeline.schemas import LatLon


@dataclass
class BoundingBox:
    """2D bounding box in lat/lon coordinates."""
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    
    def contains_point(self, lat: float, lon: float) -> bool:
        """Check if point is inside bounding box."""
        return (self.min_lat <= lat <= self.max_lat and 
                self.min_lon <= lon <= self.max_lon)
    
    def intersects(self, other: 'BoundingBox') -> bool:
        """Check if this bounding box intersects another."""
        return not (self.max_lat < other.min_lat or 
                   self.min_lat > other.max_lat or
                   self.max_lon < other.min_lon or
                   self.min_lon > other.max_lon)
    
    def intersects_ray(self, start: LatLon, end: LatLon) -> bool:
        """
        Check if ray from start to end intersects this bounding box.
        
        Uses simple bounding box test: ray intersects if either endpoint
        is inside, or if ray crosses any edge of the box.
        """
        # If either endpoint is inside, ray intersects
        if self.contains_point(start.lat, start.lon) or self.contains_point(end.lat, end.lon):
            return True
        
        # Check if ray crosses any edge of the bounding box
        # Test ray against each edge of the box
        box_corners = [
            (self.min_lat, self.min_lon),  # SW
            (self.min_lat, self.max_lon),  # SE
            (self.max_lat, self.max_lon),  # NE
            (self.max_lat, self.min_lon),  # NW
        ]
        
        # Check if ray intersects any edge
        for i in range(4):
            p1 = box_corners[i]
            p2 = box_corners[(i + 1) % 4]
            if _segments_intersect_simple(
                start.lat, start.lon, end.lat, end.lon,
                p1[0], p1[1], p2[0], p2[1]
            ):
                return True
        
        return False


def _segments_intersect_simple(
    x1: float, y1: float, x2: float, y2: float,
    x3: float, y3: float, x4: float, y4: float
) -> bool:
    """Check if two line segments intersect (simple cross product test)."""
    def ccw(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> bool:
        return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax)
    
    return (ccw(x1, y1, x3, y3, x4, y4) != ccw(x2, y2, x3, y3, x4, y4) and
            ccw(x1, y1, x2, y2, x3, y3) != ccw(x1, y1, x2, y2, x4, y4))


class QuadtreeIndex:
    """
    Quadtree spatial index for efficient spatial queries.
    
    Organizes items (polygons) in a hierarchical tree structure.
    For ray queries, only tests items in quadtree cells that the ray passes through.
    """
    
    def __init__(self, bbox: BoundingBox, max_depth: int = 8, max_items: int = 10):
        """
        Initialize quadtree.
        
        Args:
            bbox: Bounding box of the entire quadtree
            max_depth: Maximum depth of the tree (prevents infinite recursion)
            max_items: Maximum items per node before splitting
        """
        self.bbox = bbox
        self.max_depth = max_depth
        self.max_items = max_items
        self.depth = 0
        self.children: Optional[List['QuadtreeIndex']] = None
        self.items: List[Tuple[int, BoundingBox]] = []  # List of (item_id, bbox)
    
    def insert(self, item_id: int, bbox: BoundingBox) -> None:
        """
        Insert an item into the quadtree.
        
        Args:
            item_id: Unique identifier for the item (e.g., polygon ID)
            bbox: Bounding box of the item
        """
        # If this node has children, try to insert into appropriate child
        if self.children is not None:
            for child in self.children:
                if child.bbox.intersects(bbox):
                    child.insert(item_id, bbox)
            return
        
        # Add item to this node
        self.items.append((item_id, bbox))
        
        # If we've exceeded max_items and haven't reached max_depth, split
        if len(self.items) > self.max_items and self.depth < self.max_depth:
            self._split()
    
    def _split(self) -> None:
        """Split this node into 4 children."""
        mid_lat = (self.bbox.min_lat + self.bbox.max_lat) / 2.0
        mid_lon = (self.bbox.min_lon + self.bbox.max_lon) / 2.0
        
        self.children = [
            QuadtreeIndex(
                BoundingBox(self.bbox.min_lat, mid_lat, self.bbox.min_lon, mid_lon),
                self.max_depth,
                self.max_items
            ),  # SW
            QuadtreeIndex(
                BoundingBox(self.bbox.min_lat, mid_lat, mid_lon, self.bbox.max_lon),
                self.max_depth,
                self.max_items
            ),  # SE
            QuadtreeIndex(
                BoundingBox(mid_lat, self.bbox.max_lat, mid_lon, self.bbox.max_lon),
                self.max_depth,
                self.max_items
            ),  # NE
            QuadtreeIndex(
                BoundingBox(mid_lat, self.bbox.max_lat, self.bbox.min_lon, mid_lon),
                self.max_depth,
                self.max_items
            ),  # NW
        ]
        
        # Set depth for children
        for child in self.children:
            child.depth = self.depth + 1
        
        # Redistribute items to children
        items_to_redistribute = self.items
        self.items = []
        
        for item_id, bbox in items_to_redistribute:
            for child in self.children:
                if child.bbox.intersects(bbox):
                    child.insert(item_id, bbox)
    
    def query_ray(self, start: LatLon, end: LatLon) -> Set[int]:
        """
        Query quadtree for items whose bounding boxes intersect the ray.
        
        Args:
            start: Start point of ray
            end: End point of ray
        
        Returns:
            Set of item IDs whose bounding boxes intersect the ray
        """
        result: Set[int] = set()
        
        # Check if ray intersects this node's bounding box
        if not self.bbox.intersects_ray(start, end):
            return result
        
        # If this node has children, query them
        if self.children is not None:
            for child in self.children:
                result.update(child.query_ray(start, end))
        else:
            # Leaf node: test items directly
            for item_id, bbox in self.items:
                if bbox.intersects_ray(start, end):
                    result.add(item_id)
        
        return result
    
    def query_bbox(self, bbox: BoundingBox) -> Set[int]:
        """
        Query quadtree for items whose bounding boxes intersect the given bbox.
        
        Args:
            bbox: Query bounding box
        
        Returns:
            Set of item IDs whose bounding boxes intersect the query bbox
        """
        result: Set[int] = set()
        
        # Check if query bbox intersects this node's bounding box
        if not self.bbox.intersects(bbox):
            return result
        
        # If this node has children, query them
        if self.children is not None:
            for child in self.children:
                result.update(child.query_bbox(bbox))
        else:
            # Leaf node: test items directly
            for item_id, item_bbox in self.items:
                if item_bbox.intersects(bbox):
                    result.add(item_id)
        
        return result


def compute_bbox_from_polygon(polygon: List[dict]) -> Optional[BoundingBox]:
    """
    Compute bounding box from polygon geometry.
    
    Args:
        polygon: List of polygon nodes with 'lat' and 'lon' keys
    
    Returns:
        BoundingBox or None if polygon is invalid
    """
    if not polygon:
        return None
    
    lats = []
    lons = []
    
    for node in polygon:
        if "lat" in node and "lon" in node:
            lats.append(node["lat"])
            lons.append(node["lon"])
    
    if not lats or not lons:
        return None
    
    return BoundingBox(
        min_lat=min(lats),
        max_lat=max(lats),
        min_lon=min(lons),
        max_lon=max(lons),
    )


