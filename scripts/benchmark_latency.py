"""Benchmark latency of models."""

import argparse
import logging
import time
from pathlib import Path

import numpy as np
from PIL import Image

from tiny_vlm_360.config import load_config
from tiny_vlm_360.logging_config import setup_logging
from tiny_vlm_360.models import minicpm_v2, qwen2_vl_2b, slm_backend

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def benchmark_vlm(vlm, num_runs: int = 5) -> Dict[str, float]:
    """
    Benchmark VLM inference latency.

    Args:
        vlm: Loaded VLM model
        num_runs: Number of runs for averaging

    Returns:
        Dictionary with timing statistics
    """
    # Create a dummy image
    dummy_image = np.random.randint(0, 255, (512, 512, 3), dtype=np.uint8)
    prompt = "Describe this image."

    times = []
    for i in range(num_runs):
        start = time.time()
        _ = vlm.infer(dummy_image, prompt)
        elapsed = time.time() - start
        times.append(elapsed)
        logger.info(f"Run {i+1}/{num_runs}: {elapsed:.2f}s")

    return {
        "mean": sum(times) / len(times),
        "min": min(times),
        "max": max(times),
        "std": (sum((t - sum(times) / len(times)) ** 2 for t in times) / len(times)) ** 0.5,
    }


def benchmark_slm(slm, num_runs: int = 5) -> Dict[str, float]:
    """
    Benchmark SLM inference latency.

    Args:
        slm: Loaded SLM model
        num_runs: Number of runs for averaging

    Returns:
        Dictionary with timing statistics
    """
    prompt = "Summarize the following: " + " ".join(["test"] * 100)

    times = []
    for i in range(num_runs):
        start = time.time()
        _ = slm.infer(prompt)
        elapsed = time.time() - start
        times.append(elapsed)
        logger.info(f"Run {i+1}/{num_runs}: {elapsed:.2f}s")

    return {
        "mean": sum(times) / len(times),
        "min": min(times),
        "max": max(times),
        "std": (sum((t - sum(times) / len(times)) ** 2 for t in times) / len(times)) ** 0.5,
    }


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Benchmark model latency")
    parser.add_argument(
        "--config",
        type=str,
        default="configs/default.yaml",
        help="Path to default config",
    )
    parser.add_argument(
        "--model-config",
        type=str,
        default="configs/models.minicpm.yaml",
        help="Path to model config",
    )
    parser.add_argument(
        "--num-runs",
        type=int,
        default=5,
        help="Number of benchmark runs",
    )

    args = parser.parse_args()

    cfg = load_config(args.config, args.model_config)
    setup_logging(cfg)

    # Benchmark VLM
    logger.info("Benchmarking VLM...")
    vlm_cfg = cfg.get("vlm", {})
    model_id = vlm_cfg.get("model_id", "")

    if "minicpm" in model_id.lower():
        vlm = minicpm_v2.MiniCPMV2.load_from_config(vlm_cfg)
    elif "qwen" in model_id.lower():
        vlm = qwen2_vl_2b.Qwen2VL2B.load_from_config(vlm_cfg)
    else:
        vlm = minicpm_v2.MiniCPMV2.load_from_config(vlm_cfg)

    vlm_stats = benchmark_vlm(vlm, args.num_runs)
    logger.info(f"VLM stats: {vlm_stats}")

    # Benchmark SLM
    logger.info("Benchmarking SLM...")
    slm_cfg = cfg.get("slm", {})
    slm = slm_backend.SLMBackend.load_from_config(slm_cfg)
    slm_stats = benchmark_slm(slm, args.num_runs)
    logger.info(f"SLM stats: {slm_stats}")


if __name__ == "__main__":
    main()

