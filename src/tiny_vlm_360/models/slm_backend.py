"""Small language model backend for text aggregation."""

import logging
from pathlib import Path
from typing import Any, Dict, Union

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

from .base_slm import BaseSLM

logger = logging.getLogger(__name__)


class SLMBackend(BaseSLM):
    """Small language model implementation using HuggingFace transformers."""

    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.device = "cpu"

    def load(self, model_path: Union[Path, str], **kwargs: Any) -> None:
        """
        Load a small language model.

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

        logger.info(f"Loading SLM from {model_id} on {self.device}")

        # Load tokenizer and model
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_id, trust_remote_code=trust_remote_code
        )
        self.model = AutoModelForCausalLM.from_pretrained(
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
            self.model = AutoModelForCausalLM.from_pretrained(
                model_id,
                quantization_config=bnb_config,
                trust_remote_code=trust_remote_code,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )
        elif quantization == "8bit":
            self.model = AutoModelForCausalLM.from_pretrained(
                model_id,
                load_in_8bit=True,
                trust_remote_code=trust_remote_code,
                low_cpu_mem_usage=low_cpu_mem_usage,
            )

        self.model.to(self.device)
        self.model.eval()

        logger.info("SLM loaded successfully")

    def infer(self, prompt: str, **kwargs: Any) -> str:
        """
        Run inference on a text prompt.

        Args:
            prompt: Text prompt
            **kwargs: Additional inference options

        Returns:
            Generated text response
        """
        if self.model is None or self.tokenizer is None:
            raise RuntimeError("Model not loaded. Call load() first.")

        # Tokenize
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)

        # Generate
        max_new_tokens = kwargs.get("max_new_tokens", 512)
        with torch.no_grad():
            outputs = self.model.generate(
                **inputs, max_new_tokens=max_new_tokens, do_sample=False
            )

        # Decode
        generated_text = self.tokenizer.decode(outputs[0], skip_special_tokens=True)
        # Remove the input prompt from the output
        if generated_text.startswith(prompt):
            generated_text = generated_text[len(prompt) :].strip()

        return generated_text

    @classmethod
    def load_from_config(cls, config: Dict[str, Any]) -> "SLMBackend":
        """
        Create and load a model instance from configuration.

        Args:
            config: Configuration dictionary with model_id, device, quantization, etc.

        Returns:
            Loaded model instance
        """
        instance = cls()
        # Default to a small model like Llama 3.2 1B
        model_id = config.get("model_id", "meta-llama/Llama-3.2-1B-Instruct")
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

