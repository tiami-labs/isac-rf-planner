"""
RF Planner Visualization Module
Handles plotting telemetry data, coverage maps, and real-time monitoring
"""

import matplotlib.pyplot as plt
import matplotlib.patches as patches
import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
import logging
import seaborn as sns
from datetime import datetime, timedelta
import folium
from folium import plugins
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from ..config import DEFAULT_CONFIG
from .google_maps_3d import GoogleMaps3DTiles
import os

class RFVisualizer:
    """Visualization tools for RF planning and telemetry data"""
    
    def __init__(self, config=DEFAULT_CONFIG):
        self.config = config
        self.logger = logging.getLogger(__name__)
        plt.style.use('seaborn-v0_8')
        sns.set_palette("husl")
        
        # Initialize Google Maps 3D Tiles if enabled and API key is provided
        self.google_maps_3d = None
        if config.google_maps_3d_enabled and config.google_maps_api_key:
            try:
                self.google_maps_3d = GoogleMaps3DTiles(
                    api_key=config.google_maps_api_key,
                    config=config
                )
                self.logger.info("Google Maps 3D Tiles initialized")
            except Exception as e:
                self.logger.warning(f"Failed to initialize Google Maps 3D Tiles: {e}")
        
    def plot_telemetry_overview(self, telemetry_data: pd.DataFrame, 
                               save_path: Optional[str] = None) -> plt.Figure:
        """Create an overview plot of telemetry data"""
        if telemetry_data.empty:
            self.logger.warning("No telemetry data to plot")
            return None
        
        fig, axes = plt.subplots(2, 3, figsize=(15, 10))
        fig.suptitle('RF Telemetry Data Overview', fontsize=16, fontweight='bold')
        
        # 1. Signal strength over time
        if 'timestamp' in telemetry_data.columns:
            signal_strengths = telemetry_data['channel_level_db'].apply(
                lambda x: np.mean([v for v in x if v > -999.0]) if isinstance(x, list) else -999.0
            )
            axes[0, 0].plot(telemetry_data['timestamp'], signal_strengths, 'b-', alpha=0.7)
            axes[0, 0].set_title('Signal Strength Over Time')
            axes[0, 0].set_ylabel('Signal Strength (dB)')
            axes[0, 0].tick_params(axis='x', rotation=45)
        
        # 2. Motion detection
        if 'motion_magnitude' in telemetry_data.columns:
            axes[0, 1].scatter(telemetry_data['timestamp'], telemetry_data['motion_magnitude'], 
                              alpha=0.6, c='red')
            axes[0, 1].set_title('Motion Magnitude')
            axes[0, 1].set_ylabel('Timing Change (μs)')
            axes[0, 1].tick_params(axis='x', rotation=45)
        
        # 3. LLR energy distribution
        if 'llr_energy' in telemetry_data.columns:
            axes[0, 2].hist(telemetry_data['llr_energy'], bins=30, alpha=0.7, color='green')
            axes[0, 2].set_title('LLR Energy Distribution')
            axes[0, 2].set_xlabel('LLR Energy')
            axes[0, 2].set_ylabel('Frequency')
        
        # 4. PCI distribution
        if 'pci' in telemetry_data.columns:
            pci_counts = telemetry_data['pci'].value_counts()
            axes[1, 0].bar(pci_counts.index, pci_counts.values, alpha=0.7)
            axes[1, 0].set_title('PCI Distribution')
            axes[1, 0].set_xlabel('PCI')
            axes[1, 0].set_ylabel('Count')
        
        # 5. Frequency offset
        if 'freq_offset_hz' in telemetry_data.columns:
            axes[1, 1].hist(telemetry_data['freq_offset_hz'], bins=30, alpha=0.7, color='orange')
            axes[1, 1].set_title('Frequency Offset Distribution')
            axes[1, 1].set_xlabel('Frequency Offset (Hz)')
            axes[1, 1].set_ylabel('Frequency')
        
        # 6. Coverage quality
        if 'avg_channel_level_db' in telemetry_data.columns:
            quality_data = telemetry_data['avg_channel_level_db']
            axes[1, 2].hist(quality_data[quality_data > -999.0], bins=30, alpha=0.7, color='purple')
            axes[1, 2].set_title('Channel Level Distribution')
            axes[1, 2].set_xlabel('Channel Level (dB)')
            axes[1, 2].set_ylabel('Frequency')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            self.logger.info(f"Saved overview plot to {save_path}")
        
        return fig
    
    def plot_coverage_map(self, telemetry_data: pd.DataFrame, 
                         coverage_areas: List[Any] = None,
                         save_path: Optional[str] = None) -> folium.Map:
        """Create an interactive coverage map"""
        if telemetry_data.empty:
            self.logger.warning("No telemetry data for coverage map")
            return None
        
        # Calculate center of the data
        center_lat = telemetry_data['latitude'].mean()
        center_lon = telemetry_data['longitude'].mean()
        
        # Create the map
        coverage_map = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=12,
            tiles='OpenStreetMap'
        )
        
        # Add telemetry points
        for _, row in telemetry_data.iterrows():
            # Calculate signal strength for color coding
            if isinstance(row['channel_level_db'], list):
                signal_strength = np.mean([v for v in row['channel_level_db'] if v > -999.0])
            else:
                signal_strength = -999.0
            
            # Color based on signal strength
            if signal_strength > -70:
                color = 'green'
            elif signal_strength > -80:
                color = 'yellow'
            elif signal_strength > -90:
                color = 'orange'
            else:
                color = 'red'
            
            # Create popup content
            popup_content = f"""
            <b>PCI:</b> {row['pci']}<br>
            <b>Signal Strength:</b> {signal_strength:.1f} dB<br>
            <b>LLR Energy:</b> {row['llr_energy']:.2f}<br>
            <b>Time:</b> {row['timestamp']}<br>
            <b>Motion:</b> {row.get('motion_state', 'unknown')}
            """
            
            folium.CircleMarker(
                location=[row['latitude'], row['longitude']],
                radius=8,
                popup=folium.Popup(popup_content, max_width=300),
                color=color,
                fill=True,
                fillOpacity=0.7
            ).add_to(coverage_map)
        
        # Add coverage areas if provided
        if coverage_areas:
            for area in coverage_areas:
                folium.Circle(
                    location=[area.latitude, area.longitude],
                    radius=area.radius_m,
                    popup=f"Coverage: {area.coverage_quality}<br>SNR: {area.snr_db:.1f} dB",
                    color='blue',
                    fill=True,
                    fillOpacity=0.2
                ).add_to(coverage_map)
        
        # Add layer control
        folium.LayerControl().add_to(coverage_map)
        
        if save_path:
            coverage_map.save(save_path)
            self.logger.info(f"Saved coverage map to {save_path}")
        
        return coverage_map
    
    def plot_coverage_map_3d(self, telemetry_data: pd.DataFrame,
                             coverage_areas: List[Any] = None,
                             gnb_positions: List[Tuple[float, float, float]] = None,
                             save_path: Optional[str] = None) -> Optional[str]:
        """
        Create a 3D coverage map using Google Maps 3D Tiles
        
        Args:
            telemetry_data: DataFrame with telemetry data
            coverage_areas: List of coverage area objects
            gnb_positions: List of (lat, lon, altitude) tuples for gNB positions
            save_path: Path to save the HTML file
            
        Returns:
            Path to the generated HTML file, or None if 3D Tiles not available
        """
        if not self.google_maps_3d:
            self.logger.warning("Google Maps 3D Tiles not available. Enable it in config with API key.")
            return None
        
        if telemetry_data.empty:
            self.logger.warning("No telemetry data for 3D coverage map")
            return None
        
        if save_path is None:
            save_path = os.path.join(self.config.output_path, "coverage_map_3d.html")
        
        try:
            return self.google_maps_3d.create_cesium_html(
                telemetry_data=telemetry_data,
                coverage_areas=coverage_areas,
                gnb_positions=gnb_positions,
                output_path=save_path
            )
        except Exception as e:
            self.logger.error(f"Error creating 3D coverage map: {e}")
            return None
    
    def plot_motion_analysis(self, motion_data: pd.DataFrame,
                           save_path: Optional[str] = None) -> plt.Figure:
        """Plot motion analysis results"""
        if motion_data.empty:
            self.logger.warning("No motion data to plot")
            return None
        
        fig, axes = plt.subplots(2, 2, figsize=(12, 10))
        fig.suptitle('Motion Analysis', fontsize=16, fontweight='bold')
        
        # 1. Motion magnitude over time
        if 'motion_magnitude' in motion_data.columns:
            axes[0, 0].plot(motion_data['timestamp'], motion_data['motion_magnitude'], 'r-', alpha=0.7)
            axes[0, 0].axhline(y=self.config.motion_detection_threshold_us, color='orange', 
                              linestyle='--', label='Threshold')
            axes[0, 0].set_title('Motion Magnitude Over Time')
            axes[0, 0].set_ylabel('Timing Change (μs)')
            axes[0, 0].legend()
            axes[0, 0].tick_params(axis='x', rotation=45)
        
        # 2. Motion state distribution
        if 'motion_state' in motion_data.columns:
            motion_counts = motion_data['motion_state'].value_counts()
            axes[0, 1].pie(motion_counts.values, labels=motion_counts.index, autopct='%1.1f%%')
            axes[0, 1].set_title('Motion State Distribution')
        
        # 3. Motion magnitude histogram
        if 'motion_magnitude' in motion_data.columns:
            axes[1, 0].hist(motion_data['motion_magnitude'], bins=30, alpha=0.7, color='red')
            axes[1, 0].axvline(x=self.config.motion_detection_threshold_us, color='orange', 
                              linestyle='--', label='Threshold')
            axes[1, 0].set_title('Motion Magnitude Distribution')
            axes[1, 0].set_xlabel('Timing Change (μs)')
            axes[1, 0].set_ylabel('Frequency')
            axes[1, 0].legend()
        
        # 4. Motion vs signal quality
        if 'motion_magnitude' in motion_data.columns and 'llr_energy' in motion_data.columns:
            axes[1, 1].scatter(motion_data['motion_magnitude'], motion_data['llr_energy'], 
                              alpha=0.6, c='blue')
            axes[1, 1].set_title('Motion vs Signal Quality')
            axes[1, 1].set_xlabel('Motion Magnitude (μs)')
            axes[1, 1].set_ylabel('LLR Energy')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            self.logger.info(f"Saved motion analysis to {save_path}")
        
        return fig
    
    def plot_signal_quality_trends(self, quality_data: pd.DataFrame,
                                  save_path: Optional[str] = None) -> plt.Figure:
        """Plot signal quality trends"""
        if quality_data.empty:
            self.logger.warning("No quality data to plot")
            return None
        
        fig, axes = plt.subplots(2, 2, figsize=(12, 10))
        fig.suptitle('Signal Quality Analysis', fontsize=16, fontweight='bold')
        
        # 1. Signal strength over time
        if 'timestamp' in quality_data.columns and 'avg_channel_level_db' in quality_data.columns:
            valid_data = quality_data[quality_data['avg_channel_level_db'] > -999.0]
            if not valid_data.empty:
                axes[0, 0].plot(valid_data['timestamp'], valid_data['avg_channel_level_db'], 'b-', alpha=0.7)
                axes[0, 0].set_title('Average Signal Strength Over Time')
                axes[0, 0].set_ylabel('Signal Strength (dB)')
                axes[0, 0].tick_params(axis='x', rotation=45)
        
        # 2. SNR distribution
        if 'estimated_snr_db' in quality_data.columns:
            valid_snr = quality_data[quality_data['estimated_snr_db'] > -999.0]
            if not valid_snr.empty:
                axes[0, 1].hist(valid_snr['estimated_snr_db'], bins=30, alpha=0.7, color='green')
                axes[0, 1].set_title('SNR Distribution')
                axes[0, 1].set_xlabel('SNR (dB)')
                axes[0, 1].set_ylabel('Frequency')
        
        # 3. Signal quality score
        if 'signal_quality_score' in quality_data.columns:
            axes[1, 0].plot(quality_data['timestamp'], quality_data['signal_quality_score'], 'g-', alpha=0.7)
            axes[1, 0].set_title('Signal Quality Score Over Time')
            axes[1, 0].set_ylabel('Quality Score')
            axes[1, 0].tick_params(axis='x', rotation=45)
        
        # 4. Quality vs time of day
        if 'timestamp' in quality_data.columns and 'signal_quality_score' in quality_data.columns:
            quality_data['hour'] = quality_data['timestamp'].dt.hour
            hourly_quality = quality_data.groupby('hour')['signal_quality_score'].mean()
            axes[1, 1].bar(hourly_quality.index, hourly_quality.values, alpha=0.7, color='purple')
            axes[1, 1].set_title('Average Quality by Hour of Day')
            axes[1, 1].set_xlabel('Hour of Day')
            axes[1, 1].set_ylabel('Average Quality Score')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            self.logger.info(f"Saved signal quality trends to {save_path}")
        
        return fig
    
    def create_interactive_dashboard(self, telemetry_data: pd.DataFrame,
                                   motion_events: List[Any] = None,
                                   position_estimates: List[Any] = None) -> go.Figure:
        """Create an interactive Plotly dashboard"""
        if telemetry_data.empty:
            self.logger.warning("No telemetry data for dashboard")
            return None
        
        # Create subplots
        fig = make_subplots(
            rows=3, cols=2,
            subplot_titles=('Signal Strength Over Time', 'Motion Analysis',
                          'Coverage Map', 'Signal Quality Distribution',
                          'PCI Distribution', 'Frequency Offset'),
            specs=[[{"type": "scatter"}, {"type": "scatter"}],
                   [{"type": "scatter", "subplot_type": "mapbox"}, {"type": "histogram"}],
                   [{"type": "bar"}, {"type": "histogram"}]]
        )
        
        # 1. Signal strength over time
        signal_strengths = telemetry_data['channel_level_db'].apply(
            lambda x: np.mean([v for v in x if v > -999.0]) if isinstance(x, list) else -999.0
        )
        valid_signal = signal_strengths > -999.0
        
        fig.add_trace(
            go.Scatter(
                x=telemetry_data.loc[valid_signal, 'timestamp'],
                y=signal_strengths[valid_signal],
                mode='lines+markers',
                name='Signal Strength',
                line=dict(color='blue')
            ),
            row=1, col=1
        )
        
        # 2. Motion analysis
        if 'motion_magnitude' in telemetry_data.columns:
            fig.add_trace(
                go.Scatter(
                    x=telemetry_data['timestamp'],
                    y=telemetry_data['motion_magnitude'],
                    mode='markers',
                    name='Motion Magnitude',
                    marker=dict(color='red', size=6)
                ),
                row=1, col=2
            )
        
        # 3. Coverage map
        fig.add_trace(
            go.Scattermapbox(
                lat=telemetry_data['latitude'],
                lon=telemetry_data['longitude'],
                mode='markers',
                marker=dict(size=8, color=signal_strengths, colorscale='RdYlGn'),
                text=[f"PCI: {pci}<br>Signal: {sig:.1f} dB" 
                      for pci, sig in zip(telemetry_data['pci'], signal_strengths)],
                hoverinfo='text'
            ),
            row=2, col=1
        )
        
        # Update mapbox layout
        fig.update_layout(
            mapbox=dict(
                style="open-street-map",
                center=dict(
                    lat=telemetry_data['latitude'].mean(),
                    lon=telemetry_data['longitude'].mean()
                ),
                zoom=10
            )
        )
        
        # 4. Signal quality distribution
        if 'llr_energy' in telemetry_data.columns:
            fig.add_trace(
                go.Histogram(
                    x=telemetry_data['llr_energy'],
                    name='LLR Energy',
                    nbinsx=30
                ),
                row=2, col=2
            )
        
        # 5. PCI distribution
        pci_counts = telemetry_data['pci'].value_counts()
        fig.add_trace(
            go.Bar(
                x=pci_counts.index,
                y=pci_counts.values,
                name='PCI Count'
            ),
            row=3, col=1
        )
        
        # 6. Frequency offset
        if 'freq_offset_hz' in telemetry_data.columns:
            fig.add_trace(
                go.Histogram(
                    x=telemetry_data['freq_offset_hz'],
                    name='Frequency Offset',
                    nbinsx=30
                ),
                row=3, col=2
            )
        
        # Update layout
        fig.update_layout(
            title_text="RF Telemetry Dashboard",
            showlegend=True,
            height=900
        )
        
        return fig
    
    def plot_network_optimization(self, optimization_results: Dict[str, Any],
                                 save_path: Optional[str] = None) -> plt.Figure:
        """Plot network optimization results"""
        fig, axes = plt.subplots(2, 2, figsize=(12, 10))
        fig.suptitle('Network Optimization Analysis', fontsize=16, fontweight='bold')
        
        # 1. Coverage gaps
        coverage_gaps = optimization_results.get('coverage_gaps', [])
        if coverage_gaps:
            gap_severities = [gap['severity'] for gap in coverage_gaps]
            gap_counts = pd.Series(gap_severities).value_counts()
            axes[0, 0].bar(gap_counts.index, gap_counts.values, alpha=0.7, color='red')
            axes[0, 0].set_title('Coverage Gap Severity')
            axes[0, 0].set_ylabel('Count')
        
        # 2. Interference areas
        interference_areas = optimization_results.get('interference_areas', [])
        if interference_areas:
            interference_levels = [area['interference_level'] for area in interference_areas]
            interference_counts = pd.Series(interference_levels).value_counts()
            axes[0, 1].bar(interference_counts.index, interference_counts.values, alpha=0.7, color='orange')
            axes[0, 1].set_title('Interference Areas')
            axes[0, 1].set_ylabel('Count')
        
        # 3. Capacity issues
        capacity_issues = optimization_results.get('capacity_issues', [])
        if capacity_issues:
            capacity_severities = [issue['severity'] for issue in capacity_issues]
            capacity_counts = pd.Series(capacity_severities).value_counts()
            axes[1, 0].bar(capacity_counts.index, capacity_counts.values, alpha=0.7, color='blue')
            axes[1, 0].set_title('Capacity Issues')
            axes[1, 0].set_ylabel('Count')
        
        # 4. Recommendations summary
        recommendations = optimization_results.get('recommendations', [])
        if recommendations:
            axes[1, 1].text(0.1, 0.5, '\n'.join(recommendations), 
                           transform=axes[1, 1].transAxes, fontsize=10,
                           verticalalignment='center', bbox=dict(boxstyle="round,pad=0.3", 
                                                               facecolor="lightgray"))
            axes[1, 1].set_title('Network Recommendations')
            axes[1, 1].axis('off')
        
        plt.tight_layout()
        
        if save_path:
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            self.logger.info(f"Saved network optimization plot to {save_path}")
        
        return fig
    
    def create_real_time_monitor(self, telemetry_data: pd.DataFrame,
                                update_interval_ms: int = 1000) -> go.Figure:
        """Create a real-time monitoring dashboard"""
        if telemetry_data.empty:
            self.logger.warning("No telemetry data for real-time monitor")
            return None
        
        # Get latest data
        latest_data = telemetry_data.tail(100)  # Last 100 points
        
        # Create real-time dashboard
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Real-time Signal Strength', 'Motion Detection',
                          'Coverage Status', 'System Health'),
            specs=[[{"type": "scatter"}, {"type": "scatter"}],
                   [{"type": "indicator"}, {"type": "bar"}]]
        )
        
        # 1. Real-time signal strength
        signal_strengths = latest_data['channel_level_db'].apply(
            lambda x: np.mean([v for v in x if v > -999.0]) if isinstance(x, list) else -999.0
        )
        valid_signal = signal_strengths > -999.0
        
        fig.add_trace(
            go.Scatter(
                x=latest_data.loc[valid_signal, 'timestamp'],
                y=signal_strengths[valid_signal],
                mode='lines+markers',
                name='Signal Strength',
                line=dict(color='blue', width=2)
            ),
            row=1, col=1
        )
        
        # 2. Motion detection
        if 'motion_magnitude' in latest_data.columns:
            fig.add_trace(
                go.Scatter(
                    x=latest_data['timestamp'],
                    y=latest_data['motion_magnitude'],
                    mode='markers',
                    name='Motion',
                    marker=dict(color='red', size=8)
                ),
            row=1, col=2
            )
        
        # 3. Coverage status indicator
        avg_signal = signal_strengths[valid_signal].mean() if valid_signal.any() else -999.0
        coverage_status = 'Good' if avg_signal > -80 else 'Fair' if avg_signal > -90 else 'Poor'
        
        fig.add_trace(
            go.Indicator(
                mode="gauge+number+delta",
                value=avg_signal,
                domain={'x': [0, 1], 'y': [0, 1]},
                title={'text': "Coverage Status"},
                delta={'reference': -80},
                gauge={
                    'axis': {'range': [-100, -60]},
                    'bar': {'color': "darkblue"},
                    'steps': [
                        {'range': [-100, -90], 'color': "lightgray"},
                        {'range': [-90, -80], 'color': "yellow"},
                        {'range': [-80, -60], 'color': "green"}
                    ],
                    'threshold': {
                        'line': {'color': "red", 'width': 4},
                        'thickness': 0.75,
                        'value': -85
                    }
                }
            ),
            row=2, col=1
        )
        
        # 4. System health (PCI distribution)
        pci_counts = latest_data['pci'].value_counts()
        fig.add_trace(
            go.Bar(
                x=pci_counts.index,
                y=pci_counts.values,
                name='Active Cells'
            ),
            row=2, col=2
        )
        
        # Update layout
        fig.update_layout(
            title_text="Real-time RF Monitor",
            showlegend=True,
            height=800
        )
        
        return fig 

    def create_position_heatmap(self, matched_measurements_df: pd.DataFrame, 
                               gnb_estimation: Dict[str, Any],
                               output_path: str = "./replay_results/position_heatmap.html") -> str:
        """
        Create an interactive heatmap showing UE positions and estimated gNB location
        
        Args:
            matched_measurements_df: DataFrame with matched GPS-telemetry data
            gnb_estimation: Dictionary with gNB estimation results
            output_path: Path to save the HTML map
            
        Returns:
            Path to the generated HTML file
        """
        try:
            # Extract gNB coordinates
            gnb_lat = gnb_estimation.get('estimated_gnb_lat', 0)
            gnb_lon = gnb_estimation.get('estimated_gnb_lon', 0)
            gnb_confidence = gnb_estimation.get('estimated_gnb_confidence', 0)
            
            # Calculate center point for the map
            center_lat = matched_measurements_df['gps_lat'].mean()
            center_lon = matched_measurements_df['gps_lon'].mean()
            
            # Create the base map
            m = folium.Map(
                location=[center_lat, center_lon],
                zoom_start=16,
                tiles='OpenStreetMap'
            )
            
            # Add satellite layer option
            folium.TileLayer(
                tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
                attr='Esri',
                name='Satellite'
            ).add_to(m)
            
            # Create heatmap data from quality scores
            heatmap_data = []
            for _, row in matched_measurements_df.iterrows():
                # Use overall quality score for heatmap intensity
                quality_score = row['overall_quality_score']
                heatmap_data.append([
                    row['gps_lat'], 
                    row['gps_lon'], 
                    quality_score * 10  # Scale for better visibility
                ])
            
            # Add heatmap layer
            folium.plugins.HeatMap(
                heatmap_data,
                name='UE Position Quality Heatmap',
                radius=15,
                blur=10,
                max_zoom=18,
                gradient={0.4: 'blue', 0.6: 'lime', 0.8: 'orange', 1.0: 'red'}
            ).add_to(m)
            
            # Add individual UE positions with quality information
            for _, row in matched_measurements_df.iterrows():
                quality_score = row['overall_quality_score']
                
                # Color based on quality score
                if quality_score >= 0.8:
                    color = 'red'  # Excellent
                elif quality_score >= 0.6:
                    color = 'orange'  # Good
                elif quality_score >= 0.4:
                    color = 'yellow'  # Fair
                else:
                    color = 'blue'  # Poor
                
                # Create popup content
                popup_content = f"""
                <b>UE Position Quality</b><br>
                Quality Score: {quality_score:.3f}<br>
                Signal Strength: {row['signal_strength_db']}<br>
                Timing Offset: {row['timing_offset_us']} μs<br>
                PCI: {row['pci']}<br>
                GPS Accuracy: {row['estimated_gps_accuracy_m']:.1f}m<br>
                Time: {row['gps_timestamp']}<br>
                Coordinates: ({row['gps_lat']:.6f}, {row['gps_lon']:.6f})
                """
                
                folium.CircleMarker(
                    location=[row['gps_lat'], row['gps_lon']],
                    radius=3,
                    popup=folium.Popup(popup_content, max_width=300),
                    color=color,
                    fill=True,
                    fillOpacity=0.7,
                    weight=1
                ).add_to(m)
            
            # Add estimated gNB location
            if gnb_lat != 0 and gnb_lon != 0:
                # Create gNB popup
                gnb_popup = f"""
                <b>Estimated gNB Location</b><br>
                Confidence: {gnb_confidence:.3f}<br>
                Coordinates: ({gnb_lat:.6f}, {gnb_lon:.6f})<br>
                Estimated from {gnb_estimation.get('num_positions_used', 0)} best UE positions
                """
                
                # Add gNB marker with different style
                folium.Marker(
                    location=[gnb_lat, gnb_lon],
                    popup=folium.Popup(gnb_popup, max_width=300),
                    icon=folium.Icon(color='red', icon='tower', prefix='fa'),
                    name='Estimated gNB'
                ).add_to(m)
                
                # Add confidence circle around gNB
                folium.Circle(
                    location=[gnb_lat, gnb_lon],
                    radius=50,  # 50m radius
                    color='red',
                    fill=False,
                    weight=2,
                    opacity=0.8,
                    name='gNB Confidence Area'
                ).add_to(m)
            
            # Add layer control
            folium.LayerControl().add_to(m)
            
            # Add fullscreen option
            plugins.Fullscreen().add_to(m)
            
            # Save the map
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            m.save(output_path)
            
            self.logger.info(f"Position heatmap saved to {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.error(f"Error creating position heatmap: {e}")
            return ""
    
    def create_quality_distribution_plot(self, matched_measurements_df: pd.DataFrame,
                                        output_path: str = "./replay_results/quality_distribution.png") -> str:
        """
        Create quality distribution plots
        
        Args:
            matched_measurements_df: DataFrame with matched measurements
            output_path: Path to save the plot
            
        Returns:
            Path to the generated plot
        """
        try:
            fig, axes = plt.subplots(2, 2, figsize=(15, 12))
            fig.suptitle('UE Position Quality Analysis', fontsize=16, fontweight='bold')
            
            # Overall quality distribution
            axes[0, 0].hist(matched_measurements_df['overall_quality_score'], bins=20, alpha=0.7, color='skyblue', edgecolor='black')
            axes[0, 0].set_title('Overall Quality Score Distribution')
            axes[0, 0].set_xlabel('Quality Score')
            axes[0, 0].set_ylabel('Frequency')
            axes[0, 0].axvline(matched_measurements_df['overall_quality_score'].mean(), color='red', linestyle='--', label=f'Mean: {matched_measurements_df["overall_quality_score"].mean():.3f}')
            axes[0, 0].legend()
            
            # Quality components
            quality_components = ['position_quality_score', 'signal_quality_score', 'timing_quality_score', 'motion_quality_score']
            component_names = ['Position', 'Signal', 'Timing', 'Motion']
            
            for i, (component, name) in enumerate(zip(quality_components, component_names)):
                if i == 0:  # Skip first one as it's already plotted
                    continue
                row, col = i // 2, i % 2
                axes[row, col].hist(matched_measurements_df[component], bins=15, alpha=0.7, color='lightgreen', edgecolor='black')
                axes[row, col].set_title(f'{name} Quality Distribution')
                axes[row, col].set_xlabel('Quality Score')
                axes[row, col].set_ylabel('Frequency')
                axes[row, col].axvline(matched_measurements_df[component].mean(), color='red', linestyle='--', label=f'Mean: {matched_measurements_df[component].mean():.3f}')
                axes[row, col].legend()
            
            plt.tight_layout()
            
            # Save the plot
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"Quality distribution plot saved to {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.error(f"Error creating quality distribution plot: {e}")
            return ""
    
    def create_trajectory_plot(self, matched_measurements_df: pd.DataFrame,
                              gnb_estimation: Dict[str, Any],
                              output_path: str = "./replay_results/trajectory_plot.png") -> str:
        """
        Create trajectory plot showing UE movement and gNB location
        
        Args:
            matched_measurements_df: DataFrame with matched measurements
            gnb_estimation: Dictionary with gNB estimation results
            output_path: Path to save the plot
            
        Returns:
            Path to the generated plot
        """
        try:
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))
            fig.suptitle('UE Trajectory and Quality Analysis', fontsize=16, fontweight='bold')
            
            # Sort by timestamp for proper trajectory
            df_sorted = matched_measurements_df.sort_values('gps_timestamp')
            
            # Trajectory plot with quality coloring
            scatter = ax1.scatter(df_sorted['gps_lon'], df_sorted['gps_lat'], 
                                c=df_sorted['overall_quality_score'], 
                                cmap='viridis', s=30, alpha=0.7)
            
            # Add gNB location
            gnb_lat = gnb_estimation.get('estimated_gnb_lat', 0)
            gnb_lon = gnb_estimation.get('estimated_gnb_lon', 0)
            if gnb_lat != 0 and gnb_lon != 0:
                ax1.scatter(gnb_lon, gnb_lat, color='red', s=200, marker='^', 
                           label=f'Estimated gNB\nConfidence: {gnb_estimation.get("estimated_gnb_confidence", 0):.3f}')
                # Add confidence circle
                circle = plt.Circle((gnb_lon, gnb_lat), 0.0001, color='red', fill=False, alpha=0.5)
                ax1.add_patch(circle)
            
            ax1.set_title('UE Trajectory with Quality Heatmap')
            ax1.set_xlabel('Longitude')
            ax1.set_ylabel('Latitude')
            ax1.legend()
            plt.colorbar(scatter, ax=ax1, label='Quality Score')
            
            # Quality over time
            time_diffs = (df_sorted['gps_timestamp'] - df_sorted['gps_timestamp'].min()).dt.total_seconds()
            ax2.plot(time_diffs, df_sorted['overall_quality_score'], 'b-', alpha=0.7, linewidth=1)
            ax2.scatter(time_diffs, df_sorted['overall_quality_score'], 
                       c=df_sorted['overall_quality_score'], cmap='viridis', s=20)
            ax2.set_title('Quality Score Over Time')
            ax2.set_xlabel('Time (seconds)')
            ax2.set_ylabel('Quality Score')
            ax2.grid(True, alpha=0.3)
            
            plt.tight_layout()
            
            # Save the plot
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            self.logger.info(f"Trajectory plot saved to {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.error(f"Error creating trajectory plot: {e}")
            return ""
    
    def create_comprehensive_visualization(self, matched_measurements_df: pd.DataFrame,
                                         gnb_estimation: Dict[str, Any],
                                         output_dir: str = "./replay_results") -> Dict[str, str]:
        """
        Create comprehensive visualization package
        
        Args:
            matched_measurements_df: DataFrame with matched measurements
            gnb_estimation: Dictionary with gNB estimation results
            output_dir: Directory to save all visualizations
            
        Returns:
            Dictionary with paths to generated visualizations
        """
        results = {}
        
        try:
            # Create heatmap
            heatmap_path = os.path.join(output_dir, "position_heatmap.html")
            results['heatmap'] = self.create_position_heatmap(matched_measurements_df, gnb_estimation, heatmap_path)
            
            # Create quality distribution plot
            quality_plot_path = os.path.join(output_dir, "quality_distribution.png")
            results['quality_distribution'] = self.create_quality_distribution_plot(matched_measurements_df, quality_plot_path)
            
            # Create trajectory plot
            trajectory_plot_path = os.path.join(output_dir, "trajectory_plot.png")
            results['trajectory'] = self.create_trajectory_plot(matched_measurements_df, gnb_estimation, trajectory_plot_path)
            
            self.logger.info(f"Comprehensive visualization package created in {output_dir}")
            return results
            
        except Exception as e:
            self.logger.error(f"Error creating comprehensive visualization: {e}")
            return results 

    def create_motion_direction_map(self, motion_analysis: Dict[str, Any], gps_data: pd.DataFrame, 
                                   output_path: str = "./motion_results/motion_direction_map.html") -> str:
        """
        Create a dark mode HTML map showing motion direction (towards/away from gNB)
        
        Args:
            motion_analysis: Dictionary with motion analysis results
            gps_data: DataFrame with GPS data
            output_path: Path to save the HTML map
            
        Returns:
            Path to the generated HTML file
        """
        try:
            import folium
            from folium import plugins
            import pandas as pd
            
            # Create base map centered on the GPS data
            if motion_analysis.get('motion_direction_data'):
                motion_df = pd.DataFrame(motion_analysis['motion_direction_data'])
                center_lat = motion_df['latitude'].mean()
                center_lon = motion_df['longitude'].mean()
            else:
                # Use GPS data center if no motion events
                center_lat = gps_data['latitude'].mean()
                center_lon = gps_data['longitude'].mean()
            
            # Create dark mode map with black/grey tiles
            m = folium.Map(
                location=[center_lat, center_lon], 
                zoom_start=15,
                tiles='CartoDB dark_matter'
            )
            
            # Add alternative dark tile layer
            folium.TileLayer(
                tiles='https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                attr='CartoDB',
                name='Dark Theme'
            ).add_to(m)
            
            # Add motion direction markers with actual GPS position
            if motion_analysis.get('motion_direction_data'):
                motion_df = pd.DataFrame(motion_analysis['motion_direction_data'])
                
                # Color coding: towards = green, away = red
                for idx, row in motion_df.iterrows():
                    color = 'green' if row['direction'] == 'towards' else 'red'
                    
                    # Enhanced popup with motion details
                    popup_text = f"""
                    <div style="background-color: #2d3748; color: white; padding: 10px; border-radius: 5px;">
                    <h4 style="color: #68d391; margin: 0 0 10px 0;">Motion Analysis</h4>
                    <p><b>Direction:</b> <span style="color: {'#68d391' if row['direction'] == 'towards' else '#fc8181'}">{row['direction']}</span></p>
                    <p><b>Combined Radial Velocity:</b> {row['combined_velocity']:.2f} m/s</p>
                    <p><b>CFO Velocity:</b> {row['velocity_cfo']:.2f} m/s</p>
                    <p><b>Timing Velocity:</b> {row['velocity_timing']:.2f} m/s</p>
                    <p><b>Estimation Quality:</b> {row['estimation_quality']:.2f}</p>
                    <p><b>Integration Time:</b> {row['integration_time_ms']:.0f}ms</p>
                    <p><b>Integration Samples:</b> {row['integration_samples']}</p>
                    <p><b>GPS Position:</b> ({row['latitude']:.6f}, {row['longitude']:.6f})</p>
                    <p><b>GPS Altitude:</b> {row.get('altitude', 0):.1f} m</p>
                    <p><b>GPS Velocity:</b> {row.get('gps_velocity_magnitude', 0):.2f} m/s</p>
                    <p><b>GPS Match Quality:</b> {row.get('gps_match_quality', 0):.2f}</p>
                    <p><b>Time:</b> {pd.to_datetime(row['timestamp_ms'], unit='ms')}</p>
                    <p><b>PCI:</b> {row['pci']}</p>
                    <p><b>Carrier Freq:</b> {row.get('dl_carrier_freq', 0)/1e9:.1f} GHz</p>
                    <p><b>CFO Estimate:</b> {row.get('cfo_est', 0):.1f} Hz</p>
                    </div>
                    """
                    
                    folium.CircleMarker(
                        location=[row['latitude'], row['longitude']],
                        radius=8,
                        popup=folium.Popup(popup_text, max_width=400),
                        color=color,
                        fill=True,
                        fillOpacity=0.7,
                        weight=2
                    ).add_to(m)
            
            # Add velocity comparison layer if available
            if motion_analysis.get('velocity_comparison'):
                velocity_df = pd.DataFrame(motion_analysis['velocity_comparison'])
                
                for idx, row in velocity_df.iterrows():
                    popup_text = f"""
                    <div style="background-color: #2d3748; color: white; padding: 10px; border-radius: 5px;">
                    <h4 style="color: #68d391; margin: 0 0 10px 0;">Velocity Comparison</h4>
                    <p><b>Telemetry Combined Velocity:</b> {row['radial_velocity']:.2f} m/s</p>
                    <p><b>CFO Velocity:</b> {row['velocity_cfo']:.2f} m/s</p>
                    <p><b>Timing Velocity:</b> {row['velocity_timing']:.2f} m/s</p>
                    <p><b>GPS True Velocity:</b> {row['gps_velocity']:.2f} m/s</p>
                    <p><b>Difference:</b> {row['velocity_difference']:.2f} m/s</p>
                    <p><b>Direction:</b> {row['direction']}</p>
                    <p><b>Integration Time:</b> {row['integration_time_ms']:.0f}ms</p>
                    <p><b>Integration Samples:</b> {row['integration_samples']}</p>
                    <p><b>GPS Position:</b> ({row['gps_latitude']:.6f}, {row['gps_longitude']:.6f})</p>
                    <p><b>Match Quality:</b> {row['match_quality']:.2f}</p>
                    <p><b>Estimation Quality:</b> {row['estimation_quality']:.2f}</p>
                    <p><b>Time:</b> {pd.to_datetime(row['telemetry_time'], unit='ms')}</p>
                    </div>
                    """
                    
                    # Color based on velocity difference
                    if abs(row['velocity_difference']) < 2.0:
                        color = 'green'  # Good match
                    elif abs(row['velocity_difference']) < 5.0:
                        color = 'orange'  # Moderate match
                    else:
                        color = 'red'  # Poor match
                    
                    folium.CircleMarker(
                        location=[row['gps_latitude'], row['gps_longitude']],
                        radius=12,
                        popup=folium.Popup(popup_text, max_width=400),
                        color=color,
                        fill=True,
                        fillOpacity=0.8,
                        weight=2
                    ).add_to(m)
            
            # Add dark mode legend
            legend_html = '''
            <div style="position: fixed; 
                        bottom: 50px; left: 50px; width: 450px; height: 380px; 
                        background-color: #2d3748; color: white; border:2px solid #4a5568; 
                        z-index:9999; font-size:14px; padding: 15px; border-radius: 5px;
                        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);">
            <h4 style="color: #68d391; margin: 0 0 15px 0;">Motion Direction Analysis</h4>
            <p><b>Motion Direction</b></p>
            <p><i class="fa fa-circle" style="color:#68d391"></i> Towards gNB (Green)</p>
            <p><i class="fa fa-circle" style="color:#fc8181"></i> Away from gNB (Red)</p>
            <p><b>Velocity Match Quality</b></p>
            <p><i class="fa fa-circle" style="color:#68d391"></i> Good match (≤2 m/s diff)</p>
            <p><i class="fa fa-circle" style="color:#f6ad55"></i> Moderate match (2-5 m/s diff)</p>
            <p><i class="fa fa-circle" style="color:#fc8181"></i> Poor match (>5 m/s diff)</p>
            <p><b>Integration Time Support</b></p>
            <p>Multi-timestamp velocity estimation</p>
            <p>CFO + Timing drift analysis</p>
            <p><b>GPS Position: ACTIVE</b></p>
            </div>
            '''
            m.get_root().html.add_child(folium.Element(legend_html))
            
            # Add layer control
            folium.LayerControl().add_to(m)
            
            # Add fullscreen option
            plugins.Fullscreen().add_to(m)
            
            # Save the map
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            m.save(output_path)
            
            self.logger.info(f"Motion direction map saved to {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.error(f"Error creating motion direction map: {e}")
            return ""
    
    def create_ue_position_quality_heatmap(self, motion_analysis: Dict[str, Any], gps_data: pd.DataFrame,
                                          output_path: str = "./motion_results/ue_position_quality_heatmap.html") -> str:
        """
        Create a dark mode HTML heatmap showing UE position quality assessment
        
        Args:
            motion_analysis: Dictionary with motion analysis results
            gps_data: DataFrame with GPS data
            output_path: Path to save the HTML map
            
        Returns:
            Path to the generated HTML file
        """
        try:
            import folium
            from folium import plugins
            import pandas as pd
            
            # Create base map centered on the GPS data
            if motion_analysis.get('position_quality_data'):
                quality_df = pd.DataFrame(motion_analysis['position_quality_data'])
                center_lat = quality_df['latitude'].mean()
                center_lon = quality_df['longitude'].mean()
            else:
                # Use GPS data center if no quality data
                center_lat = gps_data['latitude'].mean()
                center_lon = gps_data['longitude'].mean()
            
            # Create dark mode map with black/grey tiles
            m = folium.Map(
                location=[center_lat, center_lon], 
                zoom_start=15,
                tiles='CartoDB dark_matter'
            )
            
            # Add alternative dark tile layer
            folium.TileLayer(
                tiles='https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
                attr='CartoDB',
                name='Dark Theme'
            ).add_to(m)
            
            # Create heatmap data from quality scores
            if motion_analysis.get('position_quality_data'):
                quality_df = pd.DataFrame(motion_analysis['position_quality_data'])
                
                heatmap_data = []
                for idx, row in quality_df.iterrows():
                    # Normalize quality score for heatmap intensity
                    intensity = row['quality_score']
                    heatmap_data.append([row['latitude'], row['longitude'], intensity])
                
                # Add heatmap layer with dark mode colors
                folium.plugins.HeatMap(
                    heatmap_data,
                    name='UE Position Quality Heatmap',
                    radius=15,
                    blur=10,
                    max_zoom=18,
                    gradient={0.0: '#2b6cb0', 0.3: '#3182ce', 0.6: '#38a169', 0.8: '#d69e2e', 1.0: '#e53e3e'}
                ).add_to(m)
                
                # Add individual UE positions with quality information
                for idx, row in quality_df.iterrows():
                    quality_score = row['quality_score']
                    
                    # Color based on quality score
                    if quality_score >= 0.8:
                        color = '#e53e3e'  # Excellent (red)
                    elif quality_score >= 0.6:
                        color = '#d69e2e'  # Good (yellow)
                    elif quality_score >= 0.4:
                        color = '#38a169'  # Fair (green)
                    else:
                        color = '#2b6cb0'  # Poor (blue)
                    
                    # Create popup content with dark mode styling and correct fields
                    popup_content = f"""
                    <div style="background-color: #2d3748; color: white; padding: 10px; border-radius: 5px;">
                    <h4 style="color: #68d391; margin: 0 0 10px 0;">UE Position Quality</h4>
                    <p><b>Quality Score:</b> <span style="color: {'#68d391' if quality_score >= 0.6 else '#fc8181'}">{quality_score:.3f}</span></p>
                    <p><b>Signal Strength:</b> {row.get('signal_strength_db', 0):.1f} dB</p>
                    <p><b>RSRP:</b> {row.get('rsrp_dbm', -140):.1f} dBm</p>
                    <p><b>Timing Offset:</b> {row.get('timing_offset_us', 0):.1f} μs</p>
                    <p><b>PCI:</b> {row.get('pci', 0)}</p>
                    <p><b>GPS Accuracy:</b> {row.get('estimated_gps_accuracy_m', 0):.1f}m</p>
                    <p><b>Time:</b> {pd.to_datetime(row.get('timestamp_ms', 0), unit='ms')}</p>
                    <p><b>Coordinates:</b> ({row['latitude']:.6f}, {row['longitude']:.6f})</p>
                    <p><b>Altitude:</b> {row.get('altitude', 0):.1f} m</p>
                    <p><b>LLR Energy:</b> {row.get('llr_energy', 0):.2f}</p>
                    <p><b>CFO Estimate:</b> {row.get('cfo_est', 0):.1f} Hz</p>
                    </div>
                    """
                    
                    folium.CircleMarker(
                        location=[row['latitude'], row['longitude']],
                        radius=3,
                        popup=folium.Popup(popup_content, max_width=350),
                        color=color,
                        fill=True,
                        fillOpacity=0.7,
                        weight=1
                    ).add_to(m)
            
            # Add estimated gNB location if available
            if motion_analysis.get('gnb_estimation'):
                gnb_estimation = motion_analysis['gnb_estimation']
                gnb_lat = gnb_estimation.get('estimated_gnb_lat', 0)
                gnb_lon = gnb_estimation.get('estimated_gnb_lon', 0)
                gnb_confidence = gnb_estimation.get('estimated_gnb_confidence', 0)
                
                if gnb_lat != 0 and gnb_lon != 0:
                    # Create gNB popup with dark mode styling
                    gnb_popup = f"""
                    <div style="background-color: #2d3748; color: white; padding: 10px; border-radius: 5px;">
                    <h4 style="color: #68d391; margin: 0 0 10px 0;">Estimated gNB Location</h4>
                    <p><b>Confidence:</b> {gnb_confidence:.3f}</p>
                    <p><b>Coordinates:</b> ({gnb_lat:.6f}, {gnb_lon:.6f})</p>
                    <p><b>Estimated from:</b> {gnb_estimation.get('num_positions_used', 0)} best UE positions</p>
                    </div>
                    """
                    
                    # Add gNB marker with different style
                    folium.Marker(
                        location=[gnb_lat, gnb_lon],
                        popup=folium.Popup(gnb_popup, max_width=350),
                        icon=folium.Icon(color='red', icon='tower', prefix='fa'),
                        name='Estimated gNB'
                    ).add_to(m)
                    
                    # Add confidence circle around gNB
                    folium.Circle(
                        location=[gnb_lat, gnb_lon],
                        radius=50,  # 50m radius
                        color='#e53e3e',
                        fill=False,
                        weight=2,
                        opacity=0.8,
                        name='gNB Confidence Area'
                    ).add_to(m)
            
            # Add dark mode legend
            legend_html = '''
            <div style="position: fixed; 
                        bottom: 50px; left: 50px; width: 500px; height: 350px; 
                        background-color: #2d3748; color: white; border:2px solid #4a5568; 
                        z-index:9999; font-size:14px; padding: 15px; border-radius: 5px;
                        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);">
            <h4 style="color: #68d391; margin: 0 0 15px 0;">UE Position Quality Heatmap</h4>
            <p><b>Quality Score Colors</b></p>
            <p><i class="fa fa-circle" style="color:#e53e3e"></i> Excellent (≥0.8)</p>
            <p><i class="fa fa-circle" style="color:#d69e2e"></i> Good (0.6-0.8)</p>
            <p><i class="fa fa-circle" style="color:#38a169"></i> Fair (0.4-0.6)</p>
            <p><i class="fa fa-circle" style="color:#2b6cb0"></i> Poor (<0.4)</p>
            <p><b>Heatmap Gradient</b></p>
            <p>Blue → Green → Yellow → Red</p>
            <p><b>Quality Assessment</b></p>
            <p>Based on signal strength, timing accuracy, and GPS precision</p>
            </div>
            '''
            m.get_root().html.add_child(folium.Element(legend_html))
            
            # Add layer control
            folium.LayerControl().add_to(m)
            
            # Add fullscreen option
            plugins.Fullscreen().add_to(m)
            
            # Save the map
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            m.save(output_path)
            
            self.logger.info(f"UE position quality heatmap saved to {output_path}")
            return output_path
            
        except Exception as e:
            self.logger.error(f"Error creating UE position quality heatmap: {e}")
            return "" 