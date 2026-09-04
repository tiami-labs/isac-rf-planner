#!/usr/bin/env python3
"""
RF Planner Example Script
Demonstrates how to use the RF Planner system with sample data
"""

import os
import json
import pandas as pd
from datetime import datetime, timedelta
import numpy as np

def create_sample_telemetry_data():
    """Create sample telemetry data for demonstration"""
    
    # Create sample data
    base_time = datetime.now() - timedelta(hours=2)
    sample_data = []
    
    # Generate sample telemetry records
    for i in range(100):
        timestamp = base_time + timedelta(minutes=i*2)
        
        # Simulate movement (circular pattern)
        angle = (i * 10) % 360
        radius = 0.001  # Small radius for demonstration
        lat = 40.7128 + radius * np.cos(np.radians(angle))
        lon = -74.0060 + radius * np.sin(np.radians(angle))
        
        # Simulate signal quality variations
        signal_strength = -70 + 10 * np.sin(i * 0.1) + np.random.normal(0, 2)
        llr_energy = 30 + 15 * np.sin(i * 0.05) + np.random.normal(0, 3)
        
        # Simulate motion
        motion_magnitude = 5 + 10 * np.sin(i * 0.2) + np.random.normal(0, 2)
        
        record = {
            "timestamp_ms": int(timestamp.timestamp() * 1000),
            "latitude": lat,
            "longitude": lon,
            "altitude": 0.0,
            "pci": 123 + (i % 3),  # 3 different cells
            "ssb_index": i % 8,
            "sample_offset": i * 1000,
            "timing_offset": i * 2,
            "initial_timing_offset_us": 10,
            "current_timing_offset_us": 10 + motion_magnitude,
            "timing_measurement_timestamp_ms": int(timestamp.timestamp() * 1000),
            "freq_offset_hz": 1250.5 + np.random.normal(0, 50),
            "nb_antennas_rx": 2,
            "effective_antennas": 2,
            "channel_level_db": [signal_strength, signal_strength - 3],
            "mrc_weights": [0.6, 0.4],
            "antenna_quality": [0.8, 0.7],
            "llr_energy": llr_energy,
            "checksum": i * 12345,
            "subcarrier_spacing": 30,
            "dl_carrier_freq": 3500000000,
            "sample_rate": 30720000,
            "frame_number_lsb4": i % 16,
            "half_frame_bit": i % 2,
            "tdd_pattern": {
                "nrofDownlinkSlots": 7,
                "nrofDownlinkSymbols": 0,
                "nrofUplinkSlots": 2,
                "nrofUplinkSymbols": 0
            },
            "cir_blob_path": "",
            "delay_samples": i * 100,
            "delay_us": motion_magnitude * 0.1,
            "cfo_est": 1250.5 + np.random.normal(0, 10),
            "decoder_state": 0,
            "iso_timestamp": timestamp.isoformat() + "Z"
        }
        
        sample_data.append(record)
    
    return sample_data

def setup_sample_environment():
    """Setup sample environment with telemetry data"""
    
    # Create directories
    os.makedirs("../tiami_data", exist_ok=True)
    os.makedirs("./rfplanner_output", exist_ok=True)
    
    # Create sample telemetry file
    sample_data = create_sample_telemetry_data()
    
    telemetry_file = "../tiami_data/pbch_telemetry_sample.json"
    with open(telemetry_file, 'w') as f:
        json.dump(sample_data, f, indent=2)
    
    print(f"Created sample telemetry file: {telemetry_file}")
    print(f"Generated {len(sample_data)} sample records")
    
    return telemetry_file

def main():
    """Main example function"""
    
    print("RF Planner Example Script")
    print("This script demonstrates the RF Planner system capabilities")
    print()
    
    # Setup sample data
    setup_sample_environment()
    
    print("\nSample data created successfully!")
    print("You can now run the RF Planner analysis:")
    print("  python -m rfplanner.main analyze --output ./rfplanner_output")
    print("  python -m rfplanner.main status")
    print("  python -m rfplanner.main visualize --output ./rfplanner_output")

if __name__ == '__main__':
    main() 