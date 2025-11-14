"""Download and prepare models."""

import argparse
import logging
from pathlib import Path
from typing import Optional

from huggingface_hub import snapshot_download

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def download_model(model_id: str, output_dir: Path, cache_dir: Optional[Path] = None) -> None:
    """
    Download a model from HuggingFace.

    Args:
        model_id: HuggingFace model ID
        output_dir: Directory to save the model
        cache_dir: Optional cache directory
    """
    logger.info(f"Downloading {model_id} to {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        snapshot_download(
            repo_id=model_id,
            local_dir=str(output_dir),
            cache_dir=str(cache_dir) if cache_dir else None,
        )
        logger.info(f"Successfully downloaded {model_id}")
    except Exception as e:
        logger.error(f"Failed to download {model_id}: {e}")
        raise


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Download models for tiny-vlm-360")
    parser.add_argument(
        "--model",
        type=str,
        choices=["minicpm", "qwen2vl", "slm"],
        help="Model to download",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="models",
        help="Output directory for models",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default=None,
        help="Cache directory for HuggingFace",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    models = {
        "minicpm": "openbmb/MiniCPM-V-2",
        "qwen2vl": "Qwen/Qwen2-VL-2B-Instruct",
        "slm": "meta-llama/Llama-3.2-1B-Instruct",
    }

    if args.model:
        model_id = models[args.model]
        model_output_dir = output_dir / args.model
        download_model(model_id, model_output_dir, cache_dir)
    else:
        # Download all models
        for name, model_id in models.items():
            model_output_dir = output_dir / name
            download_model(model_id, model_output_dir, cache_dir)


if __name__ == "__main__":
    main()

