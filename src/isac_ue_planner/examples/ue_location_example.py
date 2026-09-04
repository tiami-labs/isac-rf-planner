#!/usr/bin/env python3
"""
UE Location Quality Assessment Example
Demonstrates how to assess UE location quality for gNB positioning
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json
import os

from ue_location_assessor import UELocationAssessor

def create_sample_telemetry_data():
    """Create sample telemetry data for demonstration"""
    # Sample data structure matching nr_pbch_telemetry_t
    sample_data = []
    
    # Base location (Singapore)
    base_lat, base_lon = 1.285095, 103.840254
    
    # Generate sample measurements
    for i in range(50):
        # Simulate UE movement
        lat_offset = np.random.normal(0, 0.001)  # ~100m movement
        lon_offset = np.random.normal(0, 0.001)
        
        # Simulate signal quality variations
        signal_strength_1 = np.random.normal(-70, 5)  # dBm
        signal_strength_2 = np.random.normal(-75, 8)  # dBm
        
        # Simulate timing variations
        timing_offset = np.random.normal(15, 10)  # microseconds
        initial_timing = np.random.normal(10, 5)
        
        # Simulate motion
        motion_magnitude = abs(timing_offset - initial_timing)
        
        # Create telemetry record
        record = {
            'timestamp': datetime.now() + timedelta(seconds=i*10),
            'latitude': base_lat + lat_offset,
            'longitude': base_lon + lon_offset,
            'altitude': 0.0,
            'pci': 123,
            'ssb_index': 0,
            'sample_offset': 123456 + i*1000,
            'timing_offset': int(timing_offset),
            'initial_timing_offset_us': int(initial_timing),
            'current_timing_offset_us': int(timing_offset),
            'timing_measurement_timestamp_ms': int((datetime.now() + timedelta(seconds=i*10)).timestamp() * 1000),
            'freq_offset_hz': np.random.normal(1250, 100),
            'nb_antennas_rx': 2,
            'effective_antennas': 2,
            'channel_level_db': [signal_strength_1, signal_strength_2],
            'mrc_weights': [0.6, 0.4],
            'antenna_quality': [0.8, 0.7],
            'llr_energy': np.random.normal(45, 5),
            'checksum': 12345 + i,
            'subcarrier_spacing': 30,
            'dl_carrier_freq': 3500000000,
            'sample_rate': 30720000,
            'frame_number_lsb4': i % 16,
            'half_frame_bit': i % 2,
            'tdd_pattern': {
                'nrofDownlinkSlots': 7,
                'nrofDownlinkSymbols': 0,
                'nrofUplinkSlots': 2,
                'nrofUplinkSymbols': 0
            },
            'cir_blob_path': f'cir_data_{i}.bin',
            'delay_samples': 1234 + i*10,
            'delay_us': timing_offset,
            'cfo_est': np.random.normal(1250, 50),
            'decoder_state': 0,
            'iso_timestamp': (datetime.now() + timedelta(seconds=i*10)).isoformat()
        }
        
        sample_data.append(record)
    
    return pd.DataFrame(sample_data)

def main():
    """Main example function"""
    print("UE Location Quality Assessment Example")
    print("=" * 50)
    
    # Create sample telemetry data
    print("Creating sample telemetry data...")
    telemetry_data = create_sample_telemetry_data()
    print(f"Created {len(telemetry_data)} telemetry records")
    
    # Initialize UE Location Assessor
    print("\nInitializing UE Location Assessor...")
    assessor = UELocationAssessor()
    
    # Assess location quality
    print("Assessing UE location quality...")
    quality_assessments = assessor.assess_location_quality(telemetry_data)
    print(f"Assessed {len(quality_assessments)} locations")
    
    # Get best locations
    print("\nFinding best UE locations for gNB positioning...")
    best_locations = assessor.get_best_locations(quality_assessments, min_score=0.6, max_locations=5)
    
    print(f"\nTop {len(best_locations)} UE locations for gNB positioning:")
    print("-" * 80)
    
    for i, location in enumerate(best_locations, 1):
        print(f"\n{i}. Location Quality Score: {location.location_score:.3f}")
        print(f"   Position: ({location.latitude:.6f}, {location.longitude:.6f})")
        print(f"   Signal Strength: {location.signal_strength_db:.1f} dBm")
        print(f"   Signal Stability: {location.signal_stability:.3f}")
        print(f"   SNR Estimate: {location.snr_estimate_db:.1f} dB")
        print(f"   Timing Accuracy: {location.timing_accuracy_us:.1f} μs")
        print(f"   Timing Stability: {location.timing_stability:.3f}")
        print(f"   Antenna Diversity: {location.antenna_diversity:.3f}")
        print(f"   Motion Impact: {location.motion_impact:.3f}")
        print(f"   Positioning Confidence: {location.positioning_confidence:.3f}")
    
    # Analyze location distribution
    print("\n" + "=" * 50)
    print("Location Quality Distribution Analysis:")
    print("-" * 50)
    
    distribution = assessor.analyze_location_distribution(quality_assessments)
    
    if distribution:
        print(f"Total Locations: {distribution['total_locations']}")
        print(f"Mean Quality Score: {distribution['mean_score']:.3f}")
        print(f"Standard Deviation: {distribution['std_score']:.3f}")
        print(f"Score Range: {distribution['min_score']:.3f} - {distribution['max_score']:.3f}")
        print(f"\nQuality Categories:")
        print(f"  Excellent (≥0.8): {distribution['excellent_locations']}")
        print(f"  Good (0.6-0.8): {distribution['good_locations']}")
        print(f"  Fair (0.4-0.6): {distribution['fair_locations']}")
        print(f"  Poor (<0.4): {distribution['poor_locations']}")
    
    # Save results
    print("\n" + "=" * 50)
    print("Saving assessment results...")
    
    # Create output directory
    os.makedirs('./ue_location_results', exist_ok=True)
    
    # Save quality assessments to CSV
    quality_df = pd.DataFrame([
        {
            'timestamp': loc.timestamp,
            'latitude': loc.latitude,
            'longitude': loc.longitude,
            'pci': loc.pci,
            'location_score': loc.location_score,
            'signal_strength_db': loc.signal_strength_db,
            'signal_stability': loc.signal_stability,
            'snr_estimate_db': loc.snr_estimate_db,
            'timing_accuracy_us': loc.timing_accuracy_us,
            'timing_stability': loc.timing_stability,
            'measurement_quality': loc.measurement_quality,
            'positioning_confidence': loc.positioning_confidence,
            'antenna_diversity': loc.antenna_diversity,
            'mrc_quality': loc.mrc_quality,
            'motion_impact': loc.motion_impact
        }
        for loc in quality_assessments
    ])
    
    quality_df.to_csv('./ue_location_results/location_quality_assessments.csv', index=False)
    print("Saved location quality assessments to: ./ue_location_results/location_quality_assessments.csv")
    
    # Save best locations
    best_locations_df = pd.DataFrame([
        {
            'rank': i,
            'latitude': loc.latitude,
            'longitude': loc.longitude,
            'location_score': loc.location_score,
            'signal_strength_db': loc.signal_strength_db,
            'positioning_confidence': loc.positioning_confidence
        }
        for i, loc in enumerate(best_locations, 1)
    ])
    
    best_locations_df.to_csv('./ue_location_results/best_locations.csv', index=False)
    print("Saved best locations to: ./ue_location_results/best_locations.csv")
    
    # Save distribution analysis
    with open('./ue_location_results/distribution_analysis.json', 'w') as f:
        json.dump(distribution, f, indent=2, default=str)
    print("Saved distribution analysis to: ./ue_location_results/distribution_analysis.json")
    
    print("\nExample completed successfully!")
    print("\nKey Insights:")
    print("- Higher location scores indicate better UE positions for gNB estimation")
    print("- Signal stability and timing accuracy are crucial for positioning")
    print("- Motion impact reduces positioning quality")
    print("- Multi-antenna diversity improves measurement reliability")

if __name__ == "__main__":
    main() 