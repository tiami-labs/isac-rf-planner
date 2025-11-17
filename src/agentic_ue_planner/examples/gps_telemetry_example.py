#!/usr/bin/env python3
"""
GPS-Telemetry Matching Example
Demonstrates how to match GPS trajectory with telemetry data to find best UE positions for gNB estimation
"""

import pandas as pd
import numpy as np
import json
import os
import glob
from datetime import datetime, timedelta

from gps_telemetry_matcher import GPSTelemetryMatcher

def create_sample_telemetry_data():
    """Create sample telemetry data that matches the GPS time range"""
    # GPS data time range: 2025-07-22T07:33:51Z to 2025-07-22T08:14:34Z
    start_time = datetime(2025, 7, 22, 7, 33, 51, tzinfo=None)  # UTC
    end_time = datetime(2025, 7, 22, 8, 14, 34, tzinfo=None)    # UTC
    
    sample_data = []
    
    # Generate telemetry data every 10 seconds
    current_time = start_time
    while current_time <= end_time:
        # Simulate signal quality variations
        signal_strength_1 = np.random.normal(-70, 8)  # dBm
        signal_strength_2 = np.random.normal(-75, 10)  # dBm
        
        # Simulate timing variations
        timing_offset = np.random.normal(15, 12)  # microseconds
        freq_offset = np.random.normal(1250, 150)  # Hz
        
        # Create telemetry record
        record = {
            'timestamp_ms': int(current_time.timestamp() * 1000),
            'latitude': 1.2955 + np.random.normal(0, 0.001),  # Approximate GPS range
            'longitude': 103.7926 + np.random.normal(0, 0.001),
            'altitude': 0.0,
            'pci': 123,
            'ssb_index': 0,
            'sample_offset': 123456 + int(current_time.timestamp()),
            'timing_offset': int(timing_offset),
            'initial_timing_offset_us': int(timing_offset - np.random.normal(0, 5)),
            'current_timing_offset_us': int(timing_offset),
            'timing_measurement_timestamp_ms': int(current_time.timestamp() * 1000),
            'freq_offset_hz': freq_offset,
            'nb_antennas_rx': 2,
            'effective_antennas': 2,
            'channel_level_db': [signal_strength_1, signal_strength_2],
            'mrc_weights': [0.6, 0.4],
            'antenna_quality': [0.8, 0.7],
            'llr_energy': np.random.normal(45, 8),
            'checksum': 12345 + int(current_time.timestamp()),
            'subcarrier_spacing': 30,
            'dl_carrier_freq': 3500000000,
            'sample_rate': 30720000,
            'frame_number_lsb4': int(current_time.timestamp()) % 16,
            'half_frame_bit': int(current_time.timestamp()) % 2,
            'tdd_pattern': {
                'nrofDownlinkSlots': 7,
                'nrofDownlinkSymbols': 0,
                'nrofUplinkSlots': 2,
                'nrofUplinkSymbols': 0
            },
            'cir_blob_path': f'cir_data_{int(current_time.timestamp())}.bin',
            'delay_samples': 1234 + int(current_time.timestamp()),
            'delay_us': timing_offset,
            'cfo_est': freq_offset,
            'decoder_state': 0,
            'iso_timestamp': current_time.isoformat() + 'Z'
        }
        
        sample_data.append(record)
        current_time += timedelta(seconds=10)
    
    return sample_data

