"""Command-line interface for tiny-vlm-360."""

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .logging_config import setup_logging
from .pipeline.multi_view import analyze_pano_multi_view


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="360° image → analysis using tiny VLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("image", type=str, help="Path to 360° equirectangular panorama")
    parser.add_argument(
        "--config",
        type=str,
        default="configs/default.yaml",
        help="Path to config YAML (default: configs/default.yaml)",
    )
    parser.add_argument(
        "--model-config",
        type=str,
        default="configs/models.minicpm.yaml",
        help="Path to model config YAML (default: configs/models.minicpm.yaml)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional path to save JSON output",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    # Load configuration
    try:
        cfg = load_config(args.config, args.model_config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Setup logging
    if args.verbose:
        cfg["logging"]["level"] = "DEBUG"
    setup_logging(cfg)

    # Validate image path
    image_path = Path(args.image)
    if not image_path.exists():
        print(f"Error: Image not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    # Run analysis
    try:
        result = analyze_pano_multi_view(image_path, cfg)
    except Exception as e:
        print(f"Error during analysis: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)

    # Output results
    as_json = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(as_json, encoding="utf-8")
        print(f"Results saved to {output_path}")
    else:
        print(as_json)


if __name__ == "__main__":
    main()

