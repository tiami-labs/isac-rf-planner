"""End-to-end pipeline tests (with stubbed models)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from tiny_vlm_360.config import load_config
from tiny_vlm_360.pipeline.multi_view import analyze_pano_multi_view


@pytest.fixture
def dummy_pano():
    """Create a dummy panorama image."""
    return np.random.randint(0, 255, (256, 512, 3), dtype=np.uint8)


@pytest.fixture
def test_config():
    """Load test configuration."""
    return load_config("configs/default.yaml", "configs/models.minicpm.yaml")


@patch("tiny_vlm_360.pipeline.multi_view.minicpm_v2.MiniCPMV2")
@patch("tiny_vlm_360.pipeline.multi_view.slm_backend.SLMBackend")
def test_pipeline_end_to_end(mock_slm_class, mock_vlm_class, dummy_pano, test_config, tmp_path):
    """Test end-to-end pipeline with mocked models."""
    # Create a dummy image file
    from PIL import Image

    img_path = tmp_path / "test_pano.jpg"
    Image.fromarray(dummy_pano).save(img_path)

    # Mock VLM
    mock_vlm = MagicMock()
    mock_vlm.infer.return_value = json.dumps({
        "scene_type": "test scene",
        "objects": [{"label": "object1", "direction": "front"}],
        "hazards": ["hazard1"],
        "direction": "front",
    })
    mock_vlm_instance = MagicMock()
    mock_vlm_instance.load_from_config.return_value = mock_vlm
    mock_vlm_class.load_from_config = mock_vlm_instance.load_from_config

    # Mock SLM
    mock_slm = MagicMock()
    mock_slm.infer.return_value = json.dumps({
        "scene_type": "aggregated scene",
        "objects": [{"label": "object1", "direction": "front", "confidence": 0.9}],
        "hazards": ["hazard1"],
        "summary": "Test summary",
    })
    mock_slm_instance = MagicMock()
    mock_slm_instance.load_from_config.return_value = mock_slm
    mock_slm_class.load_from_config = mock_slm_instance.load_from_config

    # Run pipeline
    result = analyze_pano_multi_view(img_path, test_config)

    # Verify result structure
    assert "scene_type" in result
    assert "objects" in result
    assert "hazards" in result
    assert "summary" in result

    # Verify models were called
    assert mock_vlm.infer.call_count > 0
    assert mock_slm.infer.call_count == 1

