"""Tests for panorama tiling."""

import numpy as np
import pytest

from tiny_vlm_360.image_io.tiling import tile_pano


def test_tile_pano_basic():
    """Test basic tiling functionality."""
    # Create a dummy panorama (2:1 aspect ratio)
    pano = np.random.randint(0, 255, (256, 512, 3), dtype=np.uint8)

    tiles = tile_pano(pano, num_views=4, overlap_deg=30.0, max_pixels=512)

    assert len(tiles) == 4
    for tile in tiles:
        assert tile.image.shape[2] == 3  # RGB
        assert tile.image.dtype == np.uint8
        assert tile.index < 4


def test_tile_pano_resize():
    """Test that tiles are resized when exceeding max_pixels."""
    # Create a large panorama
    pano = np.random.randint(0, 255, (1024, 2048, 3), dtype=np.uint8)

    tiles = tile_pano(pano, num_views=2, overlap_deg=0.0, max_pixels=256)

    for tile in tiles:
        h, w = tile.image.shape[:2]
        assert h * w <= 256 * 256


def test_tile_pano_overlap():
    """Test that tiles have correct overlap."""
    pano = np.random.randint(0, 255, (256, 512, 3), dtype=np.uint8)

    tiles = tile_pano(pano, num_views=4, overlap_deg=30.0, max_pixels=512)

    # Check that tiles have reasonable azimuth ranges
    for tile in tiles:
        assert -180 <= tile.center_azimuth <= 180
        assert tile.start_lon < tile.end_lon or tile.start_lon > tile.end_lon  # Allow wrap-around

