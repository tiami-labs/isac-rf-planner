"""
ISAC (Integrated Sensing and Communication) Analyzer
Handles motion detection, timing analysis, and positioning
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import logging
from scipy import signal
from scipy.spatial.distance import cdist
from sklearn.cluster import DBSCAN
from ..config import ISAC_MOTION_THRESHOLDS, DEFAULT_CONFIG

@dataclass
class MotionEvent:
    """Motion detection event"""
    timestamp: pd.Timestamp
    motion_type: str  # 'stationary', 'slow_motion', 'fast_motion'
    magnitude_us: float
    confidence: float
    location: Tuple[float, float]  # lat, lon
    pci: int

@dataclass
class PositionEstimate:
    """Position estimate from ISAC analysis"""
    timestamp: pd.Timestamp
    latitude: float
    longitude: float
    altitude: float
    confidence: float
    method: str  # 'timing_based', 'signal_strength', 'hybrid'
    pci: int
    motion_state: str

class ISACAnalyzer:
    """ISAC analysis for motion detection and positioning"""
    
    def __init__(self, config=DEFAULT_CONFIG):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.motion_events = []
        self.position_estimates = []
        
    def analyze_motion(self, telemetry_data: pd.DataFrame) -> List[MotionEvent]:
        """Analyze motion patterns from telemetry data"""
        if telemetry_data.empty:
            return []
        
        motion_events = []
        
        # Group by PCI for per-cell analysis
        for pci, cell_data in telemetry_data.groupby('pci'):
            if len(cell_data) < 2:
                continue
                
            # Sort by timestamp
            cell_data = cell_data.sort_values('timestamp')
            
            # Calculate timing changes
            cell_data['timing_change_us'] = (
                cell_data['current_timing_offset_us'] - 
                cell_data['initial_timing_offset_us']
            )
            
            # Calculate motion magnitude
            cell_data['motion_magnitude'] = np.abs(cell_data['timing_change_us'])
            
            # Detect motion events using threshold crossing
            motion_events.extend(self._detect_motion_events(cell_data, pci))
        
        self.motion_events = motion_events
        return motion_events
    
    def _detect_motion_events(self, cell_data: pd.DataFrame, pci: int) -> List[MotionEvent]:
        """Detect motion events for a specific cell"""
        events = []
        
        # Calculate rolling statistics for motion detection
        window_size = min(10, len(cell_data))
        if window_size < 3:
            return events
        
        # Calculate rolling mean and std of motion magnitude
        rolling_mean = cell_data['motion_magnitude'].rolling(window=window_size, center=True).mean()
        rolling_std = cell_data['motion_magnitude'].rolling(window=window_size, center=True).std()
        
        # Detect significant changes in motion
        threshold = self.config.motion_detection_threshold_us
        
        for i in range(1, len(cell_data)):
            current_magnitude = cell_data.iloc[i]['motion_magnitude']
            prev_magnitude = cell_data.iloc[i-1]['motion_magnitude']
            
            # Detect motion state changes
            if current_magnitude > threshold and prev_magnitude <= threshold:
                # Motion started
                motion_type = self._classify_motion(current_magnitude)
                confidence = self._calculate_motion_confidence(current_magnitude, rolling_std.iloc[i])
                
                event = MotionEvent(
                    timestamp=cell_data.iloc[i]['timestamp'],
                    motion_type=motion_type,
                    magnitude_us=current_magnitude,
                    confidence=confidence,
                    location=(cell_data.iloc[i]['latitude'], cell_data.iloc[i]['longitude']),
                    pci=pci
                )
                events.append(event)
            
            elif current_magnitude <= threshold and prev_magnitude > threshold:
                # Motion stopped
                event = MotionEvent(
                    timestamp=cell_data.iloc[i]['timestamp'],
                    motion_type='stationary',
                    magnitude_us=current_magnitude,
                    confidence=0.9,
                    location=(cell_data.iloc[i]['latitude'], cell_data.iloc[i]['longitude']),
                    pci=pci
                )
                events.append(event)
        
        return events
    
    def _classify_motion(self, magnitude_us: float) -> str:
        """Classify motion type based on magnitude"""
        if magnitude_us < ISAC_MOTION_THRESHOLDS['stationary']:
            return 'stationary'
        elif magnitude_us < ISAC_MOTION_THRESHOLDS['slow_motion']:
            return 'slow_motion'
        else:
            return 'fast_motion'
    
    def _calculate_motion_confidence(self, magnitude: float, std: float) -> float:
        """Calculate confidence in motion detection"""
        # Higher magnitude and lower variance = higher confidence
        if std == 0:
            return 0.5
        
        # Normalize magnitude to 0-1 range
        normalized_magnitude = min(magnitude / 100.0, 1.0)
        
        # Confidence based on signal-to-noise ratio
        snr = normalized_magnitude / (std + 1e-6)
        confidence = min(snr / 10.0, 1.0)
        
        return max(confidence, 0.1)  # Minimum 10% confidence
    
    def estimate_position(self, telemetry_data: pd.DataFrame) -> List[PositionEstimate]:
        """Estimate UE position using ISAC techniques"""
        if telemetry_data.empty:
            return []
        
        position_estimates = []
        
        # Group by time windows for position estimation
        time_window = pd.Timedelta(seconds=1)  # 1-second windows
        
        for pci, cell_data in telemetry_data.groupby('pci'):
            if len(cell_data) < 3:
                continue
            
            # Resample to time windows
            cell_data = cell_data.set_index('timestamp')
            resampled = cell_data.resample(time_window).agg({
                'latitude': 'mean',
                'longitude': 'mean',
                'altitude': 'mean',
                'current_timing_offset_us': 'mean',
                'channel_level_db': lambda x: list(x.iloc[-1]) if len(x) > 0 else [-999.0, -999.0],
                'motion_magnitude': 'mean',
                'llr_energy': 'mean'
            }).dropna()
            
            for timestamp, row in resampled.iterrows():
                # Estimate position using timing-based method
                timing_position = self._estimate_position_timing(row, pci)
                
                # Estimate position using signal strength
                signal_position = self._estimate_position_signal_strength(row, pci)
                
                # Combine estimates (hybrid approach)
                hybrid_position = self._combine_position_estimates(
                    timing_position, signal_position, row
                )
                
                if hybrid_position:
                    position_estimates.append(hybrid_position)
        
        self.position_estimates = position_estimates
        return position_estimates
    
    def _estimate_position_timing(self, row: pd.Series, pci: int) -> Optional[PositionEstimate]:
        """Estimate position using timing information"""
        timing_offset = row['current_timing_offset_us']
        
        # Simple timing-based positioning (placeholder for more sophisticated algorithms)
        # In a real implementation, this would use multilateration with multiple cells
        
        # For now, use the provided coordinates with timing-based confidence
        confidence = max(0.1, 1.0 - abs(timing_offset) / 100.0)
        
        return PositionEstimate(
            timestamp=row.name,
            latitude=row['latitude'],
            longitude=row['longitude'],
            altitude=row['altitude'],
            confidence=confidence,
            method='timing_based',
            pci=pci,
            motion_state=self._classify_motion(row['motion_magnitude'])
        )
    
    def _estimate_position_signal_strength(self, row: pd.Series, pci: int) -> Optional[PositionEstimate]:
        """Estimate position using signal strength information"""
        channel_levels = row['channel_level_db']
        
        if not isinstance(channel_levels, list):
            return None
        
        # Calculate average signal strength
        valid_levels = [level for level in channel_levels if level > -999.0]
        if not valid_levels:
            return None
        
        avg_signal_strength = np.mean(valid_levels)
        
        # Signal strength-based confidence
        # Higher signal strength = higher confidence
        confidence = max(0.1, min(1.0, (avg_signal_strength + 100) / 100))
        
        return PositionEstimate(
            timestamp=row.name,
            latitude=row['latitude'],
            longitude=row['longitude'],
            altitude=row['altitude'],
            confidence=confidence,
            method='signal_strength',
            pci=pci,
            motion_state=self._classify_motion(row['motion_magnitude'])
        )
    
    def _combine_position_estimates(self, timing_pos: PositionEstimate, 
                                   signal_pos: PositionEstimate, 
                                   row: pd.Series) -> Optional[PositionEstimate]:
        """Combine multiple position estimates"""
        if not timing_pos or not signal_pos:
            return timing_pos or signal_pos
        
        # Weighted combination based on confidence
        total_confidence = timing_pos.confidence + signal_pos.confidence
        
        if total_confidence == 0:
            return None
        
        # Weighted average
        weight_timing = timing_pos.confidence / total_confidence
        weight_signal = signal_pos.confidence / total_confidence
        
        combined_lat = (timing_pos.latitude * weight_timing + 
                       signal_pos.latitude * weight_signal)
        combined_lon = (timing_pos.longitude * weight_timing + 
                       signal_pos.longitude * weight_signal)
        combined_alt = (timing_pos.altitude * weight_timing + 
                       signal_pos.altitude * weight_signal)
        
        # Combined confidence (geometric mean)
        combined_confidence = np.sqrt(timing_pos.confidence * signal_pos.confidence)
        
        return PositionEstimate(
            timestamp=timing_pos.timestamp,
            latitude=combined_lat,
            longitude=combined_lon,
            altitude=combined_alt,
            confidence=combined_confidence,
            method='hybrid',
            pci=timing_pos.pci,
            motion_state=timing_pos.motion_state
        )
    
    def analyze_trajectory(self, telemetry_data: pd.DataFrame) -> Dict[str, Any]:
        """Analyze UE trajectory patterns"""
        if telemetry_data.empty:
            return {}
        
        trajectory_analysis = {
            'total_distance_km': 0.0,
            'average_speed_kmh': 0.0,
            'motion_patterns': {},
            'coverage_analysis': {},
            'signal_quality_trends': {}
        }
        
        # Group by PCI for trajectory analysis
        for pci, cell_data in telemetry_data.groupby('pci'):
            if len(cell_data) < 2:
                continue
            
            cell_data = cell_data.sort_values('timestamp')
            
            # Calculate trajectory metrics
            trajectory = self._calculate_trajectory_metrics(cell_data)
            trajectory_analysis['motion_patterns'][pci] = trajectory
            
            # Coverage analysis
            coverage = self._analyze_coverage(cell_data)
            trajectory_analysis['coverage_analysis'][pci] = coverage
            
            # Signal quality trends
            quality_trends = self._analyze_signal_quality_trends(cell_data)
            trajectory_analysis['signal_quality_trends'][pci] = quality_trends
        
        return trajectory_analysis
    
    def _calculate_trajectory_metrics(self, cell_data: pd.DataFrame) -> Dict[str, Any]:
        """Calculate trajectory metrics for a cell"""
        if len(cell_data) < 2:
            return {}
        
        # Calculate distances between consecutive points
        distances = []
        speeds = []
        
        for i in range(1, len(cell_data)):
            prev = cell_data.iloc[i-1]
            curr = cell_data.iloc[i]
            
            # Calculate distance using Haversine formula
            distance = self._haversine_distance(
                prev['latitude'], prev['longitude'],
                curr['latitude'], curr['longitude']
            )
            distances.append(distance)
            
            # Calculate speed
            time_diff = (curr['timestamp'] - prev['timestamp']).total_seconds() / 3600  # hours
            if time_diff > 0:
                speed = distance / time_diff  # km/h
                speeds.append(speed)
        
        return {
            'total_distance_km': sum(distances),
            'average_speed_kmh': np.mean(speeds) if speeds else 0.0,
            'max_speed_kmh': max(speeds) if speeds else 0.0,
            'motion_duration_hours': (cell_data['timestamp'].max() - 
                                    cell_data['timestamp'].min()).total_seconds() / 3600
        }
    
    def _haversine_distance(self, lat1: float, lon1: float, 
                           lat2: float, lon2: float) -> float:
        """Calculate distance between two points using Haversine formula"""
        R = 6371  # Earth's radius in kilometers
        
        lat1_rad = np.radians(lat1)
        lon1_rad = np.radians(lon1)
        lat2_rad = np.radians(lat2)
        lon2_rad = np.radians(lon2)
        
        dlat = lat2_rad - lat1_rad
        dlon = lon2_rad - lon1_rad
        
        a = (np.sin(dlat/2)**2 + 
             np.cos(lat1_rad) * np.cos(lat2_rad) * np.sin(dlon/2)**2)
        c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1-a))
        
        return R * c
    
    def _analyze_coverage(self, cell_data: pd.DataFrame) -> Dict[str, Any]:
        """Analyze coverage quality for a cell"""
        # Calculate coverage metrics
        signal_strengths = []
        for levels in cell_data['channel_level_db']:
            if isinstance(levels, list):
                valid_levels = [level for level in levels if level > -999.0]
                if valid_levels:
                    signal_strengths.extend(valid_levels)
        
        if not signal_strengths:
            return {}
        
        return {
            'average_signal_strength_db': np.mean(signal_strengths),
            'signal_strength_std_db': np.std(signal_strengths),
            'coverage_area_km2': 0.0,  # Would need multiple cells for this
            'coverage_quality': 'good' if np.mean(signal_strengths) > -80 else 'poor'
        }
    
    def _analyze_signal_quality_trends(self, cell_data: pd.DataFrame) -> Dict[str, Any]:
        """Analyze signal quality trends over time"""
        if len(cell_data) < 2:
            return {}
        
        # Calculate trends
        signal_trends = {
            'signal_strength_trend': 'stable',
            'quality_improvement': 0.0,
            'interference_detected': False
        }
        
        # Simple trend analysis
        if len(cell_data) >= 10:
            first_half = cell_data.iloc[:len(cell_data)//2]
            second_half = cell_data.iloc[len(cell_data)//2:]
            
            first_avg = np.mean([level for levels in first_half['channel_level_db'] 
                               if isinstance(levels, list) 
                               for level in levels if level > -999.0])
            second_avg = np.mean([level for levels in second_half['channel_level_db'] 
                                if isinstance(levels, list) 
                                for level in levels if level > -999.0])
            
            if second_avg > first_avg + 5:
                signal_trends['signal_strength_trend'] = 'improving'
            elif second_avg < first_avg - 5:
                signal_trends['signal_strength_trend'] = 'degrading'
            
            signal_trends['quality_improvement'] = second_avg - first_avg
        
        return signal_trends
    
    def get_motion_summary(self) -> Dict[str, Any]:
        """Get summary of motion analysis"""
        if not self.motion_events:
            return {}
        
        motion_counts = {}
        motion_durations = {}
        
        for event in self.motion_events:
            motion_type = event.motion_type
            motion_counts[motion_type] = motion_counts.get(motion_type, 0) + 1
        
        return {
            'total_events': len(self.motion_events),
            'motion_distribution': motion_counts,
            'average_confidence': np.mean([event.confidence for event in self.motion_events]),
            'high_confidence_events': len([e for e in self.motion_events if e.confidence > 0.8])
        } 