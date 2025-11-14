"""Base interface for vision-language models."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Union

import numpy as np


class BaseVLM(ABC):
    """Interface for a vision-language model (image + text → text)."""

    @abstractmethod
    def load(self, model_path: Union[Path, str], **kwargs: Any) -> None:
        """
        Load the model from a path.

        Args:
            model_path: Path to model file or directory
            **kwargs: Additional loading options
        """
        ...

    @abstractmethod
    def infer(self, image: np.ndarray, prompt: str, **kwargs: Any) -> str:
        """
        Run inference on an image with a text prompt.

        Args:
            image: Input image as numpy array (H, W, C) in RGB format
            prompt: Text prompt
            **kwargs: Additional inference options

        Returns:
            Generated text response
        """
        ...

    @classmethod
    @abstractmethod
    def load_from_config(cls, config: Dict[str, Any]) -> "BaseVLM":
        """
        Create and load a model instance from configuration.

        Args:
            config: Configuration dictionary

        Returns:
            Loaded model instance
        """
        ...

