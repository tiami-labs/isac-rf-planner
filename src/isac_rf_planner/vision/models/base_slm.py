"""Base interface for small language models (text-only)."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, Union


class BaseSLM(ABC):
    """Interface for a small language model (text → text)."""

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
    def infer(self, prompt: str, **kwargs: Any) -> str:
        """
        Run inference on a text prompt.

        Args:
            prompt: Text prompt
            **kwargs: Additional inference options

        Returns:
            Generated text response
        """
        ...

    @classmethod
    @abstractmethod
    def load_from_config(cls, config: Dict[str, Any]) -> "BaseSLM":
        """
        Create and load a model instance from configuration.

        Args:
            config: Configuration dictionary

        Returns:
            Loaded model instance
        """
        ...

