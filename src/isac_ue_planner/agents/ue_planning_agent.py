"""
RF Planner Core Module
Main orchestrator for RF planning and UE placement system
"""

import pandas as pd
import numpy as np
import os
import json
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta

from ..config import RFPlannerConfig, DEFAULT_CONFIG
from ..data.telemetry_processor import TelemetryProcessor
from ..isac.isac_analyzer import ISACAnalyzer
from ..placement.placement_engine import PlacementEngine
from ..ui.visualization import RFVisualizer
from ..replay.replay_analyzer import ReplayAnalyzer, ReplayAnalysisResult
from ..geo.gps_telemetry_matcher import GPSTelemetryMatcher
from ..isac.velocity_analyzer import VelocityAnalyzer

class RFPlanner:
    """Main RF Planner orchestrator"""
    
    def __init__(self, config: Optional[RFPlannerConfig] = None):
        self.config = config or DEFAULT_CONFIG
        self.setup_logging()
        
        # Initialize components
        self.data_processor = TelemetryProcessor(self.config)
        self.isac_analyzer = ISACAnalyzer(self.config)
        self.placement_engine = PlacementEngine(self.config)
        self.visualizer = RFVisualizer(self.config)
        self.replay_analyzer = ReplayAnalyzer(self.config)
        self.gps_matcher = GPSTelemetryMatcher()
        self.velocity_analyzer = VelocityAnalyzer()
        
        # Data storage
        self.telemetry_data = None
        self.isac_results = None
        self.planning_results = None
        self.replay_results = None
        self.matched_measurements = None
        self.velocity_analysis = None
        
        self.logger = logging.getLogger(__name__)
        self.logger.info("RF Planner initialized")
    
    def setup_logging(self):
        """Setup logging configuration"""
        logging.basicConfig(
            level=getattr(logging, self.config.log_level),
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler(self.config.log_file),
                logging.StreamHandler()
            ]
        )
    
    def load_data(self) -> bool:
        """Load telemetry data"""
        try:
            self.telemetry_data = self.data_processor.load_telemetry_data()
            if self.telemetry_data.empty:
                self.logger.warning("No telemetry data loaded")
                return False
            
            self.logger.info(f"Loaded {len(self.telemetry_data)} telemetry records")
            return True
            
        except Exception as e:
            self.logger.error(f"Error loading data: {e}")
            return False
    
    def load_gps_data(self, gps_file: str) -> pd.DataFrame:
        """Load GPS trajectory data"""
        try:
            gps_data = self.gps_matcher.load_gps_data(gps_file)
            if gps_data.empty:
                self.logger.warning("No GPS data loaded")
                return pd.DataFrame()
            
            self.logger.info(f"Loaded {len(gps_data)} GPS measurements")
            return gps_data
            
        except Exception as e:
            self.logger.error(f"Error loading GPS data: {e}")
            return pd.DataFrame()
    
    def match_gps_telemetry(self, gps_file: str, telemetry_files: List[str] = None) -> List:
        """Match GPS data with telemetry data"""
        try:
            # Load GPS data
            gps_data = self.load_gps_data(gps_file)
            if gps_data.empty:
                self.logger.error("No GPS data available for matching")
                return []
            
            # Load telemetry data
            if telemetry_files is None:
                # Use default telemetry data if available
                if self.telemetry_data is not None and not self.telemetry_data.empty:
                    telemetry_data = self.telemetry_data
                else:
                    self.logger.error("No telemetry data available for matching")
                    return []
            else:
                telemetry_data = self.gps_matcher.load_telemetry_data(telemetry_files)
                if telemetry_data.empty:
                    self.logger.error("No telemetry data loaded from files")
                    return []
            
            # Match GPS and telemetry data
            self.matched_measurements = self.gps_matcher.match_gps_telemetry(gps_data, telemetry_data)
            
            self.logger.info(f"Matched {len(self.matched_measurements)} GPS-telemetry pairs")
            return self.matched_measurements
            
        except Exception as e:
            self.logger.error(f"Error matching GPS and telemetry data: {e}")
            return []
    
    def analyze_velocity_accuracy(self) -> Dict[str, Any]:
        """Analyze velocity estimation accuracy"""
        if not self.matched_measurements:
            self.logger.warning("No matched measurements available for velocity analysis")
            return {}
        
        try:
            self.velocity_analysis = self.velocity_analyzer.analyze_velocity_accuracy(self.matched_measurements)
            
            # Log key statistics
            stats = self.velocity_analysis.get('statistics', {})
            if stats:
                self.logger.info(f"Velocity Analysis Results:")
                self.logger.info(f"  Total measurements: {stats.get('total_measurements', 0)}")
                self.logger.info(f"  Mean error: {stats.get('mean_error_mps', 0):.2f} m/s")
                self.logger.info(f"  RMSE: {stats.get('rmse_mps', 0):.2f} m/s")
                self.logger.info(f"  Correlation: {stats.get('correlation_coefficient', 0):.3f}")
            
            return self.velocity_analysis
            
        except Exception as e:
            self.logger.error(f"Error analyzing velocity accuracy: {e}")
            return {}
    
    def generate_velocity_visualizations(self, output_dir: str = None) -> Dict[str, str]:
        """Generate velocity analysis visualizations"""
        if not self.velocity_analysis:
            self.logger.warning("No velocity analysis available for visualization")
            return {}
        
        try:
            if output_dir is None:
                output_dir = self.config.output_dir
            
            plot_files = self.velocity_analyzer.generate_velocity_plots(output_dir)
            
            self.logger.info(f"Generated {len(plot_files)} velocity analysis plots")
            return plot_files
            
        except Exception as e:
            self.logger.error(f"Error generating velocity visualizations: {e}")
            return {}
    
    def export_velocity_analysis(self, output_dir: str = None) -> str:
        """Export velocity analysis results"""
        if not self.velocity_analysis:
            self.logger.warning("No velocity analysis available for export")
            return ""
        
        try:
            if output_dir is None:
                output_dir = self.config.output_dir
            
            csv_file = self.velocity_analyzer.export_velocity_analysis(output_dir)
            
            self.logger.info(f"Exported velocity analysis to {csv_file}")
            return csv_file
            
        except Exception as e:
            self.logger.error(f"Error exporting velocity analysis: {e}")
            return ""
    
    def analyze_isac_data(self) -> Dict[str, Any]:
        """Analyze ISAC data for motion detection and positioning"""
        if self.telemetry_data is None or self.telemetry_data.empty:
            raise ValueError("No telemetry data available for ISAC analysis")
        
        self.logger.info("Starting ISAC analysis")
        
        # Perform ISAC analysis
        motion_events = self.isac_analyzer.analyze_motion(self.telemetry_data)
        position_estimates = self.isac_analyzer.estimate_position(self.telemetry_data)
        trajectory_analysis = self.isac_analyzer.analyze_trajectory(self.telemetry_data)
        motion_summary = self.isac_analyzer.get_motion_summary()
        
        self.isac_results = {
            'motion_events': motion_events,
            'position_estimates': position_estimates,
            'trajectory_analysis': trajectory_analysis,
            'motion_summary': motion_summary
        }
        
        self.logger.info(f"ISAC analysis complete: {len(motion_events)} motion events detected")
        return self.isac_results
    
    def perform_rf_planning(self) -> Dict[str, Any]:
        """Perform RF planning analysis"""
        if self.telemetry_data is None or self.telemetry_data.empty:
            raise ValueError("No telemetry data available for RF planning")
        
        self.logger.info("Starting RF planning analysis")
        
        # Analyze coverage
        coverage_areas = self.placement_engine.analyze_coverage(self.telemetry_data)
        
        # Get placement recommendations
        placement_recommendations = self.placement_engine.recommend_placement(
            self.telemetry_data, motion_state='stationary'
        )
        
        # Optimize network planning
        optimization_results = self.placement_engine.optimize_network_planning(self.telemetry_data)
        
        self.planning_results = {
            'coverage_areas': coverage_areas,
            'placement_recommendations': placement_recommendations,
            'optimization_results': optimization_results
        }
        
        self.logger.info(f"RF planning complete: {len(coverage_areas)} coverage areas analyzed")
        return self.planning_results
    
    def run_replay_analysis(self, gps_file: str, output_dir: str = None) -> ReplayAnalysisResult:
        """Run replay analysis with GPS trajectory data"""
        self.logger.info(f"Starting replay analysis for GPS file: {gps_file}")
        
        # Set output directory if not specified
        if output_dir is None:
            output_dir = os.path.join(self.config.output_path, "replay_analysis")
        
        # Run replay analysis
        result = self.replay_analyzer.analyze_replay_data(
            gps_file=gps_file,
            output_dir=output_dir
        )
        
        self.replay_results = result
        
        # Log results
        self.logger.info(f"Replay analysis complete:")
        self.logger.info(f"  - Total matches: {result.total_matches}")
        self.logger.info(f"  - Best positions: {len(result.best_ue_positions)}")
        if result.estimated_gnb_position:
            gnb_lat, gnb_lon, confidence = result.estimated_gnb_position
            self.logger.info(f"  - Estimated gNB: ({gnb_lat:.6f}, {gnb_lon:.6f}) with confidence {confidence:.3f}")
        
        return result
    
    def generate_visualizations(self, output_dir: str = None) -> Dict[str, str]:
        """Generate visualizations for all analysis results"""
        if output_dir is None:
            output_dir = self.config.output_path
        
        os.makedirs(output_dir, exist_ok=True)
        visualization_files = {}
        
        try:
            # Generate telemetry overview
            if self.telemetry_data is not None:
                fig = self.visualizer.plot_telemetry_overview(self.telemetry_data)
                overview_file = os.path.join(output_dir, "telemetry_overview.png")
                fig.savefig(overview_file, dpi=300, bbox_inches='tight')
                visualization_files['telemetry_overview'] = overview_file
            
            # Generate coverage map
            if self.telemetry_data is not None:
                coverage_areas = self.planning_results.get('coverage_areas', []) if self.planning_results else []
                map_obj = self.visualizer.plot_coverage_map(self.telemetry_data, coverage_areas)
                map_file = os.path.join(output_dir, "coverage_map.html")
                map_obj.save(map_file)
                visualization_files['coverage_map'] = map_file
            
            # Generate motion analysis
            if self.isac_results and self.telemetry_data is not None:
                motion_data = self.data_processor.get_motion_data()
                fig = self.visualizer.plot_motion_analysis(motion_data)
                motion_file = os.path.join(output_dir, "motion_analysis.png")
                fig.savefig(motion_file, dpi=300, bbox_inches='tight')
                visualization_files['motion_analysis'] = motion_file
            
            # Generate signal quality trends
            if self.telemetry_data is not None:
                quality_data = self.data_processor.get_signal_quality_data()
                fig = self.visualizer.plot_signal_quality_trends(quality_data)
                quality_file = os.path.join(output_dir, "signal_quality_trends.png")
                fig.savefig(quality_file, dpi=300, bbox_inches='tight')
                visualization_files['signal_quality_trends'] = quality_file
            
            # Generate interactive dashboard
            if self.telemetry_data is not None:
                motion_events = self.isac_results.get('motion_events', []) if self.isac_results else []
                position_estimates = self.isac_results.get('position_estimates', []) if self.isac_results else []
                dashboard = self.visualizer.create_interactive_dashboard(
                    self.telemetry_data, motion_events, position_estimates
                )
                dashboard_file = os.path.join(output_dir, "interactive_dashboard.html")
                dashboard.write_html(dashboard_file)
                visualization_files['interactive_dashboard'] = dashboard_file
            
            # Generate network optimization plots
            if self.planning_results:
                optimization_results = self.planning_results.get('optimization_results', {})
                fig = self.visualizer.plot_network_optimization(optimization_results)
                optimization_file = os.path.join(output_dir, "network_optimization.png")
                fig.savefig(optimization_file, dpi=300, bbox_inches='tight')
                visualization_files['network_optimization'] = optimization_file
            
            self.logger.info(f"Generated {len(visualization_files)} visualizations")
            
        except Exception as e:
            self.logger.error(f"Error generating visualizations: {e}")
        
        return visualization_files
    
    def run_complete_analysis(self, output_dir: str = None) -> Dict[str, Any]:
        """Run complete RF planning analysis"""
        self.logger.info("Starting complete RF planning analysis")
        
        # Load data
        if not self.load_data():
            raise ValueError("Failed to load telemetry data")
        
        # Perform ISAC analysis
        isac_results = self.analyze_isac_data()
        
        # Perform RF planning
        planning_results = self.perform_rf_planning()
        
        # Generate visualizations
        visualization_files = self.generate_visualizations(output_dir)
        
        # Export results
        export_files = self.export_results(output_dir)
        
        # Get recommendations
        recommendations = self.get_recommendations()
        
        results = {
            'isac_results': isac_results,
            'planning_results': planning_results,
            'visualization_files': visualization_files,
            'export_files': export_files,
            'recommendations': recommendations
        }
        
        self.logger.info("Complete analysis finished successfully")
        return results
    
    def run_real_time_monitor(self, update_interval_seconds: int = 60) -> None:
        """Run real-time monitoring"""
        self.logger.info(f"Starting real-time monitoring with {update_interval_seconds}s intervals")
        
        try:
            while True:
                # Load latest data
                if self.load_data():
                    # Perform analysis
                    self.analyze_isac_data()
                    self.perform_rf_planning()
                    
                    # Generate real-time dashboard
                    if self.telemetry_data is not None:
                        dashboard = self.visualizer.create_real_time_monitor(
                            self.telemetry_data, 
                            update_interval_ms=self.config.update_interval_ms
                        )
                        
                        # Save dashboard
                        dashboard_file = os.path.join(self.config.output_path, "real_time_dashboard.html")
                        dashboard.write_html(dashboard_file)
                        
                        self.logger.info(f"Real-time dashboard updated: {dashboard_file}")
                
                # Wait for next update
                import time
                time.sleep(update_interval_seconds)
                
        except KeyboardInterrupt:
            self.logger.info("Real-time monitoring stopped by user")
    
    def export_results(self, output_dir: str = None) -> Dict[str, str]:
        """Export analysis results to files"""
        if output_dir is None:
            output_dir = self.config.output_path
        
        os.makedirs(output_dir, exist_ok=True)
        export_files = {}
        
        try:
            # Export telemetry data
            if self.telemetry_data is not None:
                telemetry_file = os.path.join(output_dir, "telemetry_data.csv")
                self.data_processor.export_to_csv(telemetry_file, self.telemetry_data)
                export_files['telemetry_data'] = telemetry_file
            
            # Export motion data
            if self.telemetry_data is not None:
                motion_data = self.data_processor.get_motion_data()
                motion_file = os.path.join(output_dir, "motion_data.csv")
                self.data_processor.export_to_csv(motion_file, motion_data)
                export_files['motion_data'] = motion_file
            
            # Export signal quality data
            if self.telemetry_data is not None:
                quality_data = self.data_processor.get_signal_quality_data()
                quality_file = os.path.join(output_dir, "signal_quality_data.csv")
                self.data_processor.export_to_csv(quality_file, quality_data)
                export_files['signal_quality_data'] = quality_file
            
            # Export statistics
            if self.telemetry_data is not None:
                stats = self.data_processor.get_statistics()
                stats_file = os.path.join(output_dir, "analysis_statistics.json")
                with open(stats_file, 'w') as f:
                    json.dump(stats, f, indent=2, default=str)
                export_files['statistics'] = stats_file
            
            self.logger.info(f"Exported {len(export_files)} result files")
            
        except Exception as e:
            self.logger.error(f"Error exporting results: {e}")
        
        return export_files
    
    def get_recommendations(self) -> List[Dict[str, Any]]:
        """Get actionable recommendations based on analysis"""
        recommendations = []
        
        if self.telemetry_data is None:
            return recommendations
        
        try:
            # Signal quality recommendations
            quality_data = self.data_processor.get_signal_quality_data()
            if not quality_data.empty:
                avg_snr = quality_data['estimated_snr_db'].mean()
                if avg_snr < 10:
                    recommendations.append({
                        'type': 'signal_quality',
                        'priority': 'high',
                        'message': f'Low average SNR ({avg_snr:.1f} dB) - consider improving antenna or moving closer to gNB',
                        'action': 'Check antenna positioning and signal path'
                    })
            
            # Motion recommendations
            if self.isac_results:
                motion_events = self.isac_results.get('motion_events', [])
                if len(motion_events) > 10:
                    recommendations.append({
                        'type': 'motion_detection',
                        'priority': 'medium',
                        'message': f'High motion activity detected ({len(motion_events)} events) - consider stationary measurements',
                        'action': 'Use stationary UE positions for better gNB estimation'
                    })
            
            # Coverage recommendations
            if self.planning_results:
                optimization_results = self.planning_results.get('optimization_results', {})
                coverage_gaps = optimization_results.get('coverage_gaps', [])
                if coverage_gaps:
                    recommendations.append({
                        'type': 'coverage',
                        'priority': 'medium',
                        'message': f'Coverage gaps detected ({len(coverage_gaps)} areas) - consider additional measurements',
                        'action': 'Collect measurements from identified gap areas'
                    })
            
            # Replay analysis recommendations
            if self.replay_results:
                replay_recommendations = self.replay_analyzer.get_replay_recommendations(self.replay_results)
                for rec in replay_recommendations:
                    recommendations.append({
                        'type': 'replay_analysis',
                        'priority': 'medium',
                        'message': rec,
                        'action': 'Review replay analysis results and adjust measurement strategy'
                    })
            
            # Velocity analysis recommendations
            if self.velocity_analysis:
                velocity_stats = self.velocity_analysis.get('statistics', {})
                if velocity_stats.get('total_measurements', 0) > 0:
                    mean_error = velocity_stats.get('mean_error_mps', 0)
                    if abs(mean_error) > 0.5:
                        recommendations.append({
                            'type': 'velocity_accuracy',
                            'priority': 'medium',
                            'message': f'Velocity estimation accuracy is low ({mean_error:.2f} m/s) - review measurement setup and calibration',
                            'action': 'Re-evaluate measurement equipment and calibration'
                        })
            
        except Exception as e:
            self.logger.error(f"Error generating recommendations: {e}")
        
        return recommendations
    
    def save_configuration(self, config_path: str = None):
        """Save current configuration"""
        if config_path is None:
            config_path = os.path.join(self.config.output_path, "rfplanner_config.json")
        
        self.config.save_to_file(config_path)
        self.logger.info(f"Configuration saved to {config_path}")
    
    def load_configuration(self, config_path: str):
        """Load configuration from file"""
        self.config = RFPlannerConfig.from_file(config_path)
        self.logger.info(f"Configuration loaded from {config_path}") 
    
    def run_motion_analysis(self, gps_file: str, output_dir: str = None, 
                           integration_time_ms: float = None) -> Dict[str, Any]:
        """Run motion analysis with GPS trajectory and integration time support"""
        self.logger.info(f"Starting motion analysis with GPS file: {gps_file}")
        
        if output_dir is None:
            output_dir = self.config.output_path
        
        os.makedirs(output_dir, exist_ok=True)
        
        try:
            # Load telemetry data
            if not self.load_data():
                raise ValueError("No telemetry data available for motion analysis")
            
            # Load GPS data for position matching
            gps_data = self._load_gps_data(gps_file)
            if gps_data.empty:
                raise ValueError("No GPS data available for motion analysis")
            
            # PHASE 1: Replace placeholder GPS coordinates with actual GPS position
            self.logger.info("Phase 1: Replacing placeholder GPS coordinates with actual GPS position")
            
            # Match telemetry timestamps with GPS timestamps
            matched_telemetry = self._match_telemetry_with_gps(gps_data)
            
            if matched_telemetry.empty:
                raise ValueError("No telemetry data could be matched with GPS data")
            
            self.logger.info(f"Successfully matched {len(matched_telemetry)} telemetry records with GPS position")
            
            # Validate time synchronization between telemetry and GPS data
            self._validate_time_synchronization(gps_data)
            
            # PHASE 3: Integration time support for velocity estimation
            self.logger.info("Phase 3: Implementing integration time support for velocity estimation")
            
            # Analyze motion from timing offsets with integration time support
            motion_analysis = self._analyze_motion_from_timing(gps_data, integration_time_ms)
            
            # Validate motion detection results against GPS data
            self._validate_motion_detection(motion_analysis, gps_data)
            
            # Generate HTML visualization
            html_file = self._generate_motion_visualization(motion_analysis, gps_data, output_dir)
            
            # Calculate time range
            if not self.telemetry_data.empty:
                time_range = [
                    self.telemetry_data['timestamp_ms'].min(),
                    self.telemetry_data['timestamp_ms'].max()
                ]
            else:
                time_range = ['', '']
            
            result = {
                'total_motion_events': motion_analysis['total_motion_events'],
                'average_radial_velocity': motion_analysis['average_radial_velocity'],
                'max_radial_velocity': motion_analysis['max_radial_velocity'],
                'time_range': time_range,
                'motion_direction_data': motion_analysis['motion_direction_data'],
                'velocity_comparison': motion_analysis['velocity_comparison'],
                'position_quality_data': motion_analysis['position_quality_data'],
                'radial_velocity_distribution': motion_analysis['radial_velocity_distribution'],
                'html_visualization': html_file,
                'matched_telemetry_count': len(matched_telemetry),
                'gps_position_integration': 'SUCCESS',
                'velocity_estimation_formulas': 'SUCCESS',
                'integration_time_support': 'SUCCESS',
                'integration_time_ms': motion_analysis['integration_time_ms'],
                'cfo_velocity_estimates': len([e for e in motion_analysis['motion_direction_data'] if abs(e.get('velocity_cfo', 0)) > 0.1]),
                'timing_velocity_estimates': len([e for e in motion_analysis['motion_direction_data'] if abs(e.get('velocity_timing', 0)) > 0.1]),
                'combined_velocity_estimates': len([e for e in motion_analysis['motion_direction_data'] if abs(e.get('combined_velocity', 0)) > 0.1]),
                'integration_samples_avg': np.mean([e.get('integration_samples', 1) for e in motion_analysis['motion_direction_data']]) if motion_analysis['motion_direction_data'] else 1
            }
            
            self.logger.info(f"Motion analysis completed: {motion_analysis['total_motion_events']} motion events detected")
            self.logger.info(f"Phase 1 COMPLETE: GPS position integration successful")
            self.logger.info(f"Phase 2 COMPLETE: Velocity estimation formulas implemented")
            self.logger.info(f"Phase 3 COMPLETE: Integration time support implemented")
            self.logger.info(f"  - Integration time: {result['integration_time_ms']:.0f}ms")
            self.logger.info(f"  - Average integration samples: {result['integration_samples_avg']:.1f}")
            self.logger.info(f"  - CFO velocity estimates: {result['cfo_velocity_estimates']}")
            self.logger.info(f"  - Timing velocity estimates: {result['timing_velocity_estimates']}")
            self.logger.info(f"  - Combined velocity estimates: {result['combined_velocity_estimates']}")
            return result
            
        except Exception as e:
            self.logger.error(f"Error during motion analysis: {e}")
            raise
    
    def _match_telemetry_with_gps(self, gps_data: pd.DataFrame) -> pd.DataFrame:
        """Match telemetry data with GPS position data based on timestamps"""
        self.logger.info("Matching telemetry data with GPS position data")
        
        # Create a copy of telemetry data to avoid modifying original
        matched_telemetry = self.telemetry_data.copy()
        
        # Convert telemetry timestamps to datetime for matching
        matched_telemetry['timestamp_dt'] = pd.to_datetime(matched_telemetry['timestamp_ms'], unit='ms', utc=True)
        gps_data['timestamp_dt'] = pd.to_datetime(gps_data['timestamp_ms'], unit='ms', utc=True)
        
        # Initialize GPS position columns
        matched_telemetry['gps_latitude'] = 0.0
        matched_telemetry['gps_longitude'] = 0.0
        matched_telemetry['gps_altitude'] = 0.0
        matched_telemetry['gps_velocity_x'] = 0.0
        matched_telemetry['gps_velocity_y'] = 0.0
        matched_telemetry['gps_velocity_z'] = 0.0
        matched_telemetry['gps_velocity_magnitude'] = 0.0
        matched_telemetry['gps_horizontal_accuracy'] = 999.0
        matched_telemetry['gps_vertical_accuracy'] = 999.0
        matched_telemetry['gps_match_quality'] = 0.0
        
        # Match each telemetry record with closest GPS record
        time_tolerance = pd.Timedelta(seconds=2.0)  # 2 second tolerance
        matched_count = 0
        
        for idx, telemetry_row in matched_telemetry.iterrows():
            telemetry_time = telemetry_row['timestamp_dt']
            
            # Find GPS records within time tolerance
            time_diff = abs(gps_data['timestamp_dt'] - telemetry_time)
            matching_gps = gps_data[time_diff <= time_tolerance]
            
            if not matching_gps.empty:
                # Use the closest GPS record
                closest_idx = time_diff[time_diff <= time_tolerance].idxmin()
                gps_row = gps_data.loc[closest_idx]
                
                # Calculate match quality (closer time = better quality)
                time_diff_seconds = abs((gps_row['timestamp_dt'] - telemetry_time).total_seconds())
                match_quality = max(0.0, 1.0 - (time_diff_seconds / 2.0))  # 0-1 scale
                
                # Update telemetry with GPS position data
                matched_telemetry.loc[idx, 'gps_latitude'] = gps_row['latitude']
                matched_telemetry.loc[idx, 'gps_longitude'] = gps_row['longitude']
                matched_telemetry.loc[idx, 'gps_altitude'] = gps_row['altitude']
                matched_telemetry.loc[idx, 'gps_velocity_x'] = gps_row['velocity.x']
                matched_telemetry.loc[idx, 'gps_velocity_y'] = gps_row['velocity.y']
                matched_telemetry.loc[idx, 'gps_velocity_z'] = gps_row['velocity.z']
                matched_telemetry.loc[idx, 'gps_velocity_magnitude'] = gps_row['velocity_magnitude']
                matched_telemetry.loc[idx, 'gps_horizontal_accuracy'] = gps_row['horizontal_accuracy']
                matched_telemetry.loc[idx, 'gps_vertical_accuracy'] = gps_row['vertical_accuracy']
                matched_telemetry.loc[idx, 'gps_match_quality'] = match_quality
                
                # Replace placeholder coordinates with actual GPS position
                matched_telemetry.loc[idx, 'latitude'] = gps_row['latitude']
                matched_telemetry.loc[idx, 'longitude'] = gps_row['longitude']
                matched_telemetry.loc[idx, 'altitude'] = gps_row['altitude']
                
                matched_count += 1
        
        self.logger.info(f"GPS position matching complete: {matched_count}/{len(matched_telemetry)} records matched")
        
        # Filter to only include matched records
        matched_telemetry = matched_telemetry[matched_telemetry['gps_match_quality'] > 0]
        
        # Update the telemetry data with matched GPS positions
        self.telemetry_data = matched_telemetry.copy()
        
        return matched_telemetry
    
    def _validate_time_synchronization(self, gps_data: pd.DataFrame):
        """Validate that telemetry and GPS data are from the same time period"""
        if gps_data.empty:
            raise ValueError("No GPS data available for time synchronization validation")
        
        # First, try direct matching without any conversion
        telemetry_min = self.telemetry_data['timestamp_ms'].min()
        telemetry_max = self.telemetry_data['timestamp_ms'].max()
        gps_min = gps_data['timestamp_ms'].min()
        gps_max = gps_data['timestamp_ms'].max()
        
        tolerance_ms = 5 * 60 * 1000  # 5 minute tolerance
        
        # Check if telemetry falls within GPS time range without conversion
        direct_match = (
            (telemetry_min >= gps_min - tolerance_ms) and 
            (telemetry_max <= gps_max + tolerance_ms)
        )
        
        if direct_match:
            self.logger.info("Direct time match found - no conversion needed")
            self.telemetry_data['timestamp_utc_ms'] = self.telemetry_data['timestamp_ms']
            return
        
        # If direct match fails, try using system local time to convert telemetry
        self.logger.info("Direct match failed, attempting timezone conversion")
        
        import time
        import datetime
        
        # Get system's local timezone offset
        local_timezone_offset = -time.timezone  # Seconds offset from UTC
        timezone_offset_ms = local_timezone_offset * 1000
        
        self.logger.info(f"System local timezone offset: {local_timezone_offset/3600:.1f} hours from UTC")
        
        # Apply timezone conversion to telemetry data
        telemetry_utc_min = telemetry_min + timezone_offset_ms
        telemetry_utc_max = telemetry_max + timezone_offset_ms
        
        # Check if converted telemetry falls within GPS time range
        converted_match = (
            (telemetry_utc_min >= gps_min - tolerance_ms) and 
            (telemetry_utc_max <= gps_max + tolerance_ms)
        )
        
        if not converted_match:
            from datetime import datetime
            telemetry_start = datetime.fromtimestamp(telemetry_utc_min / 1000)
            telemetry_end = datetime.fromtimestamp(telemetry_utc_max / 1000)
            gps_start = datetime.fromtimestamp(gps_min / 1000)
            gps_end = datetime.fromtimestamp(gps_max / 1000)
            
            error_msg = (
                f"Telemetry and GPS data are from different time periods!\n"
                f"Telemetry (UTC): {telemetry_start} to {telemetry_end}\n"
                f"GPS (UTC): {gps_start} to {gps_end}\n"
                f"Telemetry time range does not fall within GPS time range.\n"
                f"Cannot perform motion analysis on mismatched time periods."
            )
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        
        self.logger.info("Timezone conversion successful - telemetry falls within GPS time range")
        
        # Store the UTC-converted timestamps for use in motion analysis
        self.telemetry_data['timestamp_utc_ms'] = self.telemetry_data['timestamp_ms'] + timezone_offset_ms
    
    def _validate_motion_detection(self, motion_analysis: Dict[str, Any], gps_data: pd.DataFrame):
        """Validate motion detection results against GPS data"""
        total_motion_events = motion_analysis['total_motion_events']
        
        # Check if GPS data shows significant movement
        gps_velocities = gps_data['velocity_magnitude']
        significant_gps_movement = (gps_velocities > 1.0).sum()  # More than 1 m/s
        avg_gps_velocity = gps_velocities.mean()
        
        self.logger.info(f"GPS data shows {significant_gps_movement} measurements with >1 m/s velocity")
        self.logger.info(f"Average GPS velocity: {avg_gps_velocity:.2f} m/s")
        
        # If GPS shows movement but telemetry detects none, there's a problem
        if significant_gps_movement > 10 and total_motion_events == 0:
            error_msg = (
                f"Motion detection validation failed!\n"
                f"GPS data shows {significant_gps_movement} measurements with significant movement (>1 m/s)\n"
                f"But telemetry analysis detected {total_motion_events} motion events\n"
                f"Average GPS velocity: {avg_gps_velocity:.2f} m/s\n"
                f"This indicates a problem with the motion detection algorithm or timing data."
            )
            self.logger.error(error_msg)
            raise ValueError(error_msg)
        
        self.logger.info("Motion detection validation passed")
    
    def _load_gps_data(self, gps_file: str) -> pd.DataFrame:
        """Load and preprocess GPS data"""
        try:
            gps_data = pd.read_csv(gps_file)
            
            # Handle different GPS data formats
            if 'location.lat' in gps_data.columns and 'location.lon' in gps_data.columns:
                # Format: location.lat, location.lon
                gps_data['latitude'] = gps_data['location.lat']
                gps_data['longitude'] = gps_data['location.lon']
            elif 'latitude' in gps_data.columns and 'longitude' in gps_data.columns:
                # Format: latitude, longitude (already correct)
                pass
            else:
                raise ValueError("GPS data must contain latitude and longitude columns")
            
            # Convert time to datetime and then to UTC timestamp
            # Handle ISO8601 format with milliseconds
            gps_data['datetime'] = pd.to_datetime(gps_data['time'], format='ISO8601')
            gps_data['timestamp_ms'] = gps_data['datetime'].astype(np.int64) // 10**6
            
            # Calculate true velocity magnitude
            gps_data['velocity_magnitude'] = np.sqrt(
                gps_data['velocity.x']**2 + 
                gps_data['velocity.y']**2 + 
                gps_data['velocity.z']**2
            )
            
            self.logger.info(f"Loaded GPS data: {len(gps_data)} records")
            self.logger.info(f"GPS time range: {gps_data['datetime'].min()} to {gps_data['datetime'].max()}")
            self.logger.info(f"GPS position range: lat {gps_data['latitude'].min():.6f} to {gps_data['latitude'].max():.6f}")
            self.logger.info(f"GPS position range: lon {gps_data['longitude'].min():.6f} to {gps_data['longitude'].max():.6f}")
            
            return gps_data
            
        except Exception as e:
            self.logger.error(f"Error loading GPS data: {e}")
            return pd.DataFrame()
    
    def _analyze_motion_from_timing(self, gps_data: pd.DataFrame = None, 
                                   integration_time_ms: float = None) -> Dict[str, Any]:
        """Analyze motion using velocity estimation formulas with integration time support"""
        if self.telemetry_data.empty:
            return {
                'total_motion_events': 0,
                'average_radial_velocity': 0.0,
                'max_radial_velocity': 0.0,
                'motion_direction_data': [],
                'velocity_comparison': [],
                'position_quality_data': [],
                'radial_velocity_distribution': {},
                'integration_time_ms': integration_time_ms or 1000.0
            }
        
        # Use the already-converted UTC timestamps from validation
        if 'timestamp_utc_ms' not in self.telemetry_data.columns:
            raise ValueError("UTC timestamps not available - run time synchronization validation first")
        
        self.logger.info("Using velocity estimation formulas with integration time support")
        
        # Determine optimal integration time if not specified
        if integration_time_ms is None:
            integration_time_ms = self._optimize_integration_time(self.telemetry_data)
        
        self.logger.info(f"Using integration time: {integration_time_ms}ms")
        
        # Analyze motion using velocity estimation formulas with integration time
        motion_events = []
        radial_velocities = []
        motion_directions = []
        velocity_comparison = []
        
        # Group by PCI to analyze each cell separately
        for pci in self.telemetry_data['pci'].unique():
            cell_data = self.telemetry_data[self.telemetry_data['pci'] == pci].copy()
            cell_data = cell_data.sort_values('timestamp_utc_ms')
            
            if len(cell_data) < 2:
                continue
            
            # Process each telemetry point with integration time support
            for i in range(len(cell_data)):
                telemetry_row = cell_data.iloc[i]
                baseline_ms = telemetry_row['timestamp_utc_ms']
                
                # Get integration window data
                end_time = baseline_ms + integration_time_ms
                integration_window = cell_data[
                    (cell_data['timestamp_utc_ms'] >= baseline_ms) & 
                    (cell_data['timestamp_utc_ms'] <= end_time)
                ]
                
                # Estimate velocity with integration time
                velocity_result = self._estimate_velocity_with_integration_time(
                    integration_window, integration_time_ms, baseline_ms
                )
                
                combined_velocity = velocity_result['combined_velocity']
                estimation_quality = velocity_result['estimation_quality']
                
                # Only include significant motion
                if abs(combined_velocity) > 0.1:  # 0.1 m/s threshold
                    # Determine motion direction based on velocity sign
                    if combined_velocity > 0:
                        direction = 'towards'  # Positive velocity = towards gNB
                    else:
                        direction = 'away'     # Negative velocity = away from gNB
                    
                    # Use actual GPS position from Phase 1 integration
                    gps_lat = telemetry_row['gps_latitude']
                    gps_lon = telemetry_row['gps_longitude']
                    gps_alt = telemetry_row['gps_altitude']
                    gps_velocity_magnitude = telemetry_row['gps_velocity_magnitude']
                    gps_match_quality = telemetry_row['gps_match_quality']
                    
                    motion_event = {
                        'timestamp_ms': telemetry_row['timestamp_utc_ms'],
                        'pci': pci,
                        'radial_velocity': abs(combined_velocity),
                        'direction': direction,
                        'velocity_cfo': velocity_result['velocity_cfo'],
                        'velocity_timing': velocity_result['velocity_timing'],
                        'combined_velocity': combined_velocity,
                        'estimation_quality': estimation_quality,
                        'integration_samples': velocity_result['integration_samples'],
                        'integration_time_ms': velocity_result['integration_time_ms'],
                        'freq_offset_hz': telemetry_row.get('freq_offset_hz', 0.0),
                        'dl_carrier_freq': telemetry_row.get('dl_carrier_freq', 3.5e9),
                        'current_timing_offset_us': telemetry_row.get('current_timing_offset_us', 0),
                        'initial_timing_offset_us': telemetry_row.get('initial_timing_offset_us', 0),
                        'timing_measurement_timestamp_ms': telemetry_row.get('timing_measurement_timestamp_ms', 0),
                        'latitude': gps_lat,  # Use actual GPS position
                        'longitude': gps_lon,  # Use actual GPS position
                        'altitude': gps_alt,   # Use actual GPS position
                        'gps_velocity_magnitude': gps_velocity_magnitude,
                        'gps_match_quality': gps_match_quality
                    }
                    
                    motion_events.append(motion_event)
                    radial_velocities.append(abs(combined_velocity))
                    motion_directions.append(direction)
                    
                    # Compare with GPS velocity if available and match quality is good
                    if gps_match_quality > 0.5 and gps_velocity_magnitude > 0:
                        velocity_comparison.append({
                            'telemetry_time': telemetry_row['timestamp_utc_ms'],
                            'gps_time': telemetry_row['timestamp_utc_ms'],  # Same time after matching
                            'radial_velocity': abs(combined_velocity),
                            'gps_velocity': gps_velocity_magnitude,
                            'velocity_difference': abs(combined_velocity) - gps_velocity_magnitude,
                            'direction': direction,
                            'gps_latitude': gps_lat,
                            'gps_longitude': gps_lon,
                            'match_quality': gps_match_quality,
                            'estimation_quality': estimation_quality,
                            'velocity_cfo': velocity_result['velocity_cfo'],
                            'velocity_timing': velocity_result['velocity_timing'],
                            'integration_samples': velocity_result['integration_samples'],
                            'integration_time_ms': velocity_result['integration_time_ms']
                        })
        
        # Calculate statistics
        total_motion_events = len(motion_events)
        average_radial_velocity = np.mean(radial_velocities) if radial_velocities else 0.0
        max_radial_velocity = np.max(np.abs(radial_velocities)) if radial_velocities else 0.0
        
        # Create radial velocity distribution
        radial_velocity_distribution = {}
        if radial_velocities:
            high_velocity = sum(1 for v in radial_velocities if abs(v) >= 10.0)
            medium_velocity = sum(1 for v in radial_velocities if 5.0 <= abs(v) < 10.0)
            low_velocity = sum(1 for v in radial_velocities if abs(v) < 5.0)
            
            radial_velocity_distribution = {
                'high_radial_velocity': high_velocity,
                'medium_radial_velocity': medium_velocity,
                'low_radial_velocity': low_velocity
            }
        
        # Create position quality data based on signal metrics and GPS match quality
        position_quality_data = []
        for _, row in self.telemetry_data.iterrows():
            # Calculate quality score based on signal metrics and GPS match quality
            llr_energy = row.get('llr_energy', 0.0)
            channel_level = max(row.get('channel_level_db', [0.0])) if isinstance(row.get('channel_level_db'), list) else 0.0
            gps_match_quality = row.get('gps_match_quality', 0.0)
            rsrp_dbm = row.get('rsrp_dbm', -140)  # Get RSRP value
            
            # Get all the necessary telemetry fields for quality assessment
            pci = int(row.get('pci', 0))  # Ensure PCI is int
            cfo_est = row.get('cfo_est', 0.0)  # Use fine CFO estimation instead of coarse freq_offset_hz
            timing_offset_us = row.get('current_timing_offset_us', 0.0)
            signal_strength_db = channel_level
            estimated_gps_accuracy_m = row.get('estimated_gps_accuracy_m', 0.0)
            
            # Normalize quality score (0-1) - combine signal quality, RSRP, and GPS match quality
            signal_quality = min(1.0, max(0.0, (llr_energy + abs(channel_level)) / 100.0))
            rsrp_quality = min(1.0, max(0.0, (rsrp_dbm + 140) / 60.0))  # Normalize RSRP (-140 to -80 dBm)
            combined_quality = (signal_quality * 0.4) + (rsrp_quality * 0.3) + (gps_match_quality * 0.3)
            
            position_quality_data.append({
                'timestamp_ms': row['timestamp_utc_ms'],
                'latitude': row['gps_latitude'],  # Use actual GPS position
                'longitude': row['gps_longitude'],  # Use actual GPS position
                'altitude': row['gps_altitude'],   # Use actual GPS position
                'quality_score': combined_quality,
                'signal_quality': signal_quality,
                'rsrp_quality': rsrp_quality,
                'gps_match_quality': gps_match_quality,
                'llr_energy': llr_energy,
                'channel_level_db': channel_level,
                # Include all the telemetry fields for proper quality assessment
                'pci': pci,  # Now properly as int
                'cfo_est': cfo_est,  # Use fine CFO estimation
                'timing_offset_us': timing_offset_us,
                'signal_strength_db': signal_strength_db,
                'estimated_gps_accuracy_m': estimated_gps_accuracy_m,
                'rsrp_dbm': rsrp_dbm,  # Include RSRP
                # Additional fields that might be useful
                'dl_carrier_freq': row.get('dl_carrier_freq', 0.0),
                'subcarrier_spacing': row.get('subcarrier_spacing', 0),
                'nb_antennas_rx': row.get('nb_antennas_rx', 0),
                'effective_antennas': row.get('effective_antennas', 0),
                'checksum': row.get('checksum', 0),
                'frame_number_lsb4': row.get('frame_number_lsb4', 0),
                'half_frame_bit': row.get('half_frame_bit', 0)
            })
        
        return {
            'total_motion_events': total_motion_events,
            'average_radial_velocity': average_radial_velocity,
            'max_radial_velocity': max_radial_velocity,
            'motion_direction_data': motion_events,
            'velocity_comparison': velocity_comparison,
            'position_quality_data': position_quality_data,
            'radial_velocity_distribution': radial_velocity_distribution,
            'integration_time_ms': integration_time_ms
        } 
    
    def _generate_motion_visualization(self, motion_analysis: Dict[str, Any], gps_data: pd.DataFrame, output_dir: str) -> str:
        """Generate separate HTML visualizations for motion direction and UE position quality"""
        
        # Create motion direction map
        motion_direction_file = os.path.join(output_dir, "motion_direction_map.html")
        motion_direction_path = self.visualizer.create_motion_direction_map(
            motion_analysis, gps_data, motion_direction_file
        )
        
        # Create UE position quality heatmap
        quality_heatmap_file = os.path.join(output_dir, "ue_position_quality_heatmap.html")
        quality_heatmap_path = self.visualizer.create_ue_position_quality_heatmap(
            motion_analysis, gps_data, quality_heatmap_file
        )
        
        # Return the motion direction map as the primary result (for backward compatibility)
        # Both files are created and saved
        self.logger.info(f"Generated motion direction map: {motion_direction_path}")
        self.logger.info(f"Generated UE position quality heatmap: {quality_heatmap_path}")
        
        return motion_direction_path
    
    def _estimate_velocity_from_cfo(self, telemetry_row: pd.Series) -> float:
        """Estimate radial velocity from Carrier Frequency Offset (CFO) using the CORRECT field"""
        # Use cfo_est (refined PBCH-DMRS CFO) instead of freq_offset_hz (coarse PSS/SSS CFO)
        cfo_est = telemetry_row.get('cfo_est', 0.0)
        dl_carrier_freq = telemetry_row.get('dl_carrier_freq', 3.5e9)  # Default 3.5 GHz
        
        if dl_carrier_freq <= 0:
            return 0.0
        
        # For now, use cfo_est directly without baseline correction
        # In a full implementation, you would subtract a baseline CFO
        # baseline_cfo = self._get_baseline_cfo(telemetry_row.get('pci', 0))
        # doppler_freq = cfo_est - baseline_cfo
        doppler_freq = cfo_est
        
        # Calculate radial velocity using Doppler effect
        speed_of_light = 3e8  # m/s
        v_radial = (doppler_freq * speed_of_light) / dl_carrier_freq
        
        # LOG THE EXACT CALCULATION TO FILE
        with open('velocity_calculation_debug.log', 'a') as f:
            f.write(f"CFO VELOCITY CALCULATION (CORRECTED):\n")
            f.write(f"  cfo_est = {cfo_est}\n")
            f.write(f"  dl_carrier_freq = {dl_carrier_freq}\n")
            f.write(f"  speed_of_light = {speed_of_light}\n")
            f.write(f"  v_radial = ({doppler_freq} * {speed_of_light}) / {dl_carrier_freq}\n")
            f.write(f"  v_radial = {doppler_freq * speed_of_light} / {dl_carrier_freq}\n")
            f.write(f"  v_radial = {v_radial}\n")
            f.write(f"  timestamp = {telemetry_row.get('timestamp_ms', 'unknown')}\n")
            f.write(f"  pci = {telemetry_row.get('pci', 'unknown')}\n")
            f.write(f"  ---\n")
        
        return v_radial
    
    def _estimate_velocity_from_timing(self, telemetry_row: pd.Series, baseline_ms: float = None) -> float:
        """Estimate radial velocity from timing drift"""
        # Formula: 
        # dt_s = (telemetry.timing_measurement_timestamp_ms - baseline_ms) / 1e3f
        # dτ_us = telemetry.current_timing_offset_us - telemetry.initial_timing_offset_us
        # v_radial2 = (dτ_us * 1e-6f * 3e8f) / dt_s
        
        timing_measurement_timestamp_ms = telemetry_row.get('timing_measurement_timestamp_ms', 0)
        current_timing_offset_us = telemetry_row.get('current_timing_offset_us', 0)
        initial_timing_offset_us = telemetry_row.get('initial_timing_offset_us', 0)
        
        # Use first measurement as baseline if not provided
        if baseline_ms is None:
            baseline_ms = timing_measurement_timestamp_ms
        
        # Calculate time difference in seconds
        dt_s = (timing_measurement_timestamp_ms - baseline_ms) / 1e3
        
        if dt_s <= 0:
            with open('velocity_calculation_debug.log', 'a') as f:
                f.write(f"TIMING VELOCITY CALCULATION: dt_s <= 0, returning 0\n")
                f.write(f"  timestamp = {telemetry_row.get('timestamp_ms', 'unknown')}\n")
                f.write(f"  ---\n")
            return 0.0
        
        # Calculate timing offset difference in microseconds
        dτ_us = current_timing_offset_us - initial_timing_offset_us
        
        # Calculate radial velocity from timing drift
        speed_of_light = 3e8  # m/s
        v_radial2 = (dτ_us * 1e-6 * speed_of_light) / dt_s
        
        # LOG THE EXACT CALCULATION TO FILE
        with open('velocity_calculation_debug.log', 'a') as f:
            f.write(f"TIMING VELOCITY CALCULATION:\n")
            f.write(f"  timing_measurement_timestamp_ms = {timing_measurement_timestamp_ms}\n")
            f.write(f"  baseline_ms = {baseline_ms}\n")
            f.write(f"  current_timing_offset_us = {current_timing_offset_us}\n")
            f.write(f"  initial_timing_offset_us = {initial_timing_offset_us}\n")
            f.write(f"  dt_s = ({timing_measurement_timestamp_ms} - {baseline_ms}) / 1e3 = {dt_s}\n")
            f.write(f"  dτ_us = {current_timing_offset_us} - {initial_timing_offset_us} = {dτ_us}\n")
            f.write(f"  speed_of_light = {speed_of_light}\n")
            f.write(f"  v_radial2 = ({dτ_us} * 1e-6 * {speed_of_light}) / {dt_s}\n")
            f.write(f"  v_radial2 = {dτ_us * 1e-6 * speed_of_light} / {dt_s}\n")
            f.write(f"  v_radial2 = {v_radial2}\n")
            f.write(f"  timestamp = {telemetry_row.get('timestamp_ms', 'unknown')}\n")
            f.write(f"  pci = {telemetry_row.get('pci', 'unknown')}\n")
            f.write(f"  ---\n")
        
        return v_radial2
    
    def _combine_velocity_estimates(self, velocity_cfo: float, velocity_timing: float, 
                                   telemetry_row: pd.Series) -> float:
        """Combine CFO and timing-based velocity estimates"""
        # Get signal quality indicators
        llr_energy = telemetry_row.get('llr_energy', 0.0)
        channel_level = max(telemetry_row.get('channel_level_db', [0.0])) if isinstance(telemetry_row.get('channel_level_db'), list) else 0.0
        
        # Assess signal quality (0-1 scale)
        signal_quality = min(1.0, max(0.0, (llr_energy + abs(channel_level)) / 100.0))
        
        # Weight estimates based on signal quality
        # Good signal: 70% CFO, 30% timing
        # Poor signal: 30% CFO, 70% timing
        if signal_quality > 0.5:
            cfo_weight = 0.7
            timing_weight = 0.3
        else:
            cfo_weight = 0.3
            timing_weight = 0.7
        
        # Combine estimates
        combined_velocity = (velocity_cfo * cfo_weight) + (velocity_timing * timing_weight)
        
        # LOG THE EXACT CALCULATION TO FILE
        with open('velocity_calculation_debug.log', 'a') as f:
            f.write(f"COMBINE VELOCITY CALCULATION:\n")
            f.write(f"  velocity_cfo = {velocity_cfo}\n")
            f.write(f"  velocity_timing = {velocity_timing}\n")
            f.write(f"  llr_energy = {llr_energy}\n")
            f.write(f"  channel_level = {channel_level}\n")
            f.write(f"  signal_quality = ({llr_energy} + {abs(channel_level)}) / 100 = {signal_quality}\n")
            f.write(f"  cfo_weight = {cfo_weight}\n")
            f.write(f"  timing_weight = {timing_weight}\n")
            f.write(f"  combined_velocity = ({velocity_cfo} * {cfo_weight}) + ({velocity_timing} * {timing_weight})\n")
            f.write(f"  combined_velocity = {velocity_cfo * cfo_weight} + {velocity_timing * timing_weight}\n")
            f.write(f"  combined_velocity = {combined_velocity}\n")
            f.write(f"  timestamp = {telemetry_row.get('timestamp_ms', 'unknown')}\n")
            f.write(f"  pci = {telemetry_row.get('pci', 'unknown')}\n")
            f.write(f"  ---\n")
        
        return combined_velocity
    
    def _assess_velocity_estimation_quality(self, telemetry_row: pd.Series) -> float:
        """Assess the quality of velocity estimation"""
        # Factors affecting velocity estimation quality:
        # 1. Signal strength (LLR energy, channel level)
        # 2. Frequency offset stability
        # 3. Timing stability
        
        llr_energy = telemetry_row.get('llr_energy', 0.0)
        channel_level = max(telemetry_row.get('channel_level_db', [0.0])) if isinstance(telemetry_row.get('channel_level_db'), list) else 0.0
        freq_offset_hz = abs(telemetry_row.get('freq_offset_hz', 0.0))
        
        # Normalize factors (0-1 scale)
        signal_strength = min(1.0, max(0.0, (llr_energy + abs(channel_level)) / 100.0))
        
        # Frequency offset stability (lower is better for estimation)
        freq_stability = max(0.0, 1.0 - (freq_offset_hz / 1000.0))  # Normalize to 1kHz max
        
        # Timing stability (assess based on timing offset magnitude)
        timing_offset = abs(telemetry_row.get('current_timing_offset_us', 0))
        timing_stability = max(0.0, 1.0 - (timing_offset / 100.0))  # Normalize to 100μs max
        
        # Combine quality factors
        quality_score = (signal_strength * 0.5) + (freq_stability * 0.3) + (timing_stability * 0.2)
        
        return quality_score 
    
    def _estimate_velocity_with_integration_time(self, telemetry_data: pd.DataFrame, 
                                               integration_time_ms: float = 1000.0,
                                               baseline_ms: float = None) -> Dict[str, Any]:
        """Estimate velocity using multiple timestamps over integration time period"""
        
        if telemetry_data.empty:
            return {
                'combined_velocity': 0.0,
                'velocity_cfo': 0.0,
                'velocity_timing': 0.0,
                'estimation_quality': 0.0,
                'integration_samples': 0,
                'integration_time_ms': integration_time_ms
            }
        
        # Sort by timestamp
        sorted_data = telemetry_data.sort_values('timestamp_utc_ms')
        
        # Use first timestamp as baseline if not provided
        if baseline_ms is None:
            baseline_ms = sorted_data.iloc[0]['timestamp_utc_ms']
        
        # Find data points within integration time window
        end_time = baseline_ms + integration_time_ms
        integration_data = sorted_data[
            (sorted_data['timestamp_utc_ms'] >= baseline_ms) & 
            (sorted_data['timestamp_utc_ms'] <= end_time)
        ]
        
        if len(integration_data) < 2:
            # Fall back to single timestamp estimation
            return self._estimate_velocity_single_timestamp(integration_data.iloc[0] if not integration_data.empty else sorted_data.iloc[0])
        
        # Calculate velocity estimates for each point in integration window
        cfo_velocities = []
        timing_velocities = []
        qualities = []
        
        for _, row in integration_data.iterrows():
            # CFO velocity estimation
            cfo_vel = self._estimate_velocity_from_cfo(row)
            cfo_velocities.append(cfo_vel)
            
            # Timing velocity estimation
            timing_vel = self._estimate_velocity_from_timing(row, baseline_ms)
            timing_velocities.append(timing_vel)
            
            # Quality assessment
            quality = self._assess_velocity_estimation_quality(row)
            qualities.append(quality)
        
        # Average the estimates over integration time
        avg_cfo_velocity = np.mean(cfo_velocities) if cfo_velocities else 0.0
        avg_timing_velocity = np.mean(timing_velocities) if timing_velocities else 0.0
        avg_quality = np.mean(qualities) if qualities else 0.0
        
        # Combine averaged estimates
        combined_velocity = self._combine_velocity_estimates(avg_cfo_velocity, avg_timing_velocity, integration_data.iloc[0])
        
        return {
            'combined_velocity': combined_velocity,
            'velocity_cfo': avg_cfo_velocity,
            'velocity_timing': avg_timing_velocity,
            'estimation_quality': avg_quality,
            'integration_samples': len(integration_data),
            'integration_time_ms': integration_time_ms,
            'cfo_velocities': cfo_velocities,
            'timing_velocities': timing_velocities,
            'qualities': qualities
        }
    
    def _estimate_velocity_single_timestamp(self, telemetry_row: pd.Series) -> Dict[str, Any]:
        """Estimate velocity using single timestamp (original method)"""
        velocity_cfo = self._estimate_velocity_from_cfo(telemetry_row)
        velocity_timing = self._estimate_velocity_from_timing(telemetry_row)
        combined_velocity = self._combine_velocity_estimates(velocity_cfo, velocity_timing, telemetry_row)
        estimation_quality = self._assess_velocity_estimation_quality(telemetry_row)
        
        return {
            'combined_velocity': combined_velocity,
            'velocity_cfo': velocity_cfo,
            'velocity_timing': velocity_timing,
            'estimation_quality': estimation_quality,
            'integration_samples': 1,
            'integration_time_ms': 0.0
        }
    
    def _optimize_integration_time(self, telemetry_data: pd.DataFrame, 
                                  max_integration_time_ms: float = 5000.0) -> float:
        """Find optimal integration time for best velocity estimation"""
        
        if telemetry_data.empty:
            return 1000.0  # Default 1 second
        
        # Test different integration times
        integration_times = [100, 200, 500, 1000, 2000, 3000, 5000]  # milliseconds
        integration_times = [t for t in integration_times if t <= max_integration_time_ms]
        
        best_quality = 0.0
        optimal_time = 1000.0  # Default
        
        for integration_time in integration_times:
            result = self._estimate_velocity_with_integration_time(
                telemetry_data, integration_time
            )
            
            # Quality metric: combine estimation quality with number of samples
            quality_score = result['estimation_quality'] * min(1.0, result['integration_samples'] / 10.0)
            
            if quality_score > best_quality:
                best_quality = quality_score
                optimal_time = integration_time
        
        self.logger.info(f"Optimal integration time: {optimal_time}ms (quality: {best_quality:.3f})")
        return optimal_time 