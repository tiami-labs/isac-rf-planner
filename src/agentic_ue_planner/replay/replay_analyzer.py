"""
Replay Analyzer for RF Planner
Integrates GPS-telemetry matching for historical analysis and gNB position estimation
"""

import pandas as pd
import numpy as np
import os
import json
import logging
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
import glob
from dataclasses import dataclass

from ..geo.gps_telemetry_matcher import GPSTelemetryMatcher, MatchedMeasurement
from ..placement.ue_location_assessor import UELocationAssessor
from ..ui.visualization import RFVisualizer
from ..config import RFPlannerConfig, DEFAULT_CONFIG

@dataclass
class ReplayAnalysisResult:
    """Result of replay analysis"""
    # Analysis metadata
    analysis_timestamp: datetime
    gps_file: str
    telemetry_files: List[str]
    time_range: Tuple[datetime, datetime]
    
    # Matched measurements
    matched_measurements: List[MatchedMeasurement]
    total_matches: int
    
    # Best positions for gNB estimation
    best_ue_positions: List[MatchedMeasurement]
    quality_distribution: Dict[str, Any]
    
    # gNB estimation results
    estimated_gnb_position: Optional[Tuple[float, float, float]]  # lat, lon, confidence
    estimated_gnb_confidence: float
    triangulation_quality: float
    
    # Analysis statistics
    analysis_stats: Dict[str, Any]

