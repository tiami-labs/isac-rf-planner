"""
Telemetry Data Processor for RF Planner
Handles reading and parsing telemetry data from NR PBCH decoding
"""

import json
import os
import glob
import logging
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from .config import TELEMETRY_FIELDS, DEFAULT_CONFIG

@dataclass
class TelemetryRecord:
    """Single telemetry record from PBCH decoding"""
    timestamp_ms: int
    latitude: float
    longitude: float
    altitude: float
    pci: int
    ssb_index: int
    sample_offset: int
    timing_offset: int
    initial_timing_offset_us: int
    current_timing_offset_us: int
    timing_measurement_timestamp_ms: int
    freq_offset_hz: float
    nb_antennas_rx: int
    effective_antennas: int
    channel_level_db: List[float]
    mrc_weights: List[float]
    antenna_quality: List[float]
    llr_energy: float
    checksum: int
    subcarrier_spacing: int
    dl_carrier_freq: int
    sample_rate: int
    frame_number_lsb4: int
    half_frame_bit: int
    tdd_pattern: Dict[str, int]
    cir_blob_path: str
    delay_samples: int
    delay_us: float
    cfo_est: float
    decoder_state: int
    iso_timestamp: str

class TelemetryProcessor:
    """Processes telemetry data from NR PBCH decoding"""
    
    def __init__(self, config=DEFAULT_CONFIG):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.telemetry_data = []
        self.processed_data = pd.DataFrame()
        
    def load_telemetry_files(self) -> List[str]:
        """Load all telemetry files from the configured directory"""
        pattern = os.path.join(self.config.telemetry_data_path, 
                              self.config.telemetry_file_pattern)
        files = glob.glob(pattern)
        
        if not files:
            self.logger.warning(f"No telemetry files found matching pattern: {pattern}")
            return []
        
        self.logger.info(f"Found {len(files)} telemetry files: {files}")
        return sorted(files)
    
    def parse_telemetry_record(self, record: Dict[str, Any]) -> Optional[TelemetryRecord]:
        """Parse a single telemetry record from JSON"""
        try:
            # Handle nested structures
            channel_level_db = record.get('channel_level_db', [-999.0, -999.0])
            mrc_weights = record.get('mrc_weights', [0.0, 0.0])
            antenna_quality = record.get('antenna_quality', [0.0, 0.0])
            tdd_pattern = record.get('tdd_pattern', {})
            
            # Ensure lists are properly formatted
            if isinstance(channel_level_db, list):
                channel_level_db = channel_level_db[:2] + [0.0] * (2 - len(channel_level_db))
            else:
                channel_level_db = [-999.0, -999.0]
                
            if isinstance(mrc_weights, list):
                mrc_weights = mrc_weights[:2] + [0.0] * (2 - len(mrc_weights))
            else:
                mrc_weights = [0.0, 0.0]
                
            if isinstance(antenna_quality, list):
                antenna_quality = antenna_quality[:2] + [0.0] * (2 - len(antenna_quality))
            else:
                antenna_quality = [0.0, 0.0]
            
            return TelemetryRecord(
                timestamp_ms=record.get('timestamp_ms', 0),
                latitude=record.get('latitude', 0.0),
                longitude=record.get('longitude', 0.0),
                altitude=record.get('altitude', 0.0),
                pci=record.get('pci', 0),
                ssb_index=record.get('ssb_index', 0),
                sample_offset=record.get('sample_offset', 0),
                timing_offset=record.get('timing_offset', 0),
                initial_timing_offset_us=record.get('initial_timing_offset_us', 0),
                current_timing_offset_us=record.get('current_timing_offset_us', 0),
                timing_measurement_timestamp_ms=record.get('timing_measurement_timestamp_ms', 0),
                freq_offset_hz=record.get('freq_offset_hz', 0.0),
                nb_antennas_rx=record.get('nb_antennas_rx', 1),
                effective_antennas=record.get('effective_antennas', 1),
                channel_level_db=channel_level_db,
                mrc_weights=mrc_weights,
                antenna_quality=antenna_quality,
                llr_energy=record.get('llr_energy', 0.0),
                checksum=record.get('checksum', 0),
                subcarrier_spacing=record.get('subcarrier_spacing', 15),
                dl_carrier_freq=record.get('dl_carrier_freq', 0),
                sample_rate=record.get('sample_rate', 0),
                frame_number_lsb4=record.get('frame_number_lsb4', 0),
                half_frame_bit=record.get('half_frame_bit', 0),
                tdd_pattern=tdd_pattern,
                cir_blob_path=record.get('cir_blob_path', ''),
                delay_samples=record.get('delay_samples', 0),
                delay_us=record.get('delay_us', 0.0),
                cfo_est=record.get('cfo_est', 0.0),
                decoder_state=record.get('decoder_state', 0),
                iso_timestamp=record.get('iso_timestamp', '')
            )
        except Exception as e:
            self.logger.error(f"Failed to parse telemetry record: {e}")
            return None
    
    def load_telemetry_data(self) -> pd.DataFrame:
        """Load and process all telemetry data"""
        files = self.load_telemetry_files()
        all_records = []
        
        for file_path in files:
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                
                if isinstance(data, list):
                    records = data
                else:
                    records = [data]
                
                for record in records:
                    parsed_record = self.parse_telemetry_record(record)
                    if parsed_record:
                        all_records.append(asdict(parsed_record))
                        
            except Exception as e:
                self.logger.error(f"Failed to load telemetry file {file_path}: {e}")
                continue
        
        if all_records:
            self.processed_data = pd.DataFrame(all_records)
            self.logger.info(f"Loaded {len(self.processed_data)} telemetry records")
            
            # Convert timestamp to datetime
            self.processed_data['timestamp'] = pd.to_datetime(
                self.processed_data['timestamp_ms'], unit='ms'
            )
            
            # Sort by timestamp
            self.processed_data = self.processed_data.sort_values('timestamp')
            
        else:
            self.logger.warning("No valid telemetry records found")
            self.processed_data = pd.DataFrame()
        
        return self.processed_data
    
    def get_latest_data(self, minutes: int = 5) -> pd.DataFrame:
        """Get telemetry data from the last N minutes"""
        if self.processed_data.empty:
            self.load_telemetry_data()
        
        if self.processed_data.empty:
            return pd.DataFrame()
        
        cutoff_time = datetime.now() - timedelta(minutes=minutes)
        latest_data = self.processed_data[
            self.processed_data['timestamp'] >= cutoff_time
        ].copy()
        
        return latest_data
    
    def get_cell_data(self, pci: int) -> pd.DataFrame:
        """Get all data for a specific cell (PCI)"""
        if self.processed_data.empty:
            self.load_telemetry_data()
        
        return self.processed_data[self.processed_data['pci'] == pci].copy()
    
    def get_motion_data(self) -> pd.DataFrame:
        """Get data with motion detection information"""
        if self.processed_data.empty:
            self.load_telemetry_data()
        
        # Calculate timing differences for motion detection
        motion_data = self.processed_data.copy()
        
        # Calculate timing changes
        motion_data['timing_change_us'] = (
            motion_data['current_timing_offset_us'] - 
            motion_data['initial_timing_offset_us']
        )
        
        # Calculate motion magnitude
        motion_data['motion_magnitude'] = np.abs(motion_data['timing_change_us'])
        
        # Classify motion state
        def classify_motion(magnitude):
            if magnitude < self.config.motion_detection_threshold_us:
                return 'stationary'
            elif magnitude < 20.0:
                return 'slow_motion'
            else:
                return 'fast_motion'
        
        motion_data['motion_state'] = motion_data['motion_magnitude'].apply(classify_motion)
        
        return motion_data
    
    def get_signal_quality_data(self) -> pd.DataFrame:
        """Get data with signal quality metrics"""
        if self.processed_data.empty:
            self.load_telemetry_data()
        
        quality_data = self.processed_data.copy()
        
        # Calculate average channel level across antennas
        quality_data['avg_channel_level_db'] = quality_data['channel_level_db'].apply(
            lambda x: np.mean([v for v in x if v > -999.0]) if isinstance(x, list) else -999.0
        )
        
        # Calculate SNR estimate (simplified)
        quality_data['estimated_snr_db'] = quality_data['avg_channel_level_db'] - self.config.noise_figure_db
        
        # Calculate signal quality score
        quality_data['signal_quality_score'] = (
            quality_data['llr_energy'] * 
            quality_data['avg_channel_level_db'].clip(lower=-999.0, upper=0.0) / 1000.0
        )
        
        return quality_data
    
    def export_to_csv(self, output_path: str, data: pd.DataFrame = None):
        """Export processed data to CSV"""
        if data is None:
            data = self.processed_data
        
        if not data.empty:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            data.to_csv(output_path, index=False)
            self.logger.info(f"Exported data to {output_path}")
        else:
            self.logger.warning("No data to export")
    
    def get_statistics(self) -> Dict[str, Any]:
        """Get statistics about the telemetry data"""
        if self.processed_data.empty:
            return {}
        
        stats = {
            'total_records': len(self.processed_data),
            'unique_cells': self.processed_data['pci'].nunique(),
            'time_span': {
                'start': self.processed_data['timestamp'].min(),
                'end': self.processed_data['timestamp'].max()
            },
            'frequency_bands': self.processed_data['subcarrier_spacing'].value_counts().to_dict(),
            'motion_detection': {
                'stationary': 0,
                'slow_motion': 0,
                'fast_motion': 0
            }
        }
        
        # Motion statistics
        motion_data = self.get_motion_data()
        if not motion_data.empty:
            motion_counts = motion_data['motion_state'].value_counts()
            stats['motion_detection'].update(motion_counts.to_dict())
        
        return stats 