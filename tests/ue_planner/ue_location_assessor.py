"""
UE Location Quality Assessor
Assesses how good a UE location is for estimating unknown gNB position
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Any
import logging

@dataclass
class UELocationQuality:
    """Quality assessment of UE location for gNB positioning"""
    timestamp: pd.Timestamp
    latitude: float
    longitude: float
    pci: int
    
    # Signal quality metrics
    signal_strength_db: float  # Best antenna signal strength
    signal_stability: float    # Signal strength variance (0-1, lower is better)
    snr_estimate_db: float     # Estimated SNR
    
    # Timing quality metrics  
    timing_accuracy_us: float  # Timing measurement accuracy
    timing_stability: float    # Timing variance (0-1, lower is better)
    
    # Geometric quality metrics
    measurement_quality: float  # Overall measurement quality (0-1)
    positioning_confidence: float  # Confidence in position estimate
    
    # Multi-antenna metrics
    antenna_diversity: float   # Antenna diversity gain (0-1)
    mrc_quality: float         # MRC weight quality
    
    # Motion impact
    motion_impact: float       # How motion affects positioning (0-1, lower is better)
    
    # Overall score
    location_score: float      # Overall location quality score (0-1)

class UELocationAssessor:
    """Assesses UE location quality for gNB positioning"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
    
    def assess_location_quality(self, telemetry_data: pd.DataFrame) -> List[UELocationQuality]:
        """Assess quality of UE locations for gNB positioning"""
        if telemetry_data.empty:
            return []
        
        quality_assessments = []
        
        # Group by PCI for per-cell analysis
        for pci, cell_data in telemetry_data.groupby('pci'):
            if len(cell_data) < 3:  # Need minimum measurements
                continue
                
            # Sort by timestamp
            cell_data = cell_data.sort_values('timestamp')
            
            # Assess each measurement point
            for idx, row in cell_data.iterrows():
                quality = self._assess_single_location(row, cell_data)
                if quality:
                    quality_assessments.append(quality)
        
        return quality_assessments
    
    def _assess_single_location(self, row: pd.Series, cell_data: pd.DataFrame) -> Optional[UELocationQuality]:
        """Assess quality of a single UE location"""
        try:
            # 1. Signal Quality Assessment
            signal_quality = self._assess_signal_quality(row, cell_data)
            
            # 2. Timing Quality Assessment  
            timing_quality = self._assess_timing_quality(row, cell_data)
            
            # 3. Geometric Quality Assessment
            geometric_quality = self._assess_geometric_quality(row, cell_data)
            
            # 4. Multi-antenna Quality Assessment
            antenna_quality = self._assess_antenna_quality(row)
            
            # 5. Motion Impact Assessment
            motion_impact = self._assess_motion_impact(row, cell_data)
            
            # 6. Overall Quality Score
            location_score = self._calculate_overall_score(
                signal_quality, timing_quality, geometric_quality, 
                antenna_quality, motion_impact
            )
            
            return UELocationQuality(
                timestamp=row['timestamp'],
                latitude=row['latitude'],
                longitude=row['longitude'],
                pci=row['pci'],
                signal_strength_db=signal_quality['strength'],
                signal_stability=signal_quality['stability'],
                snr_estimate_db=signal_quality['snr'],
                timing_accuracy_us=timing_quality['accuracy'],
                timing_stability=timing_quality['stability'],
                measurement_quality=geometric_quality['measurement_quality'],
                positioning_confidence=geometric_quality['confidence'],
                antenna_diversity=antenna_quality['diversity'],
                mrc_quality=antenna_quality['mrc_quality'],
                motion_impact=motion_impact,
                location_score=location_score
            )
            
        except Exception as e:
            self.logger.warning(f"Error assessing location quality: {e}")
            return None
    
    def _assess_signal_quality(self, row: pd.Series, cell_data: pd.DataFrame) -> Dict[str, float]:
        """Assess signal quality for positioning"""
        # Get signal strength from best antenna
        if 'channel_level_db' in row and isinstance(row['channel_level_db'], list):
            signal_strengths = row['channel_level_db']
            best_signal = max(signal_strengths) if signal_strengths else -100.0
        else:
            best_signal = -100.0
        
        # Calculate signal stability (variance over recent measurements)
        recent_data = cell_data.tail(10)  # Last 10 measurements
        if len(recent_data) > 1:
            recent_signals = []
            for _, recent_row in recent_data.iterrows():
                if 'channel_level_db' in recent_row and isinstance(recent_row['channel_level_db'], list):
                    recent_signals.append(max(recent_row['channel_level_db']))
            
            if recent_signals:
                signal_variance = np.var(recent_signals)
                signal_stability = max(0, 1 - (signal_variance / 100))  # Normalize to 0-1
            else:
                signal_stability = 0.5
        else:
            signal_stability = 0.5
        
        # Estimate SNR (rough approximation)
        # Assuming noise floor around -174 dBm/Hz + 10*log10(bandwidth)
        noise_floor = -174 + 10 * np.log10(40e6)  # 40 MHz bandwidth
        snr_estimate = best_signal - noise_floor
        
        return {
            'strength': best_signal,
            'stability': signal_stability,
            'snr': snr_estimate
        }
    
    def _assess_timing_quality(self, row: pd.Series, cell_data: pd.DataFrame) -> Dict[str, float]:
        """Assess timing quality for positioning"""
        # Get timing offset
        timing_offset = row.get('current_timing_offset_us', 0)
        
        # Calculate timing accuracy based on signal quality
        signal_strength = row.get('channel_level_db', [-100])[0] if isinstance(row.get('channel_level_db'), list) else -100
        timing_accuracy = max(1.0, abs(timing_offset) * (1 + (signal_strength + 100) / 50))
        
        # Calculate timing stability
        recent_data = cell_data.tail(10)
        if len(recent_data) > 1:
            recent_timings = recent_data['current_timing_offset_us'].dropna()
            if len(recent_timings) > 1:
                timing_variance = np.var(recent_timings)
                timing_stability = max(0, 1 - (timing_variance / 1000))  # Normalize to 0-1
            else:
                timing_stability = 0.5
        else:
            timing_stability = 0.5
        
        return {
            'accuracy': timing_accuracy,
            'stability': timing_stability
        }
    
    def _assess_geometric_quality(self, row: pd.Series, cell_data: pd.DataFrame) -> Dict[str, float]:
        """Assess geometric quality for positioning"""
        # Calculate measurement quality based on signal strength and timing
        signal_strength = row.get('channel_level_db', [-100])[0] if isinstance(row.get('channel_level_db'), list) else -100
        timing_offset = abs(row.get('current_timing_offset_us', 0))
        
        # Signal quality component (0-1)
        signal_quality = max(0, min(1, (signal_strength + 100) / 50))  # -100 to -50 dBm range
        
        # Timing quality component (0-1)
        timing_quality = max(0, min(1, 1 - (timing_offset / 100)))  # 0-100 μs range
        
        # Overall measurement quality
        measurement_quality = (signal_quality + timing_quality) / 2
        
        # Positioning confidence based on measurement quality and stability
        confidence = measurement_quality * 0.8  # Scale down for uncertainty
        
        return {
            'measurement_quality': measurement_quality,
            'confidence': confidence
        }
    
    def _assess_antenna_quality(self, row: pd.Series) -> Dict[str, float]:
        """Assess multi-antenna quality"""
        # Get antenna data
        channel_levels = row.get('channel_level_db', [])
        mrc_weights = row.get('mrc_weights', [])
        
        if not isinstance(channel_levels, list) or len(channel_levels) < 2:
            return {'diversity': 0.0, 'mrc_quality': 0.0}
        
        # Calculate antenna diversity
        signal_strengths = [float(x) for x in channel_levels if x is not None]
        if len(signal_strengths) >= 2:
            # Diversity based on signal strength difference
            max_signal = max(signal_strengths)
            min_signal = min(signal_strengths)
            diversity = min(1.0, (max_signal - min_signal) / 20)  # 0-20 dB range
        else:
            diversity = 0.0
        
        # Calculate MRC quality
        if isinstance(mrc_weights, list) and len(mrc_weights) >= 2:
            # MRC quality based on weight distribution
            weights = [float(w) for w in mrc_weights if w is not None]
            if weights:
                weight_variance = np.var(weights)
                mrc_quality = max(0, 1 - weight_variance)  # Lower variance = better quality
            else:
                mrc_quality = 0.0
        else:
            mrc_quality = 0.0
        
        return {
            'diversity': diversity,
            'mrc_quality': mrc_quality
        }
    
    def _assess_motion_impact(self, row: pd.Series, cell_data: pd.DataFrame) -> float:
        """Assess how motion affects positioning quality"""
        # Calculate motion magnitude from timing changes
        if 'current_timing_offset_us' in row and 'initial_timing_offset_us' in row:
            motion_magnitude = abs(row['current_timing_offset_us'] - row['initial_timing_offset_us'])
        else:
            motion_magnitude = 0
        
        # Motion impact: higher motion = lower positioning quality
        motion_threshold = 20.0  # microseconds
        motion_impact = min(1.0, motion_magnitude / motion_threshold)
        
        return motion_impact
    
    def _calculate_overall_score(self, signal_quality: Dict, timing_quality: Dict, 
                                geometric_quality: Dict, antenna_quality: Dict, 
                                motion_impact: float) -> float:
        """Calculate overall location quality score"""
        # Weighted combination of all quality metrics
        weights = {
            'signal': 0.25,
            'timing': 0.25, 
            'geometric': 0.20,
            'antenna': 0.15,
            'motion': 0.15
        }
        
        # Signal quality component
        signal_score = (signal_quality['stability'] + max(0, signal_quality['snr'] + 20) / 40) / 2
        
        # Timing quality component
        timing_score = timing_quality['stability']
        
        # Geometric quality component
        geometric_score = geometric_quality['measurement_quality']
        
        # Antenna quality component
        antenna_score = (antenna_quality['diversity'] + antenna_quality['mrc_quality']) / 2
        
        # Motion impact component (inverted - lower motion is better)
        motion_score = 1 - motion_impact
        
        # Calculate weighted score
        overall_score = (
            weights['signal'] * signal_score +
            weights['timing'] * timing_score +
            weights['geometric'] * geometric_score +
            weights['antenna'] * antenna_score +
            weights['motion'] * motion_score
        )
        
        return max(0, min(1, overall_score))  # Clamp to 0-1
    
    def get_best_locations(self, quality_assessments: List[UELocationQuality], 
                          min_score: float = 0.7, max_locations: int = 10) -> List[UELocationQuality]:
        """Get the best UE locations for gNB positioning"""
        # Filter by minimum quality score
        good_locations = [loc for loc in quality_assessments if loc.location_score >= min_score]
        
        # Sort by quality score (descending)
        good_locations.sort(key=lambda x: x.location_score, reverse=True)
        
        # Return top locations
        return good_locations[:max_locations]
    
    def analyze_location_distribution(self, quality_assessments: List[UELocationQuality]) -> Dict[str, Any]:
        """Analyze distribution of location quality"""
        if not quality_assessments:
            return {}
        
        scores = [loc.location_score for loc in quality_assessments]
        
        return {
            'total_locations': len(quality_assessments),
            'mean_score': np.mean(scores),
            'std_score': np.std(scores),
            'min_score': np.min(scores),
            'max_score': np.max(scores),
            'excellent_locations': len([s for s in scores if s >= 0.8]),
            'good_locations': len([s for s in scores if 0.6 <= s < 0.8]),
            'fair_locations': len([s for s in scores if 0.4 <= s < 0.6]),
            'poor_locations': len([s for s in scores if s < 0.4])
        } 