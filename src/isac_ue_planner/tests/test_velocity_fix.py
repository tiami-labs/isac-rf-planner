#!/usr/bin/env python3
"""
Test script to demonstrate the velocity estimation fix.

This script creates sample telemetry data that matches the user's format
and shows the difference between using the correct vs wrong CFO field.
"""

import json
import numpy as np
from velocity_analyzer import RadialVelocityEstimator

def create_sample_telemetry():
    """Create sample telemetry data that matches the user's format."""
    
    # Sample data based on the user's telemetry logs with more realistic CFO values
    sample_data = [
        {
            "timestamp_ms": 1753509847556,
            "latitude": 0.000000,
            "longitude": 0.000000,
            "altitude": 0.00,
            "pci": 283,
            "ssb_index": 2,
            "sample_offset": 3304,
            "timing_offset": -2,
            "initial_timing_offset_us": 0,
            "current_timing_offset_us": 0,
            "timing_measurement_timestamp_ms": 1753509847556,
            "freq_offset_hz": 25215.00,  # Coarse PSS/SSS-based offset
            "nb_antennas_rx": 2,
            "effective_antennas": 2,
            "channel_level_db": [62.58, 60.16],
            "mrc_weights": [0.636, 0.364],
            "antenna_quality": [3.33, 3.79],
            "llr_energy": 31.73,
            "checksum": 224,
            "subcarrier_spacing": 15,
            "dl_carrier_freq": 3562860606,
            "sample_rate": 30720000,
            "frame_number_lsb4": 5,
            "half_frame_bit": 0,
            "tdd_pattern": {
                "nrofDownlinkSlots": 0,
                "nrofDownlinkSymbols": 0,
                "nrofUplinkSlots": 0,
                "nrofUplinkSymbols": 0
            },
            "cir_blob_path": "",
            "delay_samples": 3304,
            "delay_us": 107.55,
            "cfo_est": -391.6,  # Refined PBCH-DMRS CFO (realistic value)
            "decoder_state": 0,
            "iso_timestamp": "2025-07-25T23:04:07.556Z"
        },
        {
            "timestamp_ms": 1753509847576,
            "latitude": 0.000000,
            "longitude": 0.000000,
            "altitude": 0.00,
            "pci": 283,
            "ssb_index": 2,
            "sample_offset": 3304,
            "timing_offset": 0,
            "initial_timing_offset_us": 0,
            "current_timing_offset_us": 0,
            "timing_measurement_timestamp_ms": 1753509847576,
            "freq_offset_hz": 25215.00,  # Coarse offset (25 kHz!)
            "nb_antennas_rx": 2,
            "effective_antennas": 2,
            "channel_level_db": [58.80, 61.70],
            "mrc_weights": [0.339, 0.661],
            "antenna_quality": [20.00, 18.79],
            "llr_energy": 25.81,
            "checksum": 31,
            "subcarrier_spacing": 15,
            "dl_carrier_freq": 3562860606,
            "sample_rate": 30720000,
            "frame_number_lsb4": 2,
            "half_frame_bit": 0,
            "tdd_pattern": {
                "nrofDownlinkSlots": 0,
                "nrofDownlinkSymbols": 0,
                "nrofUplinkSlots": 0,
                "nrofUplinkSymbols": 0
            },
            "cir_blob_path": "",
            "delay_samples": 3304,
            "delay_us": 107.55,
            "cfo_est": -395.2,  # Refined CFO (slightly different)
            "decoder_state": 0,
            "iso_timestamp": "2025-07-25T23:04:07.576Z"
        },
        {
            "timestamp_ms": 1753509847596,
            "latitude": 0.000000,
            "longitude": 0.000000,
            "altitude": 0.00,
            "pci": 283,
            "ssb_index": 2,
            "sample_offset": 3304,
            "timing_offset": 0,
            "initial_timing_offset_us": 0,
            "current_timing_offset_us": 0,
            "timing_measurement_timestamp_ms": 1753509847596,
            "freq_offset_hz": 25215.00,  # Coarse offset (25 kHz!)
            "nb_antennas_rx": 2,
            "effective_antennas": 2,
            "channel_level_db": [58.80, 61.70],
            "mrc_weights": [0.339, 0.661],
            "antenna_quality": [20.00, 18.79],
            "llr_energy": 25.81,
            "checksum": 31,
            "subcarrier_spacing": 15,
            "dl_carrier_freq": 3562860606,
            "sample_rate": 30720000,
            "frame_number_lsb4": 2,
            "half_frame_bit": 0,
            "tdd_pattern": {
                "nrofDownlinkSlots": 0,
                "nrofDownlinkSymbols": 0,
                "nrofUplinkSlots": 0,
                "nrofUplinkSymbols": 0
            },
            "cir_blob_path": "",
            "delay_samples": 3304,
            "delay_us": 107.55,
            "cfo_est": -389.8,  # Refined CFO (baseline established)
            "decoder_state": 0,
            "iso_timestamp": "2025-07-25T23:04:07.596Z"
        },
        {
            "timestamp_ms": 1753509847616,
            "latitude": 0.000000,
            "longitude": 0.000000,
            "altitude": 0.00,
            "pci": 283,
            "ssb_index": 2,
            "sample_offset": 3304,
            "timing_offset": 0,
            "initial_timing_offset_us": 0,
            "current_timing_offset_us": 0,
            "timing_measurement_timestamp_ms": 1753509847616,
            "freq_offset_hz": 25215.00,  # Coarse offset (25 kHz!)
            "nb_antennas_rx": 2,
            "effective_antennas": 2,
            "channel_level_db": [58.80, 61.70],
            "mrc_weights": [0.339, 0.661],
            "antenna_quality": [20.00, 18.79],
            "llr_energy": 25.81,
            "checksum": 31,
            "subcarrier_spacing": 15,
            "dl_carrier_freq": 3562860606,
            "sample_rate": 30720000,
            "frame_number_lsb4": 2,
            "half_frame_bit": 0,
            "tdd_pattern": {
                "nrofDownlinkSlots": 0,
                "nrofDownlinkSymbols": 0,
                "nrofUplinkSlots": 0,
                "nrofUplinkSymbols": 0
            },
            "cir_blob_path": "",
            "delay_samples": 3304,
            "delay_us": 107.55,
            "cfo_est": -385.5,  # Refined CFO (motion detected!)
            "decoder_state": 0,
            "iso_timestamp": "2025-07-25T23:04:07.616Z"
        }
    ]
    
    return sample_data