class ReplayAnalyzer:
    """Replay analyzer for historical GPS-telemetry analysis"""
    
    def __init__(self, config: Optional[RFPlannerConfig] = None):
        self.config = config or DEFAULT_CONFIG
        self.logger = logging.getLogger(__name__)
        self.matcher = GPSTelemetryMatcher(time_tolerance_seconds=2.0)
        
    def analyze_replay_data(self, gps_file: str, telemetry_pattern: str = None, 
                           output_dir: str = None) -> ReplayAnalysisResult:
        """Analyze historical GPS and telemetry data"""
        self.logger.info(f"Starting replay analysis for GPS file: {gps_file}")
        
        # Load GPS data
        gps_data = self.matcher.load_gps_data(gps_file)
        if gps_data.empty:
            raise ValueError(f"Could not load GPS data from {gps_file}")
        
        # Find telemetry files
        if telemetry_pattern is None:
            telemetry_pattern = os.path.join(self.config.telemetry_data_path, 
                                           self.config.telemetry_file_pattern)
        
        telemetry_files = glob.glob(telemetry_pattern)
        if not telemetry_files:
            raise ValueError(f"No telemetry files found matching pattern: {telemetry_pattern}")
        
        # Load telemetry data
        telemetry_data = self.matcher.load_telemetry_data(telemetry_files)
        if telemetry_data.empty:
            raise ValueError("Could not load telemetry data")
        
        # Match GPS and telemetry data
        matched_measurements = self.matcher.match_gps_telemetry(gps_data, telemetry_data)
        
        if not matched_measurements:
            raise ValueError("No matched measurements found. Check time ranges and tolerance settings.")
        
        # Get best UE positions
        best_positions = self.matcher.get_best_ue_positions(
            matched_measurements, 
            min_quality_score=0.6, 
            max_positions=20
        )
        
        # Analyze quality distribution
        quality_distribution = self.matcher.analyze_position_distribution(matched_measurements)
        
        # Estimate gNB position using triangulation
        estimated_gnb_position, estimated_gnb_confidence = self._estimate_gnb_position(best_positions)
        
        # Calculate triangulation quality
        triangulation_quality = self._calculate_triangulation_quality(best_positions)
        
        # Generate analysis statistics
        analysis_stats = self._generate_analysis_stats(
            gps_data, telemetry_data, matched_measurements, best_positions
        )
        
        # Create result object
        result = ReplayAnalysisResult(
            analysis_timestamp=datetime.now(),
            gps_file=gps_file,
            telemetry_files=telemetry_files,
            time_range=(gps_data['timestamp'].min(), gps_data['timestamp'].max()),
            matched_measurements=matched_measurements,
            total_matches=len(matched_measurements),
            best_ue_positions=best_positions,
            quality_distribution=quality_distribution,
            estimated_gnb_position=estimated_gnb_position,
            estimated_gnb_confidence=estimated_gnb_confidence,
            triangulation_quality=triangulation_quality,
            analysis_stats=analysis_stats
        )
        
        # Export results if output directory specified
        if output_dir:
            self._export_replay_results(result, output_dir)
        
        return result
    
    def _estimate_gnb_position(self, best_positions: List[MatchedMeasurement]) -> Tuple[Optional[Tuple[float, float, float]], float]:
        """Estimate gNB position using triangulation from best UE positions"""
        if len(best_positions) < 3:
            self.logger.warning("Insufficient positions for gNB triangulation (need at least 3)")
            return None, 0.0
        
        # Use top 10 positions for triangulation
        triangulation_positions = best_positions[:10]
        
        # Extract positions and weights (quality scores)
        positions = []
        weights = []
        
        for pos in triangulation_positions:
            positions.append((pos.gps_lat, pos.gps_lon))
            weights.append(pos.overall_quality_score)
        
        # Weighted centroid calculation
        total_weight = sum(weights)
        if total_weight == 0:
            return None, 0.0
        
        weighted_lat = sum(lat * weight for (lat, lon), weight in zip(positions, weights)) / total_weight
        weighted_lon = sum(lon * weight for (lat, lon), weight in zip(positions, weights)) / total_weight
        
        # Calculate confidence based on position spread and quality
        position_spread = self._calculate_position_spread(positions)
        avg_quality = np.mean(weights)
        
        # Confidence decreases with spread and increases with quality
        confidence = avg_quality * (1 - min(position_spread / 1000, 0.5))  # Normalize spread
        
        return (weighted_lat, weighted_lon, confidence), confidence
    
    def _calculate_position_spread(self, positions: List[Tuple[float, float]]) -> float:
        """Calculate spread of positions (higher = more spread out)"""
        if len(positions) < 2:
            return 0.0
        
        lats = [pos[0] for pos in positions]
        lons = [pos[1] for pos in positions]
        
        lat_spread = max(lats) - min(lats)
        lon_spread = max(lons) - min(lons)
        
        # Convert to approximate meters (1 degree ≈ 111,000 meters)
        spread_m = np.sqrt((lat_spread * 111000)**2 + (lon_spread * 111000)**2)
        
        return spread_m
    
    def _calculate_triangulation_quality(self, best_positions: List[MatchedMeasurement]) -> float:
        """Calculate quality of triangulation based on position distribution"""
        if len(best_positions) < 3:
            return 0.0
        
        # Factors affecting triangulation quality:
        # 1. Number of positions (more = better)
        # 2. Position spread (moderate spread = better)
        # 3. Quality of individual positions
        # 4. Geometric distribution (triangle formation)
        
        positions = [(pos.gps_lat, pos.gps_lon) for pos in best_positions[:10]]
        qualities = [pos.overall_quality_score for pos in best_positions[:10]]
        
        # Number of positions factor
        num_positions_factor = min(len(positions) / 10.0, 1.0)
        
        # Position spread factor (optimal spread around 500-2000m)
        spread = self._calculate_position_spread(positions)
        spread_factor = 1.0 - abs(spread - 1000) / 1000  # Optimal around 1000m
        spread_factor = max(0, min(1, spread_factor))
        
        # Average quality factor
        avg_quality = np.mean(qualities)
        
        # Geometric distribution factor (simplified)
        geo_factor = 1.0  # Could be enhanced with actual geometric analysis
        
        # Combined quality
        triangulation_quality = (
            num_positions_factor * 0.3 +
            spread_factor * 0.3 +
            avg_quality * 0.3 +
            geo_factor * 0.1
        )
        
        return max(0, min(1, triangulation_quality))
    
    def _generate_analysis_stats(self, gps_data: pd.DataFrame, telemetry_data: pd.DataFrame,
                                matched_measurements: List[MatchedMeasurement],
                                best_positions: List[MatchedMeasurement]) -> Dict[str, Any]:
        """Generate comprehensive analysis statistics"""
        
        # GPS statistics
        gps_stats = {
            'total_gps_measurements': len(gps_data),
            'gps_time_range_hours': (gps_data['timestamp'].max() - gps_data['timestamp'].min()).total_seconds() / 3600,
            'avg_gps_accuracy_m': gps_data['horizontal_accuracy'].mean(),
            'max_gps_accuracy_m': gps_data['horizontal_accuracy'].max(),
            'min_gps_accuracy_m': gps_data['horizontal_accuracy'].min(),
            'avg_velocity_mps': np.sqrt(
                gps_data['velocity_x']**2 + gps_data['velocity_y']**2 + gps_data['velocity_z']**2
            ).mean()
        }
        
        # Telemetry statistics
        telemetry_stats = {
            'total_telemetry_measurements': len(telemetry_data),
            'telemetry_time_range_hours': (telemetry_data['timestamp'].max() - telemetry_data['timestamp'].min()).total_seconds() / 3600,
            'unique_pcis': telemetry_data['pci'].nunique(),
            'avg_signal_strength_db': np.mean([
                max(signals) if isinstance(signals, list) else signals 
                for signals in telemetry_data['channel_level_db']
            ]),
            'avg_timing_offset_us': telemetry_data['current_timing_offset_us'].abs().mean()
        }
        
        # Matching statistics
        matching_stats = {
            'total_matches': len(matched_measurements),
            'match_rate_percent': (len(matched_measurements) / len(gps_data)) * 100,
            'avg_quality_score': np.mean([pos.overall_quality_score for pos in matched_measurements]),
            'excellent_positions': len([pos for pos in matched_measurements if pos.overall_quality_score >= 0.8]),
            'good_positions': len([pos for pos in matched_measurements if 0.6 <= pos.overall_quality_score < 0.8])
        }
        
        # Best positions statistics
        if best_positions:
            best_stats = {
                'num_best_positions': len(best_positions),
                'avg_best_quality': np.mean([pos.overall_quality_score for pos in best_positions]),
                'best_position_spread_m': self._calculate_position_spread([
                    (pos.gps_lat, pos.gps_lon) for pos in best_positions
                ]),
                'avg_best_signal_strength': np.mean([
                    max(pos.signal_strength_db) for pos in best_positions
                ]),
                'avg_best_timing_offset': np.mean([
                    abs(pos.timing_offset_us) for pos in best_positions
                ])
            }
        else:
            best_stats = {}
        
        return {
            'gps_stats': gps_stats,
            'telemetry_stats': telemetry_stats,
            'matching_stats': matching_stats,
            'best_positions_stats': best_stats
        }
    
    def _export_replay_results(self, result: ReplayAnalysisResult, output_dir: str):
        """Export replay analysis results"""
        os.makedirs(output_dir, exist_ok=True)
        
        # Export matched measurements
        self.matcher.export_results(result.matched_measurements, output_dir)
        
        # Export best positions
        best_positions_file = os.path.join(output_dir, "best_ue_positions.csv")
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
            for i, pos in enumerate(result.best_ue_positions, 1)
        ])
        best_df.to_csv(best_positions_file, index=False)
        
        # Export gNB estimation results
        if result.estimated_gnb_position:
            gnb_lat, gnb_lon, gnb_confidence = result.estimated_gnb_position
            gnb_results = {
                'estimated_gnb_lat': gnb_lat,
                'estimated_gnb_lon': gnb_lon,
                'estimated_gnb_confidence': gnb_confidence,
                'triangulation_quality': result.triangulation_quality,
                'num_positions_used': len(result.best_ue_positions),
                'analysis_timestamp': result.analysis_timestamp.isoformat()
            }
            
            gnb_file = os.path.join(output_dir, "gnb_estimation_results.json")
            with open(gnb_file, 'w') as f:
                json.dump(gnb_results, f, indent=2)
        
        # Export analysis summary
        summary = {
            'analysis_timestamp': result.analysis_timestamp.isoformat(),
            'gps_file': result.gps_file,
            'telemetry_files': result.telemetry_files,
            'time_range': [result.time_range[0].isoformat(), result.time_range[1].isoformat()],
            'total_matches': result.total_matches,
            'quality_distribution': result.quality_distribution,
            'analysis_stats': result.analysis_stats
        }
        
        summary_file = os.path.join(output_dir, "replay_analysis_summary.json")
        with open(summary_file, 'w') as f:
            json.dump(summary, f, indent=2, default=str)
        
        # Create visualizations
        self._create_visualizations(result, output_dir)
        
        self.logger.info(f"Replay analysis results exported to {output_dir}")
    
    def _create_visualizations(self, result: ReplayAnalysisResult, output_dir: str):
        """Create visualizations for replay analysis results"""
        try:
            # Convert matched measurements to DataFrame for visualization
            matched_df = pd.DataFrame([
                {
                    'gps_timestamp': m.gps_timestamp,
                    'gps_lat': m.gps_lat,
                    'gps_lon': m.gps_lon,
                    'gps_altitude': m.gps_altitude,
                    'gps_velocity_x': m.gps_velocity_x,
                    'gps_velocity_y': m.gps_velocity_y,
                    'gps_velocity_z': m.gps_velocity_z,
                    'gps_horizontal_accuracy': m.gps_horizontal_accuracy,
                    'gps_vertical_accuracy': m.gps_vertical_accuracy,
                    'telemetry_timestamp': m.telemetry_timestamp,
                    'pci': m.pci,
                    'signal_strength_db': m.signal_strength_db,
                    'timing_offset_us': m.timing_offset_us,
                    'freq_offset_hz': m.freq_offset_hz,
                    'llr_energy': m.llr_energy,
                    'position_quality_score': m.position_quality_score,
                    'signal_quality_score': m.signal_quality_score,
                    'timing_quality_score': m.timing_quality_score,
                    'motion_quality_score': m.motion_quality_score,
                    'overall_quality_score': m.overall_quality_score,
                    'estimated_distance_m': m.estimated_distance_m,
                    'estimated_gps_accuracy_m': m.estimated_gps_accuracy_m
                }
                for m in result.matched_measurements
            ])
            
            # Create gNB estimation dictionary
            gnb_estimation = {
                'estimated_gnb_lat': result.estimated_gnb_position[0] if result.estimated_gnb_position else 0,
                'estimated_gnb_lon': result.estimated_gnb_position[1] if result.estimated_gnb_position else 0,
                'estimated_gnb_confidence': result.estimated_gnb_confidence,
                'triangulation_quality': result.triangulation_quality,
                'num_positions_used': len(result.best_ue_positions)
            }
            
            # Create visualizer and generate visualizations
            visualizer = RFVisualizer(self.config)
            visualizer.create_comprehensive_visualization(matched_df, gnb_estimation, output_dir)
            
            self.logger.info(f"Visualizations created in {output_dir}")
            
        except Exception as e:
            self.logger.error(f"Error creating visualizations: {e}")
    
    def get_replay_recommendations(self, result: ReplayAnalysisResult) -> List[str]:
        """Get recommendations based on replay analysis"""
        recommendations = []
        
        # Quality-based recommendations
        if result.quality_distribution.get('excellent_positions', 0) < 5:
            recommendations.append("Consider collecting more high-quality measurements for better gNB estimation")
        
        if result.quality_distribution.get('poor_positions', 0) > len(result.matched_measurements) * 0.3:
            recommendations.append("High number of poor quality positions detected - check GPS accuracy and signal conditions")
        
        # Triangulation recommendations
        if result.triangulation_quality < 0.6:
            recommendations.append("Low triangulation quality - consider collecting measurements from more diverse locations")
        
        if result.estimated_gnb_confidence < 0.7:
            recommendations.append("Low gNB position confidence - use more high-quality UE positions for estimation")
        
        # Coverage recommendations
        if result.analysis_stats.get('matching_stats', {}).get('match_rate_percent', 0) < 50:
            recommendations.append("Low GPS-telemetry match rate - check time synchronization between devices")
        
        # Signal quality recommendations
        avg_signal = result.analysis_stats.get('telemetry_stats', {}).get('avg_signal_strength_db', -100)
        if avg_signal < -80:
            recommendations.append("Weak signal strength detected - consider moving UE closer to gNB or improving antenna")
        
        return recommendations 