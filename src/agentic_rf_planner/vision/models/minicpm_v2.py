"""MiniCPM-V 2.0 backend using HuggingFace transformers."""

import logging
from pathlib import Path
from typing import Any, Dict, Union

import numpy as np
import torch
from PIL import Image
from transformers import AutoProcessor, AutoModelForVision2Seq

from .base_vlm import BaseVLM

logger = logging.getLogger(__name__)


class MiniCPMV2(BaseVLM):
    """MiniCPM-V 2.0 implementation using HuggingFace transformers."""

    def __init__(self):
        self.model = None
        self.processor = None
        self.device = "cpu"

    def load(self, model_path: Union[Path, str], **kwargs: Any) -> None:
        """
        Load MiniCPM-V 2.0 model.

        Args:
            model_path: HuggingFace model ID or local path
            **kwargs: Additional options (device, quantization, etc.)
        """
        model_id = str(model_path)
        device = kwargs.get("device", "auto")
        trust_remote_code = kwargs.get("trust_remote_code", True)
        low_cpu_mem_usage = kwargs.get("low_cpu_mem_usage", True)

        # Determine device
        if device == "auto":
            if torch.cuda.is_available():
                self.device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                self.device = "mps"
            else:
                self.device = "cpu"
        else:
            self.device = device

        logger.info(f"Loading MiniCPM-V 2.0 from {model_id} on {self.device}")

        # Load processor and model
        self.processor = AutoProcessor.from_pretrained(
            model_id, trust_remote_code=trust_remote_code
        )
        self.model = AutoModelForVision2Seq.from_pretrained(
            model_id,
            trust_remote_code=trust_remote_code,
            low_cpu_mem_usage=low_cpu_mem_usage,
            torch_dtype=torch.float16 if self.device != "cpu" else torch.float32,
        )

        # Apply quantization if specified
        quantization = kwargs.get("quantization")
        if quantization == "4bit":
            from transformers import BitsAndBytesConfig

            bnb_config = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.float16,
            )
            self.model = AutoModelForVision2Seq.from_pretrained(
                model_id,
                quantization_config=bnb_config,
                trust_remote_code=trust_remote_code,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )
        elif quantization == "8bit":
            self.model = AutoModelForVision2Seq.from_pretrained(
                model_id,
                load_in_8bit=True,
                trust_remote_code=trust_remote_code,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

        self.model.to(self.device)
        self.model.eval()

        logger.info("MiniCPM-V 2.0 loaded successfully")

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
        if self.model is None or self.processor is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        # Convert numpy array to PIL Image
        if isinstance(image, np.ndarray):
            # Ensure RGB format
            if image.dtype != np.uint8:
                image = (image * 255).astype(np.uint8)
            pil_image = Image.fromarray(image)
        else:
            pil_image = image

        # Process inputs
        inputs = self.processor(images=pil_image, text=prompt, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}

        # Generate
        max_new_tokens = kwargs.get("max_new_tokens", 512)
        with torch.no_grad():
            generated_ids = self.model.generate(**inputs, max_new_tokens=max_new_tokens)
            # Decode the generated token IDs
            generated_text = self.processor.batch_decode(
                generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False
            )

        return generated_text[0] if isinstance(generated_text, list) else generated_text

    @classmethod
    def load_from_config(cls, config: Dict[str, Any]) -> "MiniCPMV2":
        """
        Create and load a model instance from configuration.

        Args:
            config: Configuration dictionary with model_id, device, quantization, etc.

        Returns:
            Loaded model instance
        """
        instance = cls()
        model_id = config.get("model_id", "openbmb/MiniCPM-V-2")
        model_path = config.get("model_path")
        if model_path:
            model_id = model_path

        device = config.get("device", "auto")
        quantization = config.get("quantization")
        trust_remote_code = config.get("trust_remote_code", True)
        low_cpu_mem_usage = config.get("low_cpu_mem_usage", True)

        instance.load(
            model_id,
            device=device,
            quantization=quantization,
            trust_remote_code=trust_remote_code,
            low_cpu_mem_usage=low_cpu_mem_usage,
        )
        return instance

