"""
Velocity Integration Example
Demonstrates the complete phased implementation of GPS integration and velocity estimation
"""

import os
import sys
import logging
from typing import Dict, List, Any
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Add the parent directory to the path to import rfplanner
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rfplanner.core import RFPlanner
from rfplanner.config import DEFAULT_CONFIG
from rfplanner.gps_telemetry_matcher import GPSTelemetryMatcher
from rfplanner.velocity_analyzer import VelocityAnalyzer

def create_sample_gps_data(output_file: str = "sample_gps_data.csv"):
    """Create sample GPS data for testing"""
    print("Creating sample GPS data...")
    
    # Generate sample GPS trajectory
    base_lat, base_lon = 40.7128, -74.0060  # New York coordinates
    num_points = 100
    
    gps_data = []
    for i in range(num_points):
        # Simulate movement with varying velocity
        timestamp = datetime.now() + timedelta(seconds=i*2)
        
        # Simulate different velocity patterns
        if i < 20:
            # Stationary period
            velocity_x, velocity_y, velocity_z = 0.0, 0.0, 0.0
        elif i < 50:
            # Moving period
            velocity_x, velocity_y, velocity_z = 5.0, 2.0, 0.0
        else:
            # High velocity period
            velocity_x, velocity_y, velocity_z = 15.0, 8.0, 0.0
        
        # Calculate position based on velocity
        lat_offset = (velocity_y * i * 2) / 111000  # Approximate conversion
        lon_offset = (velocity_x * i * 2) / (111000 * np.cos(np.radians(base_lat)))
        
        record = {
            'time': timestamp.isoformat() + 'Z',
            'location.lat': base_lat + lat_offset,
            'location.lon': base_lon + lon_offset,
            'altitude': 10.0 + np.random.normal(0, 1),
            'velocity.x': velocity_x + np.random.normal(0, 0.5),
            'velocity.y': velocity_y + np.random.normal(0, 0.5),
            'velocity.z': velocity_z + np.random.normal(0, 0.1),
            'horizontal_accuracy': 2.0 + np.random.exponential(1),
            'vertical_accuracy': 3.0 + np.random.exponential(1)
        }
        gps_data.append(record)
    
    df = pd.DataFrame(gps_data)
    df.to_csv(output_file, index=False)
    print(f"Created sample GPS data with {len(df)} points: {output_file}")
    return output_file

def create_sample_telemetry_data(output_file: str = "sample_telemetry_data.json"):
    """Create sample telemetry data for testing"""
    print("Creating sample telemetry data...")
    
    telemetry_data = []
    base_timestamp = datetime.now()
    
    for i in range(100):
        timestamp = base_timestamp + timedelta(seconds=i*2)
        
        # Simulate different signal conditions
        if i < 30:
            # Good signal conditions
            signal_strength = -65 + np.random.normal(0, 5)
            freq_offset = 1250 + np.random.normal(0, 50)
            timing_offset = 10 + np.random.normal(0, 5)
        elif i < 70:
            # Moderate signal conditions
            signal_strength = -80 + np.random.normal(0, 10)
            freq_offset = 1250 + np.random.normal(0, 100)
            timing_offset = 15 + np.random.normal(0, 10)
        else:
            # Poor signal conditions
            signal_strength = -95 + np.random.normal(0, 15)
            freq_offset = 1250 + np.random.normal(0, 200)
            timing_offset = 25 + np.random.normal(0, 20)
        
        record = {
            'timestamp_ms': int(timestamp.timestamp() * 1000),
            'latitude': 0.0,  # Placeholder - will be replaced by GPS
            'longitude': 0.0,  # Placeholder - will be replaced by GPS
            'altitude': 0.0,
            'pci': 123 + (i % 3),
            'ssb_index': i % 8,
            'sample_offset': 123456 + i*1000,
            'timing_offset': int(timing_offset),
            'initial_timing_offset_us': 10,
            'current_timing_offset_us': int(timing_offset),
            'timing_measurement_timestamp_ms': int(timestamp.timestamp() * 1000),
            'freq_offset_hz': freq_offset,
            'nb_antennas_rx': 2,
            'effective_antennas': 2,
            'channel_level_db': [signal_strength, signal_strength - 3],
            'mrc_weights': [0.6, 0.4],
            'antenna_quality': [0.8, 0.7],
            'llr_energy': 45 + np.random.normal(0, 5),
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
            'cfo_est': freq_offset + np.random.normal(0, 10),
            'decoder_state': 0,
            'iso_timestamp': timestamp.isoformat() + 'Z'
        }
        telemetry_data.append(record)
    
    # Save as JSON
    import json
    with open(output_file, 'w') as f:
        json.dump(telemetry_data, f, indent=2)
    
    print(f"Created sample telemetry data with {len(telemetry_data)} records: {output_file}")
    return output_file

