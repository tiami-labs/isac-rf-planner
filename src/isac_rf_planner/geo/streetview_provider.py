"""Street View 360° panorama provider."""

import io
import logging
from pathlib import Path
from typing import Optional

import numpy as np
import requests
from PIL import Image

from ..pipeline.schemas import LatLon

logger = logging.getLogger(__name__)


class StreetViewProvider:
    """Abstract interface for fetching 360° panoramas."""

    def check_availability(self, lat: float, lon: float) -> bool:
        """
        Check if 360° panorama is available at given coordinates.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            True if panorama is available
        """
        raise NotImplementedError

    def fetch_pano(self, lat: float, lon: float) -> np.ndarray:
        """
        Fetch 360° equirectangular panorama.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Panorama as numpy array (H, W, 3) in RGB format
        """
        raise NotImplementedError


class FileStreetViewProvider(StreetViewProvider):
    """Provider that loads panoramas from local files (for testing)."""

    def __init__(self, base_dir: Optional[Path] = None):
        """
        Initialize with optional base directory.

        Args:
            base_dir: Base directory containing panorama files
        """
        self.base_dir = base_dir or Path("data/panos")

    def check_availability(self, lat: float, lon: float) -> bool:
        """Check if a panorama file exists for these coordinates."""
        # For MVP, just check if base_dir exists
        return self.base_dir.exists()

    def fetch_pano(self, lat: float, lon: float) -> np.ndarray:
        """
        Load panorama from file.

        MVP: looks for any .jpg/.png in base_dir.
        TODO: Implement proper coordinate-based file lookup.
        """
        if not self.base_dir.exists():
            raise FileNotFoundError(f"Panorama directory not found: {self.base_dir}")

        # Look for any image file in the directory
        image_files = list(self.base_dir.glob("*.jpg")) + list(self.base_dir.glob("*.png"))
        if not image_files:
            raise FileNotFoundError(f"No panorama images found in {self.base_dir}")

        # Load first available image
        img_path = image_files[0]
        logger.info(f"Loading panorama from {img_path}")
        
        # Increase PIL image size limit to handle large panoramas
        # Default limit is ~178M pixels, we'll allow up to 500M pixels
        Image.MAX_IMAGE_PIXELS = 500_000_000
        
        img = Image.open(img_path)
        img = img.convert("RGB")
        
        # Resize if image is extremely large (e.g., > 8192px width)
        # This keeps memory usage reasonable while preserving quality
        max_width = 8192
        if img.width > max_width:
            scale = max_width / img.width
            new_height = int(img.height * scale)
            logger.info(f"Resizing panorama from {img.width}x{img.height} to {max_width}x{new_height}")
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        
        return np.array(img, dtype=np.uint8)


