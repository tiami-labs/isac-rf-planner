"""Sector configuration for directional antenna patterns."""

import logging
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class SectorConfig(BaseModel):
    """
    Configuration for a single antenna sector.
    
    Each sector defines:
    - Coverage shape: angular (start_angle_deg to end_angle_deg) OR polygon (list of lat/lon points)
    - Frequency and bandwidth
    - Transmit power
    - Optional: antenna pattern parameters (for future beam pattern modeling)
    """
    
    sector_id: str = Field(..., description="Unique identifier for this sector (e.g., 'sector_1', 'n78_azimuth_0')")
    sector_type: Literal["360", "angle", "polygon"] = Field("angle", description="Type of sector: 360 (omnidirectional), angle (angular range), or polygon (abstract shape)")
    
    # Angular coverage (for sector_type="angle" or "360")
    start_angle_deg: Optional[float] = Field(None, ge=0.0, le=360.0, description="Start angle in degrees (0-360, absolute bearing). Required for angle-based sectors.")
    end_angle_deg: Optional[float] = Field(None, ge=0.0, le=360.0, description="End angle in degrees (0-360, absolute bearing). Required for angle-based sectors.")
    
    # Polygon coverage (for sector_type="polygon")
    # List of [lat, lon] points forming a contiguous polygon
    # Origin point (TX location) is implicit - polygon points are relative to TX
    polygon_points: Optional[List[List[float]]] = Field(None, description="List of [lat, lon] points for polygon sector. Required for polygon sectors. Must be contiguous (single polygon, not multi-polygon).")
    
    # RF parameters specific to this sector
    freq_mhz: float = Field(..., gt=0.0, description="Frequency in MHz for this sector")
    tx_power_dbm: float = Field(..., description="Transmit power in dBm for this sector")
    channel_bandwidth_mhz: float = Field(..., gt=0.0, description="Channel bandwidth in MHz")
    
    # Optional: antenna pattern parameters (for future use)
    azimuth_deg: Optional[float] = Field(None, ge=0.0, le=360.0, description="Boresight azimuth in degrees (center of sector)")
    beamwidth_h_deg: Optional[float] = Field(None, gt=0.0, le=360.0, description="Horizontal beamwidth in degrees")
    beamwidth_v_deg: Optional[float] = Field(None, gt=0.0, le=180.0, description="Vertical beamwidth in degrees")
    
    @model_validator(mode='after')
    def validate_sector_type(self):
        """Validate that required fields are present based on sector_type."""
        if self.sector_type == "360":
            # 360° sector: set angles to 0-360
            self.start_angle_deg = 0.0
            self.end_angle_deg = 360.0
            self.polygon_points = None
        elif self.sector_type == "angle":
            # Angle-based sector: require start/end angles
            if self.start_angle_deg is None or self.end_angle_deg is None:
                raise ValueError("start_angle_deg and end_angle_deg are required for angle-based sectors")
            self.polygon_points = None
        elif self.sector_type == "polygon":
            # Polygon sector: require polygon points
            # Minimum 2 points required (TX origin is the third point, making it a triangle minimum)
            if not self.polygon_points or len(self.polygon_points) < 2:
                raise ValueError("polygon_points must have at least 2 points for polygon sectors (TX origin is the third point)")
            # Validate polygon points are [lat, lon] pairs
            for i, point in enumerate(self.polygon_points):
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError(f"polygon_points[{i}] must be [lat, lon] pair")
                lat, lon = point
                if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
                    raise ValueError(f"polygon_points[{i}] has invalid coordinates: [{lat}, {lon}]")
            self.start_angle_deg = None
            self.end_angle_deg = None
        return self
    
    def covers_angle(self, angle_deg: float) -> bool:
        """
        Check if this sector covers a given angle (in degrees, 0-360).
        
        Only valid for angle-based sectors (360° or angle type).
        For polygon sectors, use covers_point() instead.
        """
        if self.sector_type == "polygon":
            raise ValueError("covers_angle() is not valid for polygon sectors. Use covers_point() instead.")
        
        start = self.start_angle_deg
        end = self.end_angle_deg
        
        # Normalize angle to 0-360
        angle = angle_deg % 360.0
        
        # Case 1: No wrap-around (start < end)
        if start <= end:
            return start <= angle <= end
        
        # Case 2: Wrap-around (start > end, e.g., 350-10)
        # Sector covers: [start, 360) U [0, end]
        return angle >= start or angle <= end
    
    def covers_point(self, lat: float, lon: float, tx_lat: float, tx_lon: float) -> bool:
        """
        Check if a point (lat, lon) is inside this sector.
        
        For polygon sectors: uses point-in-polygon test.
        For angle-based sectors: checks if bearing from TX to point is within angle range.
        For 360° sectors: always returns True.
        """
        if self.sector_type == "360":
            return True
        elif self.sector_type == "polygon":
            if not self.polygon_points:
                logger.warning(f"Polygon sector {self.sector_id} has no polygon_points")
                return False
            # Point-in-polygon test using ray casting algorithm
            # Polygon points are absolute coordinates (not relative to TX)
            # We need to construct the full polygon including TX as origin
            # polygon_points contains only the vertices (not TX), so we add TX as first point
            polygon = [[tx_lat, tx_lon]] + self.polygon_points
            result = _point_in_polygon(lat, lon, polygon)
            logger.debug(f"Point ({lat:.6f}, {lon:.6f}) in polygon sector {self.sector_id}: {result} "
                        f"(polygon has {len(polygon)} points: TX + {len(self.polygon_points)} vertices)")
            return result
        else:  # angle-based
            # Calculate bearing from TX to point
            bearing = _calculate_bearing(tx_lat, tx_lon, lat, lon)
            return self.covers_angle(bearing)
    
    def get_center_angle(self) -> float:
        """Get the center angle of this sector (only for angle-based sectors)."""
        if self.sector_type == "polygon":
            raise ValueError("get_center_angle() is not valid for polygon sectors")
        
        start = self.start_angle_deg
        end = self.end_angle_deg
        
        if start <= end:
            center = (start + end) / 2.0
        else:
            # Wrap-around case: e.g., 350-10 -> center is 0 (or 360)
            span = (360.0 - start) + end
            center = (start + span / 2.0) % 360.0
        
        return center
    
    def get_span_deg(self) -> float:
        """Get the angular span of this sector in degrees (only for angle-based sectors)."""
        if self.sector_type == "polygon":
            raise ValueError("get_span_deg() is not valid for polygon sectors")
        
        start = self.start_angle_deg
        end = self.end_angle_deg
        
        if start <= end:
            return end - start
        else:
            # Wrap-around case
            return (360.0 - start) + end


