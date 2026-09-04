"""
GPS-Telemetry Matcher
Matches GPS trajectory data with telemetry data to assess UE position quality for gNB estimation
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any
import logging
from datetime import datetime, timedelta
import os

@dataclass
class MatchedMeasurement:
    """Combined GPS and telemetry measurement"""
    # GPS data
    gps_timestamp: pd.Timestamp
    gps_lat: float
    gps_lon: float
    gps_altitude: float
    gps_velocity_x: float
    gps_velocity_y: float
    gps_velocity_z: float
    gps_horizontal_accuracy: float
    gps_vertical_accuracy: float
    
    # Telemetry data
    telemetry_timestamp: pd.Timestamp
    pci: int
    signal_strength_db: List[float]
    timing_offset_us: int
    freq_offset_hz: float
    mrc_weights: List[float]
    llr_energy: float
    
    # Velocity estimation from telemetry
    estimated_velocity_cfo_ms: float  # Velocity from CFO (m/s)
    estimated_velocity_timing_ms: float  # Velocity from timing drift (m/s)
    estimated_velocity_combined_ms: float  # Combined velocity estimate (m/s)
    velocity_estimation_quality: float  # Quality of velocity estimation (0-1)
    
    # Quality metrics
    position_quality_score: float  # 0-1, higher is better
    signal_quality_score: float   # 0-1, higher is better
    timing_quality_score: float   # 0-1, higher is better
    motion_quality_score: float   # 0-1, lower motion is better
    overall_quality_score: float  # 0-1, overall assessment
    
    # Distance from gNB (estimated)
    estimated_distance_m: float
    estimated_gps_accuracy_m: float

class GPSTelemetryMatcher:
    """Matches GPS trajectory with telemetry data for UE position assessment"""
    
    def __init__(self, time_tolerance_seconds: float = 2.0):
        self.time_tolerance_seconds = time_tolerance_seconds
        self.logger = logging.getLogger(__name__)
        
        # Constants for velocity estimation
        self.SPEED_OF_LIGHT = 299792458.0  # m/s
        self.CARRIER_FREQ_DEFAULT = 3.5e9  # Hz (3.5 GHz default)
    
    def load_gps_data(self, csv_file_path: str) -> pd.DataFrame:
        """Load GPS trajectory data from CSV"""
        try:
            # Read CSV with proper timestamp parsing
            gps_data = pd.read_csv(csv_file_path)
            
            # Convert timestamp to datetime - use default parsing to handle microseconds
            gps_data['timestamp'] = pd.to_datetime(gps_data['time'], utc=True)
            
            # Rename columns for consistency
            gps_data = gps_data.rename(columns={
                'location.lat': 'latitude',
                'location.lon': 'longitude',
                'velocity.x': 'velocity_x',
                'velocity.y': 'velocity_y', 
                'velocity.z': 'velocity_z',
                'horizontal_accuracy': 'horizontal_accuracy',
                'vertical_accuracy': 'vertical_accuracy'
            })
            
            # Sort by timestamp
            gps_data = gps_data.sort_values('timestamp')
            
            self.logger.info(f"Loaded {len(gps_data)} GPS measurements from {csv_file_path}")
            return gps_data
            
        except Exception as e:
            self.logger.error(f"Error loading GPS data: {e}")
            return pd.DataFrame()
    
    def load_telemetry_data(self, telemetry_files: List[str]) -> pd.DataFrame:
        """Load telemetry data from JSON files"""
        all_telemetry = []
        
        for file_path in telemetry_files:
            try:
                # Load JSON data
                with open(file_path, 'r') as f:
                    telemetry_data = pd.read_json(f)
                
                # Convert timestamp to datetime
                telemetry_data['timestamp'] = pd.to_datetime(telemetry_data['timestamp_ms'], unit='ms', utc=True)
                
                all_telemetry.append(telemetry_data)
                self.logger.info(f"Loaded {len(telemetry_data)} telemetry records from {file_path}")
                
            except Exception as e:
                self.logger.warning(f"Error loading telemetry file {file_path}: {e}")
        
        if all_telemetry:
            combined_telemetry = pd.concat(all_telemetry, ignore_index=True)
            combined_telemetry = combined_telemetry.sort_values('timestamp')
            return combined_telemetry
        else:
            return pd.DataFrame()
    
    def match_gps_telemetry(self, gps_data: pd.DataFrame, telemetry_data: pd.DataFrame) -> List[MatchedMeasurement]:
        """Match GPS data with telemetry data based on timestamps"""
        matched_measurements = []
        
        if gps_data.empty or telemetry_data.empty:
            self.logger.warning("Empty GPS or telemetry data")
            return matched_measurements
        
        # Find matching timestamps within tolerance
        for _, gps_row in gps_data.iterrows():
            gps_timestamp = gps_row['timestamp']
            
            # Find telemetry measurements within time tolerance
            time_diff = abs(telemetry_data['timestamp'] - gps_timestamp)
            matching_mask = time_diff <= pd.Timedelta(seconds=self.time_tolerance_seconds)
            matching_telemetry = telemetry_data[matching_mask]
            
            if not matching_telemetry.empty:
                # Use the closest telemetry measurement
                closest_idx = time_diff[matching_mask].idxmin()
                telemetry_row = telemetry_data.loc[closest_idx]
                
                # Create matched measurement
                matched = self._create_matched_measurement(gps_row, telemetry_row, gps_timestamp, telemetry_row['timestamp'])
                if matched:
                    matched_measurements.append(matched)
        
        self.logger.info(f"Matched {len(matched_measurements)} GPS-telemetry pairs")
        return matched_measurements
    
    def _create_matched_measurement(self, gps_row: pd.Series, telemetry_row: pd.Series, 
                                   gps_timestamp: pd.Timestamp, telemetry_timestamp: pd.Timestamp) -> Optional[MatchedMeasurement]:
        """Create a matched measurement with quality assessment and velocity estimation"""
        try:
            # Extract GPS data
            gps_lat = gps_row['latitude']
            gps_lon = gps_row['longitude']
            gps_altitude = gps_row['altitude']
            gps_velocity_x = gps_row['velocity_x']
            gps_velocity_y = gps_row['velocity_y']
            gps_velocity_z = gps_row['velocity_z']
            gps_horizontal_accuracy = gps_row['horizontal_accuracy']
            gps_vertical_accuracy = gps_row['vertical_accuracy']
            
            # Extract telemetry data
            pci = telemetry_row.get('pci', 0)
            signal_strength_db = telemetry_row.get('channel_level_db', [-100])
            timing_offset_us = telemetry_row.get('current_timing_offset_us', 0)
            freq_offset_hz = telemetry_row.get('freq_offset_hz', 0)
            mrc_weights = telemetry_row.get('mrc_weights', [1.0])
            llr_energy = telemetry_row.get('llr_energy', 0)
            
            # Phase 2: Velocity estimation from telemetry
            estimated_velocity_cfo = self._estimate_velocity_from_cfo(telemetry_row)
            estimated_velocity_timing = self._estimate_velocity_from_timing(telemetry_row)
            estimated_velocity_combined = self._combine_velocity_estimates(
                estimated_velocity_cfo, estimated_velocity_timing, telemetry_row
            )
            velocity_estimation_quality = self._assess_velocity_estimation_quality(telemetry_row)
            
            # Calculate quality scores
            position_quality = self._assess_position_quality(gps_row)
            signal_quality = self._assess_signal_quality(telemetry_row)
            timing_quality = self._assess_timing_quality(telemetry_row)
            motion_quality = self._assess_motion_quality(gps_row)
            
            # Overall quality score
            overall_quality = self._calculate_overall_quality(
                position_quality, signal_quality, timing_quality, motion_quality
            )
            
            # Estimate distance from gNB (rough approximation)
            estimated_distance = self._estimate_distance_from_gps(gps_row, telemetry_row)
            
            return MatchedMeasurement(
                gps_timestamp=gps_timestamp,
                gps_lat=gps_lat,
                gps_lon=gps_lon,
                gps_altitude=gps_altitude,
                gps_velocity_x=gps_velocity_x,
                gps_velocity_y=gps_velocity_y,
                gps_velocity_z=gps_velocity_z,
                gps_horizontal_accuracy=gps_horizontal_accuracy,
                gps_vertical_accuracy=gps_vertical_accuracy,
                telemetry_timestamp=telemetry_timestamp,
                pci=pci,
                signal_strength_db=signal_strength_db,
                timing_offset_us=timing_offset_us,
                freq_offset_hz=freq_offset_hz,
                mrc_weights=mrc_weights,
                llr_energy=llr_energy,
                estimated_velocity_cfo_ms=estimated_velocity_cfo,
                estimated_velocity_timing_ms=estimated_velocity_timing,
                estimated_velocity_combined_ms=estimated_velocity_combined,
                velocity_estimation_quality=velocity_estimation_quality,
                position_quality_score=position_quality,
                signal_quality_score=signal_quality,
                timing_quality_score=timing_quality,
                motion_quality_score=motion_quality,
                overall_quality_score=overall_quality,
                estimated_distance_m=estimated_distance,
                estimated_gps_accuracy_m=gps_horizontal_accuracy
            )
            
        except Exception as e:
            self.logger.warning(f"Error creating matched measurement: {e}")
            return None
    
    def _assess_position_quality(self, gps_row: pd.Series) -> float:
        """Assess GPS position quality"""
        # Factors: horizontal accuracy, vertical accuracy, velocity
        horizontal_accuracy = gps_row['horizontal_accuracy']
        vertical_accuracy = gps_row['vertical_accuracy']
        
        # Calculate velocity magnitude
        velocity_x = gps_row['velocity_x']
        velocity_y = gps_row['velocity_y']
        velocity_z = gps_row['velocity_z']
        velocity_magnitude = np.sqrt(velocity_x**2 + velocity_y**2 + velocity_z**2)
        
        # Position accuracy score (0-1, higher is better)
        accuracy_score = max(0, 1 - (horizontal_accuracy / 50))  # 0-50m range
        
        # Velocity score (lower velocity is better for positioning)
        velocity_score = max(0, 1 - (velocity_magnitude / 10))  # 0-10 m/s range
        
        # Combined position quality
        position_quality = (accuracy_score * 0.7 + velocity_score * 0.3)
        
        return max(0, min(1, position_quality))
    
    def _assess_signal_quality(self, telemetry_row: pd.Series) -> float:
        """Assess signal quality from telemetry"""
        signal_strengths = telemetry_row.get('channel_level_db', [-100])
        llr_energy = telemetry_row.get('llr_energy', 0)
        
        if not isinstance(signal_strengths, list):
            signal_strengths = [-100]
        
        # Best signal strength
        best_signal = max(signal_strengths) if signal_strengths else -100
        
        # Signal strength score (-100 to -50 dBm range)
        signal_score = max(0, min(1, (best_signal + 100) / 50))
        
        # LLR energy score (higher is better)
        llr_score = max(0, min(1, llr_energy / 100))
        
        # Combined signal quality
        signal_quality = (signal_score * 0.8 + llr_score * 0.2)
        
        return max(0, min(1, signal_quality))
    
    def _assess_timing_quality(self, telemetry_row: pd.Series) -> float:
        """Assess timing quality from telemetry"""
        timing_offset = abs(telemetry_row.get('current_timing_offset_us', 0))
        freq_offset = abs(telemetry_row.get('freq_offset_hz', 0))
        
        # Timing offset score (lower is better)
        timing_score = max(0, 1 - (timing_offset / 100))  # 0-100 μs range
        
        # Frequency offset score (lower is better)
        freq_score = max(0, 1 - (freq_offset / 2000))  # 0-2000 Hz range
        
        # Combined timing quality
        timing_quality = (timing_score * 0.7 + freq_score * 0.3)
        
        return max(0, min(1, timing_quality))
    
    def _assess_motion_quality(self, gps_row: pd.Series) -> float:
        """Assess motion quality (lower motion is better for positioning)"""
        velocity_x = gps_row['velocity_x']
        velocity_y = gps_row['velocity_y']
        velocity_z = gps_row['velocity_z']
        
        # Calculate velocity magnitude
        velocity_magnitude = np.sqrt(velocity_x**2 + velocity_y**2 + velocity_z**2)
        
        # Motion quality (lower velocity = higher quality)
        motion_quality = max(0, 1 - (velocity_magnitude / 5))  # 0-5 m/s range
        
        return max(0, min(1, motion_quality))
    
    def _calculate_overall_quality(self, position_quality: float, signal_quality: float,
                                  timing_quality: float, motion_quality: float) -> float:
        """Calculate overall quality score"""
        # Weighted combination
        weights = {
            'position': 0.25,
            'signal': 0.30,
            'timing': 0.25,
            'motion': 0.20
        }
        
        overall_quality = (
            weights['position'] * position_quality +
            weights['signal'] * signal_quality +
            weights['timing'] * timing_quality +
            weights['motion'] * motion_quality
        )
        
        return max(0, min(1, overall_quality))
    
    def _estimate_distance_from_gps(self, gps_row: pd.Series, telemetry_row: pd.Series) -> float:
        """Estimate distance from gNB based on signal strength and timing"""
        # This is a rough estimation - in reality you'd need gNB location
        signal_strengths = telemetry_row.get('channel_level_db', [-100])
        timing_offset = telemetry_row.get('current_timing_offset_us', 0)
        
        if not isinstance(signal_strengths, list):
            signal_strengths = [-100]
        
        best_signal = max(signal_strengths) if signal_strengths else -100
        
        # Rough distance estimation based on signal strength
        # Assuming free space path loss model
        # Distance ∝ 1/sqrt(signal_power)
        estimated_distance = 1000 * (10 ** ((best_signal + 100) / 20))  # meters
        
        return max(50, min(5000, estimated_distance))  # Clamp to reasonable range
    
    def _estimate_velocity_from_cfo(self, telemetry_row: pd.Series) -> float:
        """Estimate velocity from CFO using Doppler effect"""
        freq_offset_hz = telemetry_row.get('freq_offset_hz', 0)
        dl_carrier_freq = telemetry_row.get('dl_carrier_freq', self.CARRIER_FREQ_DEFAULT)
        
        # Velocity from CFO: v = (Δf * c) / f_c
        # This gives radial velocity component
        if dl_carrier_freq > 0:
            velocity_radial = (freq_offset_hz * self.SPEED_OF_LIGHT) / dl_carrier_freq
            return velocity_radial
        else:
            return 0.0
    
    def _estimate_velocity_from_timing(self, telemetry_row: pd.Series) -> float:
        """Estimate velocity from timing drift"""
        current_timing_offset_us = telemetry_row.get('current_timing_offset_us', 0)
        initial_timing_offset_us = telemetry_row.get('initial_timing_offset_us', 0)
        timing_measurement_timestamp_ms = telemetry_row.get('timing_measurement_timestamp_ms', 0)
        
        # Calculate timing drift
        timing_drift_us = current_timing_offset_us - initial_timing_offset_us
        
        # Calculate time interval (assuming baseline was established)
        # For now, use a default interval if baseline timing is not available
        dt_s = 0.02  # 20ms default (one SSB period)
        
        if timing_measurement_timestamp_ms > 0:
            # Try to calculate actual time interval
            baseline_timestamp_ms = telemetry_row.get('baseline_timing_timestamp_ms', 0)
            if baseline_timestamp_ms > 0:
                dt_s = (timing_measurement_timestamp_ms - baseline_timestamp_ms) / 1000.0
        
        # Velocity from timing drift: v = (Δτ * c) / Δt
        if dt_s > 0:
            velocity_radial = (timing_drift_us * 1e-6 * self.SPEED_OF_LIGHT) / dt_s
            return velocity_radial
        else:
            return 0.0
    
    def _combine_velocity_estimates(self, velocity_cfo: float, velocity_timing: float, 
                                   telemetry_row: pd.Series) -> float:
        """Combine CFO and timing-based velocity estimates"""
        # Weight based on signal quality
        signal_strengths = telemetry_row.get('channel_level_db', [-100])
        if not isinstance(signal_strengths, list):
            signal_strengths = [-100]
        
        best_signal = max(signal_strengths) if signal_strengths else -100
        
        # CFO is more reliable at higher SNR
        if best_signal > -80:  # Good signal
            cfo_weight = 0.7
            timing_weight = 0.3
        else:  # Poor signal
            cfo_weight = 0.3
            timing_weight = 0.7
        
        # Combine estimates
        combined_velocity = cfo_weight * velocity_cfo + timing_weight * velocity_timing
        
        return combined_velocity
    
    def _assess_velocity_estimation_quality(self, telemetry_row: pd.Series) -> float:
        """Assess quality of velocity estimation"""
        signal_strengths = telemetry_row.get('channel_level_db', [-100])
        freq_offset_hz = abs(telemetry_row.get('freq_offset_hz', 0))
        timing_offset_us = abs(telemetry_row.get('current_timing_offset_us', 0))
        
        if not isinstance(signal_strengths, list):
            signal_strengths = [-100]
        
        best_signal = max(signal_strengths) if signal_strengths else -100
        
        # Signal quality score
        signal_score = max(0, min(1, (best_signal + 100) / 50))  # -100 to -50 dBm range
        
        # Frequency offset stability (lower is better)
        freq_stability = max(0, 1 - (freq_offset_hz / 2000))  # 0-2000 Hz range
        
        # Timing stability (lower is better)
        timing_stability = max(0, 1 - (timing_offset_us / 100))  # 0-100 μs range
        
        # Combined quality score
        quality_score = (signal_score * 0.5 + freq_stability * 0.3 + timing_stability * 0.2)
        
        return max(0, min(1, quality_score))
    
    def get_best_ue_positions(self, matched_measurements: List[MatchedMeasurement],
                              min_quality_score: float = 0.7, max_positions: int = 10) -> List[MatchedMeasurement]:
        """Get the best UE positions for gNB estimation"""
        # Filter by minimum quality score
        good_positions = [pos for pos in matched_measurements if pos.overall_quality_score >= min_quality_score]
        
        # Sort by overall quality score (descending)
        good_positions.sort(key=lambda x: x.overall_quality_score, reverse=True)
        
        # Return top positions
        return good_positions[:max_positions]
    
    def analyze_position_distribution(self, matched_measurements: List[MatchedMeasurement]) -> Dict[str, Any]:
        """Analyze distribution of position quality"""
        if not matched_measurements:
            return {}
        
        quality_scores = [pos.overall_quality_score for pos in matched_measurements]
        
        return {
            'total_measurements': len(matched_measurements),
            'mean_quality_score': np.mean(quality_scores),
            'std_quality_score': np.std(quality_scores),
            'min_quality_score': np.min(quality_scores),
            'max_quality_score': np.max(quality_scores),
            'excellent_positions': len([s for s in quality_scores if s >= 0.8]),
            'good_positions': len([s for s in quality_scores if 0.6 <= s < 0.8]),
            'fair_positions': len([s for s in quality_scores if 0.4 <= s < 0.6]),
            'poor_positions': len([s for s in quality_scores if s < 0.4])
        }
    
    def export_results(self, matched_measurements: List[MatchedMeasurement], output_dir: str = "./gps_telemetry_results"):
        """Export matched measurements to CSV"""
        os.makedirs(output_dir, exist_ok=True)
        
        # Convert to DataFrame
        results_data = []
        for measurement in matched_measurements:
            results_data.append({
                'gps_timestamp': measurement.gps_timestamp,
                'gps_lat': measurement.gps_lat,
                'gps_lon': measurement.gps_lon,
                'gps_altitude': measurement.gps_altitude,
                'gps_velocity_x': measurement.gps_velocity_x,
                'gps_velocity_y': measurement.gps_velocity_y,
                'gps_velocity_z': measurement.gps_velocity_z,
                'gps_horizontal_accuracy': measurement.gps_horizontal_accuracy,
                'gps_vertical_accuracy': measurement.gps_vertical_accuracy,
                'telemetry_timestamp': measurement.telemetry_timestamp,
                'pci': measurement.pci,
                'signal_strength_db': measurement.signal_strength_db,
                'timing_offset_us': measurement.timing_offset_us,
                'freq_offset_hz': measurement.freq_offset_hz,
                'llr_energy': measurement.llr_energy,
                'position_quality_score': measurement.position_quality_score,
                'signal_quality_score': measurement.signal_quality_score,
                'timing_quality_score': measurement.timing_quality_score,
                'motion_quality_score': measurement.motion_quality_score,
                'overall_quality_score': measurement.overall_quality_score,
                'estimated_distance_m': measurement.estimated_distance_m,
                'estimated_gps_accuracy_m': measurement.estimated_gps_accuracy_m
            })
        
        df = pd.DataFrame(results_data)
        output_file = os.path.join(output_dir, "matched_measurements.csv")
        df.to_csv(output_file, index=False)
        
        self.logger.info(f"Exported {len(results_data)} matched measurements to {output_file}")
        return output_file 