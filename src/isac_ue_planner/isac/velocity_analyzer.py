#!/usr/bin/env python3
"""
Radial Velocity Estimation from PBCH Telemetry

This module correctly processes the PBCH telemetry data to estimate radial velocity
using the refined CFO (cfo_est) rather than the coarse frequency offset (freq_offset_hz).

The key insight is that:
- freq_offset_hz: Coarse PSS/SSS-based frequency offset (tens of kHz, mostly LO error)
- cfo_est: Refined PBCH-DMRS CFO (few hundred Hz, true Doppler)

Author: Tiami Labs
Date: 2025-01-27
"""

import json
import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Any
import matplotlib.pyplot as plt
from scipy import signal
from scipy.stats import linregress
import logging
import os

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class RadialVelocityEstimator:
    """
    Correctly estimates radial velocity from PBCH telemetry data.
    
    The key correction is using cfo_est (refined CFO) instead of freq_offset_hz
    (coarse frequency offset) for Doppler calculation.
    """
    
    def __init__(self, 
                 dl_carrier_freq: float = 3562860606.0,  # 3.56 GHz
                 speed_of_light: float = 300000000.0,    # m/s
                 baseline_window_ms: int = 5000,         # 5s baseline window
                 min_baseline_samples: int = 10):        # Minimum samples for baseline
        
        self.dl_carrier_freq = dl_carrier_freq
        self.speed_of_light = speed_of_light
        self.baseline_window_ms = baseline_window_ms
        self.min_baseline_samples = min_baseline_samples
        
        # Baseline tracking
        self.baseline_cfo = None
        self.baseline_timestamp = None
        self.baseline_pci = None
        self.baseline_samples = []  # Store samples for averaging
        
        # Results storage
        self.velocity_history = []
        self.cfo_history = []
        self.timestamps = []
        
        logger.info(f"Initialized RadialVelocityEstimator with carrier freq: {dl_carrier_freq/1e9:.2f} GHz")
    
    def calculate_radial_velocity(self, cfo_est: float, baseline_cfo: float = None) -> float:
        """
        Calculate radial velocity from CFO using the correct formula.
        
        Args:
            cfo_est: Refined CFO from nr_ue_pbch_freq_offset() (Hz)
            baseline_cfo: Baseline CFO to subtract (Hz)
            
        Returns:
            Radial velocity in m/s (positive = moving away, negative = moving toward)
        """
        # Use baseline correction if provided
        if baseline_cfo is not None:
            doppler_freq = cfo_est - baseline_cfo
        else:
            doppler_freq = cfo_est
            
        # Calculate radial velocity: v = (c * f_d) / f_c
        # where: c = speed of light, f_d = Doppler frequency, f_c = carrier frequency
        radial_velocity = (self.speed_of_light * doppler_freq) / self.dl_carrier_freq
        
        return radial_velocity
    
    def update_baseline(self, cfo_est: float, timestamp_ms: int, pci: int) -> bool:
        """
        Update the baseline CFO for a stationary period.
        
        Args:
            cfo_est: Current CFO estimate
            timestamp_ms: Current timestamp
            pci: Physical Cell ID
            
        Returns:
            True if baseline was updated, False otherwise
        """
        # Check if we need to establish a new baseline
        if (self.baseline_pci != pci or 
            self.baseline_timestamp is None or
            timestamp_ms - self.baseline_timestamp > self.baseline_window_ms):
            
            # Reset baseline collection
            self.baseline_samples = []
            self.baseline_timestamp = timestamp_ms
            self.baseline_pci = pci
            
            logger.info(f"Starting new baseline collection for PCI {pci}")
            return True
        
        # Add sample to baseline collection
        self.baseline_samples.append(cfo_est)
        
        # Update baseline if we have enough samples
        if len(self.baseline_samples) >= self.min_baseline_samples:
            self.baseline_cfo = np.mean(self.baseline_samples)
            logger.info(f"Updated baseline CFO: {self.baseline_cfo:.2f} Hz (from {len(self.baseline_samples)} samples)")
        
        return False
    
    def process_telemetry_record(self, record: Dict) -> Optional[Dict]:
        """
        Process a single telemetry record and estimate radial velocity.
        
        Args:
            record: Telemetry record from JSON
            
        Returns:
            Dictionary with velocity estimation results, or None if invalid
        """
        try:
            # Extract key fields
            timestamp_ms = record.get('timestamp_ms', 0)
            pci = record.get('pci', 0)
            cfo_est = record.get('cfo_est', 0.0)
            freq_offset_hz = record.get('freq_offset_hz', 0.0)
            dl_carrier_freq = record.get('dl_carrier_freq', self.dl_carrier_freq)
            
            # Validate data
            if timestamp_ms == 0 or pci == 0:
                return None
                
            # Update baseline if needed
            baseline_updated = self.update_baseline(cfo_est, timestamp_ms, pci)
            
            # Calculate radial velocity using the CORRECT field (cfo_est) with baseline correction
            radial_velocity = self.calculate_radial_velocity(cfo_est, self.baseline_cfo)
            
            # Also calculate using the WRONG field for comparison
            wrong_velocity = self.calculate_radial_velocity(freq_offset_hz, None)
            
            # Store results
            result = {
                'timestamp_ms': timestamp_ms,
                'pci': pci,
                'cfo_est': cfo_est,
                'freq_offset_hz': freq_offset_hz,
                'radial_velocity_mps': radial_velocity,
                'wrong_velocity_mps': wrong_velocity,
                'baseline_cfo': self.baseline_cfo,
                'baseline_updated': baseline_updated,
                'dl_carrier_freq': dl_carrier_freq,
                'doppler_freq': cfo_est - (self.baseline_cfo or 0.0)
            }
            
            # Store in history
            self.velocity_history.append(radial_velocity)
            self.cfo_history.append(cfo_est)
            self.timestamps.append(timestamp_ms)
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing telemetry record: {e}")
            return None
    
    def process_telemetry_file(self, filename: str) -> List[Dict]:
        """
        Process a telemetry JSON file and return velocity estimates.
        
        Args:
            filename: Path to telemetry JSON file
            
        Returns:
            List of velocity estimation results
        """
        logger.info(f"Processing telemetry file: {filename}")
        
        try:
            with open(filename, 'r') as f:
                data = json.load(f)
            
            if not isinstance(data, list):
                logger.error("Expected JSON array")
                return []
            
            results = []
            for record in data:
                result = self.process_telemetry_record(record)
                if result:
                    results.append(result)
            
            logger.info(f"Processed {len(results)} valid records from {len(data)} total records")
            return results
            
        except Exception as e:
            logger.error(f"Error reading telemetry file: {e}")
            return []
    
    def get_statistics(self) -> Dict:
        """
        Get statistics about the velocity estimates.
        
        Returns:
            Dictionary with velocity statistics
        """
        if not self.velocity_history:
            return {}
        
        velocities = np.array(self.velocity_history)
        cfos = np.array(self.cfo_history)
        
        stats = {
            'num_samples': len(velocities),
            'velocity_mean_mps': np.mean(velocities),
            'velocity_std_mps': np.std(velocities),
            'velocity_min_mps': np.min(velocities),
            'velocity_max_mps': np.max(velocities),
            'cfo_mean_hz': np.mean(cfos),
            'cfo_std_hz': np.std(cfos),
            'cfo_min_hz': np.min(cfos),
            'cfo_max_hz': np.max(cfos)
        }
        
        return stats
    
    def plot_velocity_timeline(self, save_path: str = None):
        """
        Plot velocity timeline and CFO over time.
        
        Args:
            save_path: Optional path to save the plot
        """
        if not self.velocity_history:
            logger.warning("No velocity data to plot")
            return
        
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8))
        
        # Convert timestamps to relative time in seconds
        timestamps_s = [(ts - self.timestamps[0]) / 1000.0 for ts in self.timestamps]
        
        # Plot 1: Radial Velocity
        ax1.plot(timestamps_s, self.velocity_history, 'b-', linewidth=1, alpha=0.7)
        ax1.set_ylabel('Radial Velocity (m/s)')
        ax1.set_title('Radial Velocity Timeline (Corrected)')
        ax1.grid(True, alpha=0.3)
        ax1.axhline(y=0, color='r', linestyle='--', alpha=0.5)
        
        # Add statistics
        stats = self.get_statistics()
        if stats:
            ax1.text(0.02, 0.98, f'Mean: {stats["velocity_mean_mps"]:.2f} m/s\nStd: {stats["velocity_std_mps"]:.2f} m/s', 
                    transform=ax1.transAxes, verticalalignment='top',
                    bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))
        
        # Plot 2: CFO over time
        ax2.plot(timestamps_s, self.cfo_history, 'g-', linewidth=1, alpha=0.7)
        ax2.set_ylabel('CFO (Hz)')
        ax2.set_xlabel('Time (seconds)')
        ax2.set_title('CFO Timeline (cfo_est)')
        ax2.grid(True, alpha=0.3)
        ax2.axhline(y=0, color='r', linestyle='--', alpha=0.5)
        
        # Add baseline line if available
        if self.baseline_cfo is not None:
            ax2.axhline(y=self.baseline_cfo, color='orange', linestyle='--', 
                       label=f'Baseline: {self.baseline_cfo:.2f} Hz')
            ax2.legend()
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            logger.info(f"Saved velocity plot to: {save_path}")
        
        plt.show()
    
    def compare_fields(self, results: List[Dict]) -> None:
        """
        Compare the correct vs wrong velocity calculation.
        
        Args:
            results: List of velocity estimation results
        """
        if not results:
            logger.warning("No results to compare")
            return
        
        correct_velocities = [r['radial_velocity_mps'] for r in results]
        wrong_velocities = [r['wrong_velocity_mps'] for r in results]
        
        print("\n" + "="*60)
        print("VELOCITY ESTIMATION COMPARISON")
        print("="*60)
        print(f"Using cfo_est (CORRECT):")
        print(f"  Mean velocity: {np.mean(correct_velocities):.2f} m/s")
        print(f"  Std velocity:  {np.std(correct_velocities):.2f} m/s")
        print(f"  Range:         {np.min(correct_velocities):.2f} to {np.max(correct_velocities):.2f} m/s")
        print()
        print(f"Using freq_offset_hz (WRONG):")
        print(f"  Mean velocity: {np.mean(wrong_velocities):.2f} m/s")
        print(f"  Std velocity:  {np.std(wrong_velocities):.2f} m/s")
        print(f"  Range:         {np.min(wrong_velocities):.2f} to {np.max(wrong_velocities):.2f} m/s")
        print()
        
        # Safe division
        if np.std(correct_velocities) > 0.001:
            improvement = np.std(wrong_velocities) / np.std(correct_velocities)
            print(f"Improvement factor: {improvement:.1f}x")
        else:
            print(f"Improvement factor: Correct velocity has very low variance")
        print("="*60)