def _point_in_polygon(lat: float, lon: float, polygon: List[List[float]]) -> bool:
    """
    Point-in-polygon test using ray casting algorithm.
    
    Args:
        lat, lon: Point to test
        polygon: List of [lat, lon] points forming a closed polygon
                 (includes TX origin as first point, so minimum 3 points total = 2 user points + TX)
    
    Returns:
        True if point is inside polygon, False otherwise
    
    Note: Uses lat as y-axis (vertical) and lon as x-axis (horizontal) for ray casting.
    """
    # Minimum 3 points required (TX origin + at least 2 user points = triangle)
    if len(polygon) < 3:
        return False
    
    n = len(polygon)
    inside = False
    
    j = n - 1
    for i in range(n):
        # polygon[i] = [lat_i, lon_i]
        lat_i, lon_i = polygon[i]
        lat_j, lon_j = polygon[j]
        
        # Ray casting: cast horizontal ray from point (lat, lon) to infinity (eastward)
        # Check if ray crosses edge between points i and j
        # Ray is at y = lat (horizontal line at point's latitude)
        # Edge goes from (lon_i, lat_i) to (lon_j, lat_j)
        
        # Check if ray is between the edge endpoints vertically
        ray_between_vertices = (lat_i > lat) != (lat_j > lat)
        
        if ray_between_vertices:
            # Calculate x-coordinate (longitude) where ray crosses the edge
            # Line equation: lon = lon_i + (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i)
            if lat_j != lat_i:  # Avoid division by zero
                intersection_lon = lon_i + (lon_j - lon_i) * (lat - lat_i) / (lat_j - lat_i)
                # Ray crosses edge if point is to the left (west) of intersection
                if lon < intersection_lon:
                    inside = not inside
        
        j = i
    
    return inside


def _calculate_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate bearing (azimuth) from point 1 to point 2 in degrees (0-360).
    
    Uses the haversine formula for bearing calculation.
    """
    import math
    
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    dlon_rad = math.radians(lon2 - lon1)
    
    y = math.sin(dlon_rad) * math.cos(lat2_rad)
    x = math.cos(lat1_rad) * math.sin(lat2_rad) - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon_rad)
    
    bearing_rad = math.atan2(y, x)
    bearing_deg = math.degrees(bearing_rad)
    
    # Normalize to 0-360
    return (bearing_deg + 360.0) % 360.0


def validate_sectors(sectors: List[SectorConfig]) -> None:
    """
    Validate that sectors don't overlap (or handle overlaps appropriately).
    
    For now, we allow overlaps (multiple sectors can cover the same angle).
    In the future, we might want to detect and warn about overlaps.
    """
    if not sectors:
        logger.warning("No sectors defined - will use omnidirectional default")
        return
    
    logger.info(f"Validating {len(sectors)} sector(s)...")
    
    for i, sector in enumerate(sectors):
        if sector.sector_type == "polygon":
            logger.debug(f"  Sector {i+1}: {sector.sector_id} - Polygon with {len(sector.polygon_points)} points")
        else:
            logger.debug(f"  Sector {i+1}: {sector.sector_id} - {sector.start_angle_deg:.1f}° to {sector.end_angle_deg:.1f}° "
                        f"(span: {sector.get_span_deg():.1f}°, center: {sector.get_center_angle():.1f}°)")
            
            if sector.get_span_deg() <= 0:
                raise ValueError(f"Sector {sector.sector_id} has invalid span: {sector.get_span_deg():.1f}°")
    
    # Check for overlaps (warning only, not error)
    for i in range(len(sectors)):
        for j in range(i + 1, len(sectors)):
            s1, s2 = sectors[i], sectors[j]
            # Simple overlap check: if centers are close, likely overlap
            center1 = s1.get_center_angle()
            center2 = s2.get_center_angle()
            min_span = min(s1.get_span_deg(), s2.get_span_deg())
            
            # Check if sectors might overlap
            if abs(center1 - center2) < min_span:
                logger.warning(f"Sectors {s1.sector_id} and {s2.sector_id} may overlap "
                              f"(centers: {center1:.1f}° and {center2:.1f}°, spans: {s1.get_span_deg():.1f}° and {s2.get_span_deg():.1f}°)")
    
    logger.info(f"✓ Validated {len(sectors)} sector(s)")


def create_omnidirectional_sector(rf_params) -> SectorConfig:
    """
    Create a single omnidirectional sector (360° coverage).
    
    Used as default when no sectors are specified.
    """
    return SectorConfig(
        sector_id="omnidirectional",
        sector_type="360",
        freq_mhz=rf_params.freq_mhz,
        tx_power_dbm=rf_params.tx_power_dbm,
        channel_bandwidth_mhz=rf_params.channel_bandwidth_mhz,
    )