def main():
    """Main example function"""
    print("GPS-Telemetry Matching Example")
    print("=" * 50)
    
    # Initialize GPS-Telemetry Matcher
    print("Initializing GPS-Telemetry Matcher...")
    matcher = GPSTelemetryMatcher(time_tolerance_seconds=2.0)
    
    # Load GPS trajectory data
    print("\nLoading GPS trajectory data...")
    gps_file = "ue_position/00:33:50-01:14:34.csv"
    gps_data = matcher.load_gps_data(gps_file)
    
    if gps_data.empty:
        print("Error: Could not load GPS data")
        return
    
    print(f"Loaded {len(gps_data)} GPS measurements")
    print(f"Time range: {gps_data['timestamp'].min()} to {gps_data['timestamp'].max()}")
    
    # Create sample telemetry data (in real scenario, you'd load actual telemetry files)
    print("\nCreating sample telemetry data...")
    sample_telemetry = create_sample_telemetry_data()
    
    # Save sample telemetry to JSON file
    telemetry_file = "sample_telemetry.json"
    with open(telemetry_file, 'w') as f:
        json.dump(sample_telemetry, f, indent=2)
    
    # Load telemetry data
    print("Loading telemetry data...")
    telemetry_data = matcher.load_telemetry_data([telemetry_file])
    
    if telemetry_data.empty:
        print("Error: Could not load telemetry data")
        return
    
    print(f"Loaded {len(telemetry_data)} telemetry measurements")
    print(f"Time range: {telemetry_data['timestamp'].min()} to {telemetry_data['timestamp'].max()}")
    
    # Match GPS and telemetry data
    print("\nMatching GPS and telemetry data...")
    matched_measurements = matcher.match_gps_telemetry(gps_data, telemetry_data)
    
    if not matched_measurements:
        print("No matched measurements found. Check time ranges and tolerance settings.")
        return
    
    print(f"Successfully matched {len(matched_measurements)} GPS-telemetry pairs")
    
    # Get best UE positions for gNB estimation
    print("\nFinding best UE positions for gNB estimation...")
    best_positions = matcher.get_best_ue_positions(
        matched_measurements, 
        min_quality_score=0.6, 
        max_positions=10
    )
    
    print(f"\nTop {len(best_positions)} UE positions for gNB estimation:")
    print("-" * 100)
    
    for i, position in enumerate(best_positions, 1):
        print(f"\n{i}. Overall Quality Score: {position.overall_quality_score:.3f}")
        print(f"   GPS Position: ({position.gps_lat:.6f}, {position.gps_lon:.6f})")
        print(f"   GPS Altitude: {position.gps_altitude:.1f} m")
        print(f"   GPS Accuracy: {position.gps_horizontal_accuracy:.1f} m")
        print(f"   Velocity: ({position.gps_velocity_x:.1f}, {position.gps_velocity_y:.1f}, {position.gps_velocity_z:.1f}) m/s")
        print(f"   Signal Strength: {max(position.signal_strength_db):.1f} dBm")
        print(f"   Timing Offset: {position.timing_offset_us} μs")
        print(f"   Estimated Distance: {position.estimated_distance_m:.0f} m")
        print(f"   Quality Breakdown:")
        print(f"     - Position Quality: {position.position_quality_score:.3f}")
        print(f"     - Signal Quality: {position.signal_quality_score:.3f}")
        print(f"     - Timing Quality: {position.timing_quality_score:.3f}")
        print(f"     - Motion Quality: {position.motion_quality_score:.3f}")
    
    # Analyze position distribution
    print("\n" + "=" * 50)
    print("Position Quality Distribution Analysis:")
    print("-" * 50)
    
    distribution = matcher.analyze_position_distribution(matched_measurements)
    
    if distribution:
        print(f"Total Matched Measurements: {distribution['total_measurements']}")
        print(f"Mean Quality Score: {distribution['mean_quality_score']:.3f}")
        print(f"Standard Deviation: {distribution['std_quality_score']:.3f}")
        print(f"Score Range: {distribution['min_quality_score']:.3f} - {distribution['max_quality_score']:.3f}")
        print(f"\nQuality Categories:")
        print(f"  Excellent (≥0.8): {distribution['excellent_positions']}")
        print(f"  Good (0.6-0.8): {distribution['good_positions']}")
        print(f"  Fair (0.4-0.6): {distribution['fair_positions']}")
        print(f"  Poor (<0.4): {distribution['poor_positions']}")
    
    # Export results
    print("\n" + "=" * 50)
    print("Exporting results...")
    
    output_file = matcher.export_results(matched_measurements, "./gps_telemetry_results")
    print(f"Results exported to: {output_file}")
    
    # Save best positions separately
    best_positions_file = "./gps_telemetry_results/best_ue_positions.csv"
    best_df = pd.DataFrame([
        {
            'rank': i,
            'gps_lat': pos.gps_lat,
            'gps_lon': pos.gps_lon,
            'gps_altitude': pos.gps_altitude,
            'overall_quality_score': pos.overall_quality_score,
            'position_quality_score': pos.position_quality_score,
            'signal_quality_score': pos.signal_quality_score,
            'timing_quality_score': pos.timing_quality_score,
            'motion_quality_score': pos.motion_quality_score,
            'estimated_distance_m': pos.estimated_distance_m,
            'gps_horizontal_accuracy': pos.gps_horizontal_accuracy,
            'signal_strength_db': max(pos.signal_strength_db),
            'timing_offset_us': pos.timing_offset_us
        }
        for i, pos in enumerate(best_positions, 1)
    ])
    
    best_df.to_csv(best_positions_file, index=False)
    print(f"Best positions exported to: {best_positions_file}")
    
    # Clean up sample file
    if os.path.exists(telemetry_file):
        os.remove(telemetry_file)
    
    print("\nExample completed successfully!")
    print("\nKey Insights:")
    print("- Higher quality scores indicate better UE positions for gNB estimation")
    print("- GPS accuracy and low velocity improve positioning quality")
    print("- Strong signal strength and stable timing are crucial")
    print("- Motion reduces positioning quality for gNB estimation")
    print("- Best positions can be used for triangulation or gNB location estimation")

if __name__ == "__main__":
    main() 