def run_phase_1_gps_integration():
    """Phase 1: GPS Position Integration"""
    print("\n" + "="*60)
    print("PHASE 1: GPS Position Integration")
    print("="*60)
    
    # Create sample data
    gps_file = create_sample_gps_data()
    telemetry_file = create_sample_telemetry_data()
    
    # Initialize RF Planner
    planner = RFPlanner()
    
    # Load and match GPS with telemetry
    print("\nMatching GPS data with telemetry...")
    matched_measurements = planner.match_gps_telemetry(gps_file, [telemetry_file])
    
    if matched_measurements:
        print(f"✓ Successfully matched {len(matched_measurements)} GPS-telemetry pairs")
        
        # Show sample of matched data
        print("\nSample matched measurements:")
        for i, measurement in enumerate(matched_measurements[:3]):
            print(f"  {i+1}. GPS: ({measurement.gps_lat:.6f}, {measurement.gps_lon:.6f})")
            print(f"     Telemetry: PCI {measurement.pci}, Signal: {measurement.signal_strength_db}")
            print(f"     Quality Score: {measurement.overall_quality_score:.3f}")
    else:
        print("✗ No matched measurements found")
    
    return matched_measurements

def run_phase_2_velocity_estimation(matched_measurements: List):
    """Phase 2: Velocity Estimation from Telemetry"""
    print("\n" + "="*60)
    print("PHASE 2: Velocity Estimation from Telemetry")
    print("="*60)
    
    if not matched_measurements:
        print("✗ No matched measurements available for velocity estimation")
        return None
    
    # Initialize velocity analyzer
    velocity_analyzer = VelocityAnalyzer()
    
    # Analyze velocity accuracy
    print("\nAnalyzing velocity estimation accuracy...")
    velocity_analysis = velocity_analyzer.analyze_velocity_accuracy(matched_measurements)
    
    if velocity_analysis:
        stats = velocity_analysis.get('statistics', {})
        print(f"✓ Velocity analysis complete:")
        print(f"  - Total measurements: {stats.get('total_measurements', 0)}")
        print(f"  - Mean error: {stats.get('mean_error_mps', 0):.2f} m/s")
        print(f"  - RMSE: {stats.get('rmse_mps', 0):.2f} m/s")
        print(f"  - Correlation: {stats.get('correlation_coefficient', 0):.3f}")
        
        # Show sample velocity comparisons
        comparisons = velocity_analysis.get('comparisons', [])
        if comparisons:
            print("\nSample velocity comparisons:")
            for i, comp in enumerate(comparisons[:3]):
                print(f"  {i+1}. GPS: {comp.gps_velocity_magnitude:.2f} m/s")
                print(f"     Estimated: {comp.estimated_velocity_combined:.2f} m/s")
                print(f"     Error: {comp.velocity_error:.2f} m/s ({comp.velocity_error_percentage:.1f}%)")
    else:
        print("✗ Velocity analysis failed")
    
    return velocity_analysis

