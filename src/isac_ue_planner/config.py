"""
Configuration settings for RF Planner system
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
import json

@dataclass
class RFPlannerConfig:
    """Configuration for RF Planner system"""
    
    # Data paths
    telemetry_data_path: str = "../tiami_data"
    output_path: str = "./rfplanner_output"
    
    # Telemetry file patterns
    telemetry_file_pattern: str = "pbch_telemetry_*.json"
    raw_iq_file: str = "raw_iq_data.bin"
    cir_data_file: str = "cir_data.bin"
    
    # ISAC-specific settings
    isac_enabled: bool = True
    motion_detection_threshold_us: float = 10.0  # microseconds
    timing_accuracy_threshold_us: float = 5.0
    
    # RF Planning parameters
    min_snr_db: float = 10.0
    max_path_loss_db: float = 140.0
    coverage_radius_m: float = 500.0
    
    # Antenna parameters
    antenna_gain_dbi: float = 3.0
    cable_loss_db: float = 2.0
    noise_figure_db: float = 7.0
    
    # Frequency bands (5G NR)
    frequency_bands: Dict[str, Dict] = None
    
    # Visualization settings
    map_center_lat: float = 40.7128  # Default to NYC
    map_center_lon: float = -74.0060
    map_zoom: int = 12
    
    # Google Maps 3D Tiles settings
    google_maps_api_key: Optional[str] = None  # Google Maps Platform API key
    google_maps_3d_enabled: bool = False  # Enable Google Maps 3D Tiles
    cesium_ion_token: Optional[str] = None  # Optional Cesium Ion access token
    
    # Real-time settings
    update_interval_ms: int = 1000
    max_history_points: int = 1000
    
    # Logging
    log_level: str = "INFO"
    log_file: str = "rfplanner.log"
    
    # Visualization configuration
    visualization: Dict[str, Any] = None
    
    # Motion analysis configuration
    motion_analysis: Dict[str, Any] = None
    
    # Position estimation configuration
    position_estimation: Dict[str, Any] = None
    
    # RF planning configuration
    rf_planning: Dict[str, Any] = None
    
    # Clustering configuration
    clustering: Dict[str, Any] = None
    
    # Export configuration
    export: Dict[str, Any] = None
    
    # Real-time configuration
    real_time: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.frequency_bands is None:
            self.frequency_bands = {
                "n41": {"freq_mhz": 2500, "bandwidth_mhz": 100},
                "n77": {"freq_mhz": 3700, "bandwidth_mhz": 100},
                "n78": {"freq_mhz": 3500, "bandwidth_mhz": 100},
                "n79": {"freq_mhz": 4700, "bandwidth_mhz": 100}
            }
        
        if self.visualization is None:
            self.visualization = {
                "figure_size": [12, 8],
                "dpi": 300,
                "style": "seaborn-v0_8",
                "color_palette": "viridis",
                "save_format": "png"
            }
        
        if self.motion_analysis is None:
            self.motion_analysis = {
                "stationary_threshold_us": 5.0,
                "slow_motion_threshold_us": 20.0,
                "fast_motion_threshold_us": 50.0,
                "confidence_threshold": 0.7
            }
        
        if self.position_estimation is None:
            self.position_estimation = {
                "timing_weight": 0.6,
                "signal_strength_weight": 0.4,
                "min_confidence": 0.5,
                "max_position_error_m": 100.0
            }
        
        if self.rf_planning is None:
            self.rf_planning = {
                "environment": "urban",
                "path_loss_model": "cost231",
                "shadow_fading_std_db": 8.0,
                "penetration_loss_db": 15.0,
                "coverage_quality_thresholds": {
                    "excellent_snr_db": 20.0,
                    "good_snr_db": 15.0,
                    "fair_snr_db": 10.0,
                    "poor_snr_db": 5.0
                }
            }
        
        if self.clustering is None:
            self.clustering = {
                "n_clusters": 5,
                "random_state": 42,
                "min_samples": 3,
                "eps": 50.0
            }
        
        if self.export is None:
            self.export = {
                "include_raw_data": True,
                "include_processed_data": True,
                "include_statistics": True,
                "include_visualizations": True,
                "compression": False
            }
        
        if self.real_time is None:
            self.real_time = {
                "enable_alerts": True,
                "alert_thresholds": {
                    "low_snr_db": 8.0,
                    "high_interference_db": -60.0,
                    "motion_detected": True,
                    "coverage_gap": True
                },
                "max_data_age_seconds": 300
            }
    
    @classmethod
    def from_file(cls, config_path: str) -> 'RFPlannerConfig':
        """Load configuration from JSON file"""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config_data = json.load(f)
            return cls(**config_data)
        return cls()
    
    def save_to_file(self, config_path: str):
        """Save configuration to JSON file"""
        config_dict = {
            'telemetry_data_path': self.telemetry_data_path,
            'output_path': self.output_path,
            'telemetry_file_pattern': self.telemetry_file_pattern,
            'raw_iq_file': self.raw_iq_file,
            'cir_data_file': self.cir_data_file,
            'isac_enabled': self.isac_enabled,
            'motion_detection_threshold_us': self.motion_detection_threshold_us,
            'timing_accuracy_threshold_us': self.timing_accuracy_threshold_us,
            'min_snr_db': self.min_snr_db,
            'max_path_loss_db': self.max_path_loss_db,
            'coverage_radius_m': self.coverage_radius_m,
            'antenna_gain_dbi': self.antenna_gain_dbi,
            'cable_loss_db': self.cable_loss_db,
            'noise_figure_db': self.noise_figure_db,
            'frequency_bands': self.frequency_bands,
            'map_center_lat': self.map_center_lat,
            'map_center_lon': self.map_center_lon,
            'map_zoom': self.map_zoom,
            'update_interval_ms': self.update_interval_ms,
            'max_history_points': self.max_history_points,
            'log_level': self.log_level,
            'log_file': self.log_file,
            'visualization': self.visualization,
            'motion_analysis': self.motion_analysis,
            'position_estimation': self.position_estimation,
            'rf_planning': self.rf_planning,
            'clustering': self.clustering,
            'export': self.export,
            'real_time': self.real_time
        }
        
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(config_dict, f, indent=2)

# Default configuration
DEFAULT_CONFIG = RFPlannerConfig()

# Constants
TELEMETRY_FIELDS = [
    'timestamp_ms', 'latitude', 'longitude', 'altitude', 'pci', 'ssb_index',
    'sample_offset', 'timing_offset', 'initial_timing_offset_us', 
    'current_timing_offset_us', 'timing_measurement_timestamp_ms', 'freq_offset_hz',
    'nb_antennas_rx', 'effective_antennas', 'channel_level_db', 'mrc_weights',
    'antenna_quality', 'llr_energy', 'checksum', 'subcarrier_spacing',
    'dl_carrier_freq', 'sample_rate', 'frame_number_lsb4', 'half_frame_bit',
    'tdd_pattern', 'cir_blob_path', 'delay_samples', 'delay_us', 'cfo_est',
    'decoder_state', 'iso_timestamp'
]

# ISAC-specific constants
ISAC_MOTION_THRESHOLDS = {
    'stationary': 5.0,      # microseconds
    'slow_motion': 20.0,    # microseconds  
    'fast_motion': 50.0     # microseconds
}

# RF Planning constants
RF_PLANNING_CONSTANTS = {
    'free_space_path_loss_exponent': 2.0,
    'urban_path_loss_exponent': 3.5,
    'indoor_path_loss_exponent': 4.0,
    'shadow_fading_std_db': 8.0,
    'penetration_loss_db': 15.0
} 