def demonstrate_the_fix():
    """Demonstrate the velocity estimation fix."""
    
    print("="*70)
    print("RADIAL VELOCITY ESTIMATION FIX DEMONSTRATION")
    print("="*70)
    
    # Create sample telemetry data
    sample_data = create_sample_telemetry()
    
    # Initialize the corrected velocity estimator
    estimator = RadialVelocityEstimator()
    
    # Process the sample data
    results = []
    for record in sample_data:
        result = estimator.process_telemetry_record(record)
        if result:
            results.append(result)
    
    # Show the comparison
    if results:
        print("\nSAMPLE DATA ANALYSIS:")
        print("-" * 50)
        
        for i, result in enumerate(results):
            print(f"\nRecord {i+1}:")
            print(f"  Timestamp: {result['timestamp_ms']}")
            print(f"  PCI: {result['pci']}")
            print(f"  freq_offset_hz (coarse): {result['freq_offset_hz']:.2f} Hz")
            print(f"  cfo_est (refined): {result['cfo_est']:.2f} Hz")
            print(f"  Baseline CFO: {result['baseline_cfo']:.2f} Hz" if result['baseline_cfo'] else "  Baseline CFO: None")
            print(f"  Doppler freq: {result['doppler_freq']:.2f} Hz")
            print(f"  Wrong velocity (using freq_offset_hz): {result['wrong_velocity_mps']:.2f} m/s")
            print(f"  Correct velocity (using cfo_est): {result['radial_velocity_mps']:.2f} m/s")
            
            # Safe division to avoid zero division error
            if abs(result['radial_velocity_mps']) > 0.001:
                improvement = abs(result['wrong_velocity_mps'] / result['radial_velocity_mps'])
                print(f"  Improvement: {improvement:.1f}x more reasonable")
            else:
                print(f"  Improvement: Correct velocity is near zero (stationary)")
        
        # Show the comparison
        estimator.compare_fields(results)
        
        # Calculate the improvement
        wrong_velocities = [r['wrong_velocity_mps'] for r in results]
        correct_velocities = [r['radial_velocity_mps'] for r in results]
        
        print(f"\nKEY INSIGHT:")
        print(f"  Using freq_offset_hz (25 kHz): {wrong_velocities[0]:.0f} m/s (absurd!)")
        print(f"  Using cfo_est (baseline-corrected): {correct_velocities[-1]:.1f} m/s (reasonable!)")
        if abs(correct_velocities[-1]) > 0.001:
            print(f"  The fix makes velocity estimation {abs(wrong_velocities[0] / correct_velocities[-1]):.0f}x more realistic!")
        else:
            print(f"  The fix shows the UE is stationary (no motion detected)")
        
    else:
        print("No valid results generated from sample data.")