def run_phase_3_velocity_comparison(velocity_analysis: Dict[str, Any]):
    """Phase 3: GPS-Telemetry Velocity Comparison and Visualization"""
    print("\n" + "="*60)
    print("PHASE 3: GPS-Telemetry Velocity Comparison")
    print("="*60)
    
    if not velocity_analysis:
        print("✗ No velocity analysis available for comparison")
        return None
    
    # Initialize velocity analyzer
    velocity_analyzer = VelocityAnalyzer()
    
    # Generate visualizations
    print("\nGenerating velocity analysis plots...")
    output_dir = "./velocity_analysis_results"
    plot_files = velocity_analyzer.generate_velocity_plots(output_dir)
    
    if plot_files:
        print(f"✓ Generated {len(plot_files)} visualization plots:")
        for plot_name, plot_path in plot_files.items():
            print(f"  - {plot_name}: {plot_path}")
    
    # Export analysis results
    print("\nExporting velocity analysis results...")
    csv_file = velocity_analyzer.export_velocity_analysis(output_dir)
    if csv_file:
        print(f"✓ Exported velocity analysis to: {csv_file}")
    
    # Show detailed analysis
    accuracy_analysis = velocity_analysis.get('accuracy_analysis', {})
    if accuracy_analysis:
        print("\nDetailed accuracy analysis:")
        
        # By velocity range
        by_velocity = accuracy_analysis.get('by_velocity_range', {})
        for range_name, stats in by_velocity.items():
            if stats['count'] > 0:
                print(f"  {range_name}: {stats['count']} measurements")
                print(f"    Mean error: {stats['mean_error']:.2f} m/s")
                print(f"    Mean error %: {stats['mean_error_percentage']:.1f}%")
        
        # By signal quality
        by_signal = accuracy_analysis.get('by_signal_quality', {})
        for quality_name, stats in by_signal.items():
            if stats['count'] > 0:
                print(f"  {quality_name}: {stats['count']} measurements")
                print(f"    Mean error: {stats['mean_error']:.2f} m/s")
    
    return plot_files

def run_complete_analysis():
    """Run the complete phased analysis"""
    print("VELOCITY INTEGRATION ANALYSIS")
    print("="*60)
    print("This example demonstrates the complete phased implementation of")
    print("GPS integration and velocity estimation for RF planning.")
    print()
    
    try:
        # Phase 1: GPS Position Integration
        matched_measurements = run_phase_1_gps_integration()
        
        # Phase 2: Velocity Estimation
        velocity_analysis = run_phase_2_velocity_estimation(matched_measurements)
        
        # Phase 3: Velocity Comparison and Visualization
        plot_files = run_phase_3_velocity_comparison(velocity_analysis)
        
        print("\n" + "="*60)
        print("ANALYSIS COMPLETE")
        print("="*60)
        print("✓ All phases completed successfully")
        print("✓ GPS positioning integrated")
        print("✓ Velocity estimation implemented")
        print("✓ Comprehensive analysis and visualization generated")
        print("\nNext steps:")
        print("1. Review the generated plots in ./velocity_analysis_results/")
        print("2. Analyze the velocity_analysis.csv file for detailed results")
        print("3. Use the insights to improve measurement setup and calibration")
        
    except Exception as e:
        print(f"\n✗ Analysis failed with error: {e}")
        import traceback
        traceback.print_exc()

def demonstrate_velocity_formulas():
    """Demonstrate the velocity estimation formulas"""
    print("\n" + "="*60)
    print("VELOCITY ESTIMATION FORMULAS")
    print("="*60)
    
    # Constants
    SPEED_OF_LIGHT = 299792458.0  # m/s
    CARRIER_FREQ = 3.5e9  # Hz (3.5 GHz)
    
    print("1. CFO-based velocity estimation:")
    print("   v_radial = (freq_offset_hz * c) / carrier_freq")
    print(f"   Example: freq_offset = 1250 Hz")
    print(f"   v_radial = (1250 * {SPEED_OF_LIGHT}) / {CARRIER_FREQ}")
    v_cfo = (1250 * SPEED_OF_LIGHT) / CARRIER_FREQ
    print(f"   v_radial = {v_cfo:.2f} m/s")
    
    print("\n2. Timing drift-based velocity estimation:")
    print("   v_radial = (timing_drift_us * 1e-6 * c) / dt_s")
    print(f"   Example: timing_drift = 50 μs, dt = 0.02 s")
    print(f"   v_radial = (50e-6 * {SPEED_OF_LIGHT}) / 0.02")
    v_timing = (50e-6 * SPEED_OF_LIGHT) / 0.02
    print(f"   v_radial = {v_timing:.2f} m/s")
    
    print("\n3. Combined velocity estimation:")
    print("   v_combined = w1 * v_cfo + w2 * v_timing")
    print("   where weights depend on signal quality")
    v_combined = 0.7 * v_cfo + 0.3 * v_timing
    print(f"   v_combined = 0.7 * {v_cfo:.2f} + 0.3 * {v_timing:.2f}")
    print(f"   v_combined = {v_combined:.2f} m/s")

if __name__ == "__main__":
    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    # Demonstrate formulas
    demonstrate_velocity_formulas()
    
    # Run complete analysis
    run_complete_analysis() 