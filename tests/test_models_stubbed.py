"""Tests for model interfaces (stubbed implementations)."""

import numpy as np
import pytest

from tiny_vlm_360.models.base_slm import BaseSLM
from tiny_vlm_360.models.base_vlm import BaseVLM


class StubVLM(BaseVLM):
    """Stub VLM implementation for testing."""

    def load(self, model_path, **kwargs):
        """Stub load method."""
        pass

    def infer(self, image, prompt, **kwargs):
        """Stub infer method."""
        return f"Stub response for: {prompt}"

    @classmethod
    def load_from_config(cls, config):
        """Stub load_from_config method."""
        instance = cls()
        instance.load("stub_path")
        return instance


class StubSLM(BaseSLM):
    """Stub SLM implementation for testing."""

    def load(self, model_path, **kwargs):
        """Stub load method."""
        pass

    def infer(self, prompt, **kwargs):
        """Stub infer method."""
        return f"Stub SLM response for: {prompt}"

    @classmethod
    def load_from_config(cls, config):
        """Stub load_from_config method."""
        instance = cls()
        instance.load("stub_path")
        return instance


def test_vlm_interface():
    """Test that VLM interface works correctly."""
    vlm = StubVLM()
    vlm.load("test_path")

    image = np.random.randint(0, 255, (256, 256, 3), dtype=np.uint8)
    result = vlm.infer(image, "test prompt")

    assert isinstance(result, str)
    assert "test prompt" in result


def test_slm_interface():
    """Test that SLM interface works correctly."""
    slm = StubSLM()
    slm.load("test_path")

    result = slm.infer("test prompt")

    assert isinstance(result, str)
    assert "test prompt" in result


def test_vlm_load_from_config():
    """Test VLM load_from_config class method."""
    config = {"model_id": "test_model"}
    vlm = StubVLM.load_from_config(config)
    assert isinstance(vlm, StubVLM)


def test_slm_load_from_config():
    """Test SLM load_from_config class method."""
    config = {"model_id": "test_model"}
    slm = StubSLM.load_from_config(config)
    assert isinstance(slm, StubSLM)