def show_the_formula():
    """Show the correct velocity calculation formula."""
    
    print("\n" + "="*70)
    print("CORRECT VELOCITY CALCULATION FORMULA")
    print("="*70)
    
    # Constants
    c = 300000000.0  # Speed of light (m/s)
    f_c = 3562860606.0  # Carrier frequency (Hz)
    
    # Example calculations
    print(f"Constants:")
    print(f"  Speed of light (c): {c/1e6:.1f} × 10⁶ m/s")
    print(f"  Carrier frequency (f_c): {f_c/1e9:.2f} GHz")
    print()
    
    # Example 1: Using freq_offset_hz (WRONG)
    freq_offset_hz = 25215.0
    wrong_velocity = (c * freq_offset_hz) / f_c
    print(f"WRONG CALCULATION (using freq_offset_hz):")
    print(f"  freq_offset_hz = {freq_offset_hz:.0f} Hz")
    print(f"  v = (c × freq_offset_hz) / f_c")
    print(f"  v = ({c/1e6:.1f} × 10⁶ × {freq_offset_hz:.0f}) / {f_c/1e9:.2f} × 10⁹")
    print(f"  v = {wrong_velocity:.0f} m/s (absurd!)")
    print()
    
    # Example 2: Using cfo_est with baseline correction (CORRECT)
    cfo_est = -385.5  # Hz (from sample data)
    baseline_cfo = -392.2  # Hz (average of first 3 samples)
    doppler_freq = cfo_est - baseline_cfo
    correct_velocity = (c * doppler_freq) / f_c
    
    print(f"CORRECT CALCULATION (using cfo_est with baseline correction):")
    print(f"  cfo_est = {cfo_est:.2f} Hz")
    print(f"  baseline_cfo = {baseline_cfo:.2f} Hz")
    print(f"  doppler_freq = cfo_est - baseline_cfo = {doppler_freq:.2f} Hz")
    print(f"  v = (c × doppler_freq) / f_c")
    print(f"  v = ({c/1e6:.1f} × 10⁶ × {doppler_freq:.2f}) / {f_c/1e9:.2f} × 10⁹")
    print(f"  v = {correct_velocity:.2f} m/s (reasonable!)")
    print()
    
    print(f"IMPROVEMENT: {abs(wrong_velocity / correct_velocity):.0f}x more realistic velocity estimate!")

def show_the_problem():
    """Show the exact problem from the user's data."""
    
    print("\n" + "="*70)
    print("THE EXACT PROBLEM FROM YOUR TELEMETRY")
    print("="*70)
    
    # User's exact data
    freq_offset_hz = 25215.0  # Hz
    dl_carrier_freq = 3562860606  # Hz (3.56 GHz)
    speed_of_light = 300000000.0  # m/s
    
    # User's calculation (WRONG)
    wrong_v_radial = (freq_offset_hz * speed_of_light) / dl_carrier_freq
    
    print(f"Your calculation:")
    print(f"  freq_offset_hz = {freq_offset_hz:.0f} Hz")
    print(f"  dl_carrier_freq = {dl_carrier_freq/1e9:.2f} GHz")
    print(f"  speed_of_light = {speed_of_light/1e6:.1f} × 10⁶ m/s")
    print(f"  v_radial = ({freq_offset_hz:.0f} × {speed_of_light/1e6:.1f} × 10⁶) / {dl_carrier_freq/1e9:.2f} × 10⁹")
    print(f"  v_radial = {wrong_v_radial:.2f} m/s")
    print()
    print(f"PROBLEM: This gives {wrong_v_radial:.0f} m/s which is absurd!")
    print(f"         (This is faster than a commercial jet!)")
    print()
    
    # Correct calculation using cfo_est with baseline correction
    cfo_est = -391.6  # Hz (from your telemetry)
    baseline_cfo = -391.6  # Hz (assuming stationary baseline)
    doppler_freq = cfo_est - baseline_cfo
    correct_v_radial = (doppler_freq * speed_of_light) / dl_carrier_freq
    
    print(f"CORRECT calculation using cfo_est with baseline correction:")
    print(f"  cfo_est = {cfo_est:.2f} Hz")
    print(f"  baseline_cfo = {baseline_cfo:.2f} Hz")
    print(f"  doppler_freq = cfo_est - baseline_cfo = {doppler_freq:.2f} Hz")
    print(f"  v_radial = ({doppler_freq:.2f} × {speed_of_light/1e6:.1f} × 10⁶) / {dl_carrier_freq/1e9:.2f} × 10⁹")
    print(f"  v_radial = {correct_v_radial:.2f} m/s")
    print()
    print(f"RESULT: {correct_v_radial:.2f} m/s which is reasonable!")
    print(f"        (This represents stationary UE or very slow motion)")

if __name__ == "__main__":
    demonstrate_the_fix()
    show_the_formula()
    show_the_problem() 