class MapillaryStreetViewProvider(StreetViewProvider):
    """
    Mapillary Street View API provider.
    
    Free API, but requires API key (free to obtain).
    Get API key from: https://www.mapillary.com/dashboard/developers
    
    Uses Mapillary API v4 to fetch 360° panoramas.
    """

    def __init__(self, api_key: Optional[str] = None, radius_m: float = 50.0):
        """
        Initialize Mapillary provider.

        Args:
            api_key: Mapillary API key (required - get from https://www.mapillary.com/dashboard/developers)
            radius_m: Search radius in meters for nearby panoramas
        """
        self.api_key = api_key
        self.radius_m = radius_m
        self.base_url = "https://graph.mapillary.com"
        
        if not api_key:
            logger.warning(
                "Mapillary API key not provided. "
                "Get a free API key from: https://www.mapillary.com/dashboard/developers"
            )

    def check_availability(self, lat: float, lon: float) -> bool:
        """
        Check if panorama is available via Mapillary API.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            True if panorama is available
        """
        try:
            # Query for nearby images
            images = self._query_nearby_images(lat, lon, limit=1)
            return len(images) > 0
        except Exception as e:
            logger.warning(f"Failed to check Mapillary availability: {e}")
            return False

    def fetch_pano(self, lat: float, lon: float) -> np.ndarray:
        """
        Fetch 360° panorama from Mapillary.

        Args:
            lat: Latitude
            lon: Longitude

        Returns:
            Panorama as numpy array (H, W, 3) in RGB format
        """
        # Find nearest image
        images = self._query_nearby_images(lat, lon, limit=1)
        if not images:
            raise ValueError(f"No Mapillary panorama found near ({lat}, {lon})")

        image_id = images[0]["id"]
        image_data = images[0]
        
        # Extract location from geometry if available
        pano_lat = lat
        pano_lon = lon
        if "geometry" in image_data and "coordinates" in image_data["geometry"]:
            coords = image_data["geometry"]["coordinates"]
            pano_lon = coords[0]  # Longitude first in GeoJSON
            pano_lat = coords[1]  # Latitude second
        
        logger.info(
            f"Found Mapillary image: {image_id} "
            f"at ({pano_lat:.6f}, {pano_lon:.6f})"
        )

        # Get image details to find panorama URL
        # Try to get URL from initial query first (may already have it)
        pano_url = None
        if images and "thumb_2048_url" in images[0]:
            pano_url = images[0]["thumb_2048_url"]
        elif images and "thumb_1024_url" in images[0]:
            pano_url = images[0]["thumb_1024_url"]
        
        # If not in initial query, fetch image details
        if not pano_url:
            image_data = self._get_image_details(image_id)
            # Mapillary provides different sizes: thumb_256, thumb_1024, thumb_2048
            pano_url = image_data.get("thumb_2048_url") or image_data.get("thumb_1024_url")
        
        if not pano_url:
            raise ValueError(f"No panorama URL found for image {image_id}")

        logger.info(f"Downloading panorama from: {pano_url}")
        
        # Download image
        response = requests.get(pano_url, timeout=30)
        response.raise_for_status()

        # Load image
        img = Image.open(io.BytesIO(response.content))
        img = img.convert("RGB")

        # Increase PIL limit for large images
        Image.MAX_IMAGE_PIXELS = 500_000_000

        # Resize if too large
        max_width = 8192
        if img.width > max_width:
            scale = max_width / img.width
            new_height = int(img.height * scale)
            logger.info(f"Resizing panorama from {img.width}x{img.height} to {max_width}x{new_height}")
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)

        return np.array(img, dtype=np.uint8)

    def _query_nearby_images(self, lat: float, lon: float, limit: int = 1) -> list:
        """
        Query Mapillary API for nearby 360° images.

        Args:
            lat: Latitude
            lon: Longitude
            limit: Maximum number of images to return

        Returns:
            List of image metadata dictionaries
        """
        # Warn if radius is very large (API may have limits)
        if self.radius_m > 2000.0:
            logger.warning(
                f"Large search radius ({self.radius_m}m) may cause API errors. "
                "Consider using a smaller radius (max 2000m recommended)."
            )
        
        # Mapillary API v4 endpoint for searching images
        # We search for images with is_pano=true (360° panoramas)
        url = f"{self.base_url}/images"
        
        params = {
            "fields": "id,thumb_2048_url,thumb_1024_url,geometry,compass_angle",
            "bbox": self._bbox_around_point(lat, lon, self.radius_m),
            "is_pano": "true",  # Only 360° panoramas
            "limit": str(limit),
        }

        if not self.api_key:
            raise ValueError(
                "Mapillary API key required. "
                "Get a free API key from: https://www.mapillary.com/dashboard/developers"
            )
        
        params["access_token"] = self.api_key

        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            return data.get("data", [])
        except requests.exceptions.HTTPError as e:
            if response.status_code == 401:
                raise ValueError(
                    "Mapillary API authentication failed. "
                    "Please check your API key: https://www.mapillary.com/dashboard/developers"
                ) from e
            elif response.status_code == 500:
                # API error - likely due to too large bounding box
                logger.warning(
                    f"Mapillary API returned 500 error (likely due to large search radius {self.radius_m}m). "
                    "Try a smaller radius."
                )
                # Return empty list instead of raising - allows fallback to work
                return []
            else:
                logger.warning(f"Mapillary API error {response.status_code}: {response.text[:200]}")
                raise

    def _get_image_details(self, image_id: str) -> dict:
        """
        Get detailed information about a specific image.

        Args:
            image_id: Mapillary image ID

        Returns:
            Image metadata dictionary
        """
        url = f"{self.base_url}/images/{image_id}"
        params = {
            "fields": "id,thumb_2048_url,thumb_1024_url,thumb_256_url,geometry",
        }

        if not self.api_key:
            raise ValueError("Mapillary API key required")
        
        params["access_token"] = self.api_key

        response = requests.get(url, params=params, timeout=15)
        response.raise_for_status()
        return response.json()

    def _bbox_around_point(self, lat: float, lon: float, radius_m: float) -> str:
        """
        Create bounding box string for Mapillary API.

        Args:
            lat: Center latitude
            lon: Center longitude
            radius_m: Radius in meters

        Returns:
            Bounding box string: "min_lon,min_lat,max_lon,max_lat"
        """
        # Approximate: 1 degree ≈ 111km
        dlat = radius_m / 111000.0
        dlon = radius_m / (111000.0 * abs(np.cos(np.radians(lat))))

        min_lon = lon - dlon
        max_lon = lon + dlon
        min_lat = lat - dlat
        max_lat = lat + dlat

        return f"{min_lon},{min_lat},{max_lon},{max_lat}"


class GoogleStreetViewProvider(StreetViewProvider):
    """
    Google Street View API provider.

    TODO: Implement actual API integration.
    Requires API key and proper API calls.
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize with API key.

        Args:
            api_key: Google Street View API key
        """
        self.api_key = api_key
        if not api_key:
            logger.warning("GoogleStreetViewProvider initialized without API key")

    def check_availability(self, lat: float, lon: float) -> bool:
        """
        Check availability via API.

        TODO: Implement actual API call to check panorama availability.
        """
        # Stub implementation
        logger.warning("GoogleStreetViewProvider.check_availability: stub implementation")
        return True

    def fetch_pano(self, lat: float, lon: float) -> np.ndarray:
        """
        Fetch panorama via API.

        TODO: Implement actual API call to fetch panorama image.
        """
        raise NotImplementedError("GoogleStreetViewProvider.fetch_pano not yet implemented")


def create_streetview_provider(provider_type: str = "mapillary", **kwargs) -> StreetViewProvider:
    """
    Factory function to create appropriate provider.

    Args:
        provider_type: Type of provider ("file", "mapillary", or "google")
        **kwargs: Provider-specific arguments

    Returns:
        StreetViewProvider instance
    """
    if provider_type == "file":
        base_dir = kwargs.get("base_dir")
        if base_dir:
            base_dir = Path(base_dir)
        return FileStreetViewProvider(base_dir=base_dir)
    elif provider_type == "mapillary":
        return MapillaryStreetViewProvider(
            api_key=kwargs.get("api_key"),
            radius_m=kwargs.get("radius_m", 50.0),
        )
    elif provider_type == "google":
        return GoogleStreetViewProvider(api_key=kwargs.get("api_key"))
    else:
        raise ValueError(f"Unknown provider type: {provider_type}. Use 'file', 'mapillary', or 'google'")