class VelocityAnalyzer:
    """
    Velocity analyzer for GPS-telemetry comparison.
    
    This class provides the interface expected by core.py while using
    the corrected RadialVelocityEstimator internally.
    """
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.estimator = RadialVelocityEstimator()
        self.comparisons = []
        
    def analyze_velocity_accuracy(self, matched_measurements: List) -> Dict[str, Any]:
        """
        Analyze velocity estimation accuracy.
        
        Args:
            matched_measurements: List of matched GPS-telemetry measurements
            
        Returns:
            Dictionary with velocity analysis results
        """
        if not matched_measurements:
            self.logger.warning("No matched measurements provided")
            return {}
        
        # Process telemetry data for velocity estimation
        telemetry_records = []
        for measurement in matched_measurements:
            if hasattr(measurement, 'telemetry_data') and measurement.telemetry_data:
                telemetry_records.append(measurement.telemetry_data)
        
        # Use the corrected estimator to process telemetry
        results = []
        for record in telemetry_records:
            result = self.estimator.process_telemetry_record(record)
            if result:
                results.append(result)
        
        # Calculate statistics
        stats = self.estimator.get_statistics()
        
        return {
            'velocity_estimates': results,
            'statistics': stats,
            'num_measurements': len(matched_measurements),
            'num_processed': len(results)
        }
    
    def generate_velocity_plots(self, output_dir: str = "./velocity_analysis") -> Dict[str, str]:
        """
        Generate velocity analysis plots.
        
        Args:
            output_dir: Output directory for plots
            
        Returns:
            Dictionary mapping plot names to file paths
        """
        os.makedirs(output_dir, exist_ok=True)
        plot_files = {}
        
        # Generate velocity timeline plot
        if self.estimator.velocity_history:
            timeline_path = os.path.join(output_dir, "velocity_timeline.png")
            self.estimator.plot_velocity_timeline(timeline_path)
            plot_files['velocity_timeline'] = timeline_path
        
        return plot_files
    
    def export_velocity_analysis(self, output_dir: str = "./velocity_analysis") -> str:
        """
        Export velocity analysis results to CSV.
        
        Args:
            output_dir: Output directory for CSV file
            
        Returns:
            Path to exported CSV file
        """
        os.makedirs(output_dir, exist_ok=True)
        
        # Create DataFrame from velocity estimates
        if hasattr(self.estimator, 'velocity_history') and self.estimator.velocity_history:
            data = []
            for i, velocity in enumerate(self.estimator.velocity_history):
                data.append({
                    'timestamp_ms': self.estimator.timestamps[i] if i < len(self.estimator.timestamps) else 0,
                    'radial_velocity_mps': velocity,
                    'cfo_est_hz': self.estimator.cfo_history[i] if i < len(self.estimator.cfo_history) else 0
                })
            
            df = pd.DataFrame(data)
            csv_path = os.path.join(output_dir, "velocity_analysis.csv")
            df.to_csv(csv_path, index=False)
            
            self.logger.info(f"Exported velocity analysis to {csv_path}")
            return csv_path
        
        return ""


def main():
    """Example usage of the RadialVelocityEstimator."""
    
    # Initialize estimator
    estimator = RadialVelocityEstimator()
    
    # Example: Process telemetry file
    telemetry_file = "../../../tiami_data/pbch_telemetry.json"
    
    try:
        results = estimator.process_telemetry_file(telemetry_file)
        
        if results:
            # Show statistics
            stats = estimator.get_statistics()
            print("\nVelocity Estimation Statistics:")
            for key, value in stats.items():
                print(f"  {key}: {value}")
            
            # Compare correct vs wrong calculation
            estimator.compare_fields(results)
            
            # Plot results
            estimator.plot_velocity_timeline("velocity_timeline.png")
            
        else:
            print("No valid results found")
            
    except FileNotFoundError:
        print(f"Telemetry file not found: {telemetry_file}")
        print("Please ensure the telemetry file exists and contains valid JSON data.")


if __name__ == "__main__":
    main() 