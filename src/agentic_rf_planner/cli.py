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

    # Load RF params (all knobs from rf.params.yaml; CLI args override where provided)
    rf_cfg = cfg.get("rf", {})
    ray_mode = args.ray_mode if args.ray_mode is not None else rf_cfg.get("ray_mode", "2d")
    tx_height_m = args.tx_height_m if args.tx_height_m is not None else rf_cfg.get("tx_height_m", 0.0)
    rx_height_m = args.rx_height_m if args.rx_height_m is not None else rf_cfg.get("rx_height_m", 1.5)
    rf_params = RFParams(
        freq_mhz=rf_cfg.get("freq_mhz", 3500.0),
        tx_power_dbm=rf_cfg.get("tx_power_dbm", 30.0),
        noise_floor_dbm=rf_cfg.get("noise_floor_dbm"),
        noise_figure_db=rf_cfg.get("noise_figure_db", 7.0),
        max_range_m=rf_cfg.get("max_range_m", 2000.0),
        step_m=rf_cfg.get("step_m", 5.0),
        dtheta_deg=rf_cfg.get("dtheta_deg", 5.0),
        subcarrier_spacing_khz=rf_cfg.get("subcarrier_spacing_khz", 15.0),
        num_resource_blocks=rf_cfg.get("num_resource_blocks", 100),
        channel_bandwidth_mhz=rf_cfg.get("channel_bandwidth_mhz", 20.0),
        num_tx_antennas=rf_cfg.get("num_tx_antennas", 1),
        num_rx_antennas=rf_cfg.get("num_rx_antennas", 1),
        mimo_mode=rf_cfg.get("mimo_mode", "SISO"),
        enable_link_adaptation=rf_cfg.get("enable_link_adaptation", True),
        fixed_modulation=rf_cfg.get("fixed_modulation"),
        sectors=rf_cfg.get("sectors"),
        ray_mode=ray_mode,
        tx_height_m=float(tx_height_m),
        rx_height_m=float(rx_height_m),
        tx_antenna_gain_dbi=rf_cfg.get("tx_antenna_gain_dbi", 17.0),
        tx_feeder_loss_db=rf_cfg.get("tx_feeder_loss_db", 2.0),
        reference_signal_offset_db=rf_cfg.get("reference_signal_offset_db", -18.0),
        ue_antenna_gain_dbi=rf_cfg.get("ue_antenna_gain_dbi", 0.0),
        max_rsrp_dbm=rf_cfg.get("max_rsrp_dbm", -62.0),
        electrical_tilt_deg=rf_cfg.get("electrical_tilt_deg", 0.0),
        mechanical_tilt_deg=rf_cfg.get("mechanical_tilt_deg", 0.0),
        vertical_beamwidth_deg=rf_cfg.get("vertical_beamwidth_deg", 8.0),
        max_vertical_attenuation_db=rf_cfg.get("max_vertical_attenuation_db", 30.0),
        max_horizontal_attenuation_db=rf_cfg.get("max_horizontal_attenuation_db", 30.0),
        front_to_back_attenuation_db=rf_cfg.get("front_to_back_attenuation_db", 25.0),
        path_loss_model=rf_cfg.get("path_loss_model", "3gpp_38901"),
        propagation_scenario=rf_cfg.get("propagation_scenario", "umi_street_canyon"),
        shadow_loss_db=rf_cfg.get("shadow_loss_db", 6.0),
        shadow_decay_db_per_100m=rf_cfg.get("shadow_decay_db_per_100m", 4.0),
        shadow_loss_cap_db=rf_cfg.get("shadow_loss_cap_db", 22.0),
        diffraction_base_loss_db=rf_cfg.get("diffraction_base_loss_db", 6.0),
        diffraction_slope_db_per_100m=rf_cfg.get("diffraction_slope_db_per_100m", 3.0),
        diffraction_loss_cap_db=rf_cfg.get("diffraction_loss_cap_db", 18.0),
        canyon_recovery_max_db=rf_cfg.get("canyon_recovery_max_db", 8.0),
        canyon_recovery_slope_db_per_100m=rf_cfg.get("canyon_recovery_slope_db_per_100m", 6.0),
        termination_rsrp_dbm=rf_cfg.get("termination_rsrp_dbm", -140.0),
        building_attenuation=rf_cfg.get("building_attenuation"),
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
