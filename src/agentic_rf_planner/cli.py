"""Command-line interface for agentic RF planner."""

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .logging_config import setup_logging
from .pipeline.schemas import RFParams, LatLon
from .vision.models.minicpm_v2 import MiniCPMV2
from .vision.models.qwen2_vl_2b import Qwen2VL2B
from .agents.rf_planning_agent import run_rf_planning_for_point


def main() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Agentic RF planning with VLM-based material detection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("lat", type=float, help="Latitude")
    parser.add_argument("lon", type=float, help="Longitude")
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
        "--rf-config",
        type=str,
        default="configs/rf.params.yaml",
        help="Path to RF params config YAML (default: configs/rf.params.yaml)",
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
    parser.add_argument(
        "--ray-mode",
        choices=["2d", "3d"],
        default=None,
        help="Ray propagation mode: 2d (OSM polygons) or 3d (persisted Google mesh profiles)",
    )
    parser.add_argument("--tx-height-m", type=float, default=None, help="TX height above ground (meters)")
    parser.add_argument("--rx-height-m", type=float, default=None, help="RX height above ground (meters)")
    args = parser.parse_args()

    # Load configuration
    try:
        cfg = load_config(args.config, args.model_config, args.rf_config)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Setup logging
    if args.verbose:
        cfg["logging"]["level"] = "DEBUG"
    setup_logging(cfg)

    # Load RF params
    rf_cfg = cfg.get("rf", {})
    ray_mode = args.ray_mode or rf_cfg.get("ray_mode", "2d")
    tx_height_m = args.tx_height_m if args.tx_height_m is not None else rf_cfg.get("tx_height_m", 0.0)
    rx_height_m = args.rx_height_m if args.rx_height_m is not None else rf_cfg.get("rx_height_m", 1.5)
    rf_params = RFParams(
        freq_mhz=rf_cfg.get("freq_mhz", 3500.0),
        tx_power_dbm=rf_cfg.get("tx_power_dbm", 30.0),
        noise_floor_dbm=rf_cfg.get("noise_floor_dbm", -100.0),
        max_range_m=rf_cfg.get("max_range_m", 500.0),
        step_m=rf_cfg.get("step_m", 5.0),
        ray_mode=ray_mode,
        tx_height_m=float(tx_height_m),
        rx_height_m=float(rx_height_m),
    )

    # Load VLM
    vlm_cfg = cfg.get("vlm", {})
    model_id = vlm_cfg.get("model_id", "")

    if "minicpm" in model_id.lower():
        vlm = MiniCPMV2.load_from_config(vlm_cfg)
    elif "qwen" in model_id.lower():
        vlm = Qwen2VL2B.load_from_config(vlm_cfg)
    else:
        # Default to MiniCPM
        print("Warning: Unknown model, defaulting to MiniCPM-V 2.0", file=sys.stderr)
        vlm = MiniCPMV2.load_from_config(vlm_cfg)

    # Run RF planning
    try:
        result = run_rf_planning_for_point(
            lat=args.lat,
            lon=args.lon,
            rf_params=rf_params,
            vlm=vlm,
        )
    except Exception as e:
        print(f"Error during RF planning: {e}", file=sys.stderr)
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
