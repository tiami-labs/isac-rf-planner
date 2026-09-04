"""
RF Placement Engine
Handles RF planning and optimal UE placement recommendations
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
import logging
from scipy.optimize import minimize
from scipy.spatial.distance import cdist
from sklearn.cluster import KMeans
from ..config import RF_PLANNING_CONSTANTS, DEFAULT_CONFIG

@dataclass
class CellSite:
    """Cell site information"""
    pci: int
    latitude: float
    longitude: float
    altitude: float
    frequency_mhz: float
    antenna_gain_dbi: float
    transmit_power_dbm: float
    coverage_radius_m: float

@dataclass
class CoverageArea:
    """Coverage area definition"""
    latitude: float
    longitude: float
    radius_m: float
    signal_strength_db: float
    snr_db: float
    coverage_quality: str  # 'excellent', 'good', 'fair', 'poor'

@dataclass
class PlacementRecommendation:
    """UE placement recommendation"""
    latitude: float
    longitude: float
    altitude: float
    confidence: float
    reasoning: str
    coverage_quality: str
    expected_snr_db: float
    motion_state: str

class PlacementEngine:
    """RF placement engine for optimal UE positioning"""
    
    def __init__(self, config=DEFAULT_CONFIG):
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.cell_sites = []
        self.coverage_areas = []
        
    def add_cell_site(self, pci: int, lat: float, lon: float, alt: float = 0.0,
                      freq_mhz: float = 3500.0, tx_power_dbm: float = 43.0):
        """Add a cell site to the placement engine"""
        cell_site = CellSite(
            pci=pci,
            latitude=lat,
            longitude=lon,
            altitude=alt,
            frequency_mhz=freq_mhz,
            antenna_gain_dbi=self.config.antenna_gain_dbi,
            transmit_power_dbm=tx_power_dbm,
            coverage_radius_m=self.config.coverage_radius_m
        )
        self.cell_sites.append(cell_site)
        self.logger.info(f"Added cell site PCI {pci} at ({lat:.6f}, {lon:.6f})")
    
    def calculate_path_loss(self, distance_m: float, frequency_mhz: float, 
                           environment: str = 'urban') -> float:
        """Calculate path loss using appropriate model"""
        # Free space path loss
        free_space_loss = 20 * np.log10(distance_m) + 20 * np.log10(frequency_mhz) + 32.44
        
        # Environment-specific path loss exponent
        if environment == 'free_space':
            path_loss_exponent = RF_PLANNING_CONSTANTS['free_space_path_loss_exponent']
        elif environment == 'urban':
            path_loss_exponent = RF_PLANNING_CONSTANTS['urban_path_loss_exponent']
        elif environment == 'indoor':
            path_loss_exponent = RF_PLANNING_CONSTANTS['indoor_path_loss_exponent']
        else:
            path_loss_exponent = RF_PLANNING_CONSTANTS['urban_path_loss_exponent']
        
        # Additional losses
        shadow_fading = np.random.normal(0, RF_PLANNING_CONSTANTS['shadow_fading_std_db'])
        penetration_loss = RF_PLANNING_CONSTANTS['penetration_loss_db']
        
        total_path_loss = free_space_loss + (path_loss_exponent - 2) * 20 * np.log10(distance_m) + shadow_fading + penetration_loss
        
        return total_path_loss
    
    def calculate_received_power(self, cell_site: CellSite, distance_m: float,
                                environment: str = 'urban') -> float:
        """Calculate received power at a given distance"""
        path_loss = self.calculate_path_loss(distance_m, cell_site.frequency_mhz, environment)
        
        # EIRP (Effective Isotropic Radiated Power)
        eirp = cell_site.transmit_power_dbm + cell_site.antenna_gain_dbi
        
        # Received power
        received_power = eirp - path_loss - self.config.cable_loss_db
        
        return received_power
    
    def calculate_snr(self, received_power_dbm: float, noise_floor_dbm: float = -174.0) -> float:
        """Calculate SNR from received power"""
        # Convert to linear scale
        received_power_w = 10**(received_power_dbm / 10) / 1000  # Convert to Watts
        noise_floor_w = 10**(noise_floor_dbm / 10) / 1000
        
        # Calculate SNR
        snr_linear = received_power_w / noise_floor_w
        snr_db = 10 * np.log10(snr_linear)
        
        return snr_db
    
    def analyze_coverage(self, telemetry_data: pd.DataFrame) -> List[CoverageArea]:
        """Analyze coverage based on telemetry data"""
        coverage_areas = []
        
        # Group by PCI to analyze each cell's coverage
        for pci, cell_data in telemetry_data.groupby('pci'):
            if len(cell_data) < 5:  # Need minimum data points
                continue
            
            # Find the cell site
            cell_site = next((site for site in self.cell_sites if site.pci == pci), None)
            if not cell_site:
                # Create a virtual cell site from telemetry data
                avg_lat = cell_data['latitude'].mean()
                avg_lon = cell_data['longitude'].mean()
                avg_alt = cell_data['altitude'].mean()
                
                cell_site = CellSite(
                    pci=pci,
                    latitude=avg_lat,
                    longitude=avg_lon,
                    altitude=avg_alt,
                    frequency_mhz=3500.0,  # Default 5G frequency
                    antenna_gain_dbi=self.config.antenna_gain_dbi,
                    transmit_power_dbm=43.0,
                    coverage_radius_m=self.config.coverage_radius_m
                )
                self.cell_sites.append(cell_site)
            
            # Calculate coverage metrics
            coverage = self._calculate_cell_coverage(cell_site, cell_data)
            coverage_areas.append(coverage)
        
        self.coverage_areas = coverage_areas
        return coverage_areas
    
    def _calculate_cell_coverage(self, cell_site: CellSite, cell_data: pd.DataFrame) -> CoverageArea:
        """Calculate coverage for a specific cell"""
        # Calculate distances from cell site to measurement points
        distances = []
        signal_strengths = []
        
        for _, row in cell_data.iterrows():
            distance = self._haversine_distance(
                cell_site.latitude, cell_site.longitude,
                row['latitude'], row['longitude']
            ) * 1000  # Convert to meters
            
            distances.append(distance)
            
            # Use actual signal strength if available
            if isinstance(row['channel_level_db'], list):
                valid_levels = [level for level in row['channel_level_db'] if level > -999.0]
                if valid_levels:
                    signal_strengths.append(np.mean(valid_levels))
                else:
                    # Calculate theoretical signal strength
                    rx_power = self.calculate_received_power(cell_site, distance)
                    signal_strengths.append(rx_power)
            else:
                # Calculate theoretical signal strength
                rx_power = self.calculate_received_power(cell_site, distance)
                signal_strengths.append(rx_power)
        
        # Calculate coverage metrics
        avg_signal_strength = np.mean(signal_strengths)
        avg_distance = np.mean(distances)
        snr = self.calculate_snr(avg_signal_strength)
        
        # Determine coverage quality
        if snr > 20:
            coverage_quality = 'excellent'
        elif snr > 15:
            coverage_quality = 'good'
        elif snr > 10:
            coverage_quality = 'fair'
        else:
            coverage_quality = 'poor'
        
        return CoverageArea(
            latitude=cell_site.latitude,
            longitude=cell_site.longitude,
            radius_m=avg_distance,
            signal_strength_db=avg_signal_strength,
            snr_db=snr,
            coverage_quality=coverage_quality
        )
    
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
    
    def recommend_placement(self, telemetry_data: pd.DataFrame, 
                           motion_state: str = 'stationary') -> List[PlacementRecommendation]:
        """Recommend optimal UE placement based on telemetry data"""
        recommendations = []
        
        # Analyze coverage first
        coverage_areas = self.analyze_coverage(telemetry_data)
        
        # Group by PCI for placement recommendations
        for pci, cell_data in telemetry_data.groupby('pci'):
            if len(cell_data) < 3:
                continue
            
            # Find the best coverage area for this cell
            cell_coverage = next((area for area in coverage_areas 
                                if self._haversine_distance(area.latitude, area.longitude,
                                                          cell_data['latitude'].mean(), 
                                                          cell_data['longitude'].mean()) < 1000), None)
            
            if cell_coverage:
                # Recommend placement within the coverage area
                recommendation = self._create_placement_recommendation(
                    cell_data, cell_coverage, motion_state
                )
                recommendations.append(recommendation)
            else:
                # Use clustering to find optimal placement
                recommendation = self._cluster_based_placement(cell_data, motion_state)
                if recommendation:
                    recommendations.append(recommendation)
        
        return recommendations
    
    def _create_placement_recommendation(self, cell_data: pd.DataFrame, 
                                       coverage: CoverageArea, 
                                       motion_state: str) -> PlacementRecommendation:
        """Create placement recommendation based on coverage analysis"""
        # Find the point with best signal quality
        best_idx = cell_data['llr_energy'].idxmax()
        best_point = cell_data.loc[best_idx]
        
        # Calculate confidence based on signal quality and motion state
        signal_quality = best_point['llr_energy'] / 1000.0  # Normalize
        motion_confidence = 0.9 if motion_state == 'stationary' else 0.7
        confidence = min(signal_quality * motion_confidence, 1.0)
        
        reasoning = f"Optimal placement based on signal quality (LLR energy: {best_point['llr_energy']:.2f})"
        
        return PlacementRecommendation(
            latitude=best_point['latitude'],
            longitude=best_point['longitude'],
            altitude=best_point['altitude'],
            confidence=confidence,
            reasoning=reasoning,
            coverage_quality=coverage.coverage_quality,
            expected_snr_db=coverage.snr_db,
            motion_state=motion_state
        )
    
    def _cluster_based_placement(self, cell_data: pd.DataFrame, 
                                motion_state: str) -> Optional[PlacementRecommendation]:
        """Use clustering to find optimal placement"""
        if len(cell_data) < 3:
            return None
        
        # Prepare data for clustering
        coords = cell_data[['latitude', 'longitude']].values
        
        # Use K-means clustering
        n_clusters = min(3, len(coords))
        kmeans = KMeans(n_clusters=n_clusters, random_state=42)
        cluster_labels = kmeans.fit_predict(coords)
        
        # Find the cluster with best average signal quality
        best_cluster = 0
        best_avg_quality = -999.0
        
        for cluster_id in range(n_clusters):
            cluster_mask = cluster_labels == cluster_id
            cluster_data = cell_data[cluster_mask]
            
            if len(cluster_data) > 0:
                # Calculate average signal quality
                avg_llr = cluster_data['llr_energy'].mean()
                avg_channel_level = cluster_data['channel_level_db'].apply(
                    lambda x: np.mean([v for v in x if v > -999.0]) if isinstance(x, list) else -999.0
                ).mean()
                
                avg_quality = avg_llr * (avg_channel_level + 100) / 1000.0
                
                if avg_quality > best_avg_quality:
                    best_avg_quality = avg_quality
                    best_cluster = cluster_id
        
        # Get the centroid of the best cluster
        best_cluster_mask = cluster_labels == best_cluster
        best_cluster_data = cell_data[best_cluster_mask]
        
        if len(best_cluster_data) == 0:
            return None
        
        # Calculate centroid
        centroid_lat = best_cluster_data['latitude'].mean()
        centroid_lon = best_cluster_data['longitude'].mean()
        centroid_alt = best_cluster_data['altitude'].mean()
        
        # Calculate confidence
        confidence = min(best_avg_quality, 1.0)
        
        reasoning = f"Cluster-based placement (cluster {best_cluster}, avg quality: {best_avg_quality:.2f})"
        
        return PlacementRecommendation(
            latitude=centroid_lat,
            longitude=centroid_lon,
            altitude=centroid_alt,
            confidence=confidence,
            reasoning=reasoning,
            coverage_quality='good' if confidence > 0.5 else 'fair',
            expected_snr_db=15.0,  # Estimate
            motion_state=motion_state
        )
    
    def optimize_network_planning(self, telemetry_data: pd.DataFrame) -> Dict[str, Any]:
        """Optimize network planning based on telemetry data"""
        optimization_results = {
            'coverage_gaps': [],
            'interference_areas': [],
            'capacity_issues': [],
            'recommendations': []
        }
        
        # Analyze coverage gaps
        coverage_analysis = self._analyze_coverage_gaps(telemetry_data)
        optimization_results['coverage_gaps'] = coverage_analysis
        
        # Analyze interference
        interference_analysis = self._analyze_interference(telemetry_data)
        optimization_results['interference_areas'] = interference_analysis
        
        # Analyze capacity
        capacity_analysis = self._analyze_capacity_issues(telemetry_data)
        optimization_results['capacity_issues'] = capacity_analysis
        
        # Generate recommendations
        recommendations = self._generate_network_recommendations(
            coverage_analysis, interference_analysis, capacity_analysis
        )
        optimization_results['recommendations'] = recommendations
        
        return optimization_results
    
    def _analyze_coverage_gaps(self, telemetry_data: pd.DataFrame) -> List[Dict[str, Any]]:
        """Analyze coverage gaps in the network"""
        coverage_gaps = []
        
        # Find areas with poor signal quality
        poor_coverage = telemetry_data[
            telemetry_data['channel_level_db'].apply(
                lambda x: np.mean([v for v in x if v > -999.0]) if isinstance(x, list) else -999.0
            ) < -90.0
        ]
        
        if len(poor_coverage) > 0:
            # Cluster poor coverage areas
            poor_coords = poor_coverage[['latitude', 'longitude']].values
            
            if len(poor_coords) >= 3:
                kmeans = KMeans(n_clusters=min(3, len(poor_coords)), random_state=42)
                cluster_labels = kmeans.fit_predict(poor_coords)
                
                for cluster_id in range(kmeans.n_clusters_):
                    cluster_mask = cluster_labels == cluster_id
                    cluster_data = poor_coverage[cluster_mask]
                    
                    if len(cluster_data) > 0:
                        gap_center = {
                            'latitude': cluster_data['latitude'].mean(),
                            'longitude': cluster_data['longitude'].mean(),
                            'radius_m': 500.0,  # Estimate
                            'severity': 'high' if len(cluster_data) > 10 else 'medium',
                            'affected_measurements': len(cluster_data)
                        }
                        coverage_gaps.append(gap_center)
        
        return coverage_gaps
    
    def _analyze_interference(self, telemetry_data: pd.DataFrame) -> List[Dict[str, Any]]:
        """Analyze interference areas"""
        interference_areas = []
        
        # Look for areas with high signal variance (potential interference)
        signal_variance = telemetry_data['channel_level_db'].apply(
            lambda x: np.var([v for v in x if v > -999.0]) if isinstance(x, list) and len(x) > 1 else 0.0
        )
        
        high_variance = telemetry_data[signal_variance > 100.0]  # High variance threshold
        
        if len(high_variance) > 0:
            # Cluster high variance areas
            high_var_coords = high_variance[['latitude', 'longitude']].values
            
            if len(high_var_coords) >= 3:
                kmeans = KMeans(n_clusters=min(3, len(high_var_coords)), random_state=42)
                cluster_labels = kmeans.fit_predict(high_var_coords)
                
                for cluster_id in range(kmeans.n_clusters_):
                    cluster_mask = cluster_labels == cluster_id
                    cluster_data = high_variance[cluster_mask]
                    
                    if len(cluster_data) > 0:
                        interference_area = {
                            'latitude': cluster_data['latitude'].mean(),
                            'longitude': cluster_data['longitude'].mean(),
                            'radius_m': 300.0,  # Estimate
                            'interference_level': 'high' if len(cluster_data) > 5 else 'medium',
                            'affected_measurements': len(cluster_data)
                        }
                        interference_areas.append(interference_area)
        
        return interference_areas
    
    def _analyze_capacity_issues(self, telemetry_data: pd.DataFrame) -> List[Dict[str, Any]]:
        """Analyze capacity issues"""
        capacity_issues = []
        
        # Look for areas with high load (many measurements in short time)
        time_grouped = telemetry_data.groupby(
            pd.Grouper(key='timestamp', freq='1H')
        ).size()
        
        high_load_hours = time_grouped[time_grouped > time_grouped.mean() + 2 * time_grouped.std()]
        
        for timestamp, count in high_load_hours.items():
            hour_data = telemetry_data[
                (telemetry_data['timestamp'] >= timestamp) & 
                (telemetry_data['timestamp'] < timestamp + pd.Timedelta(hours=1))
            ]
            
            if len(hour_data) > 0:
                capacity_issue = {
                    'timestamp': timestamp,
                    'location': {
                        'latitude': hour_data['latitude'].mean(),
                        'longitude': hour_data['longitude'].mean()
                    },
                    'measurement_count': count,
                    'severity': 'high' if count > time_grouped.mean() + 3 * time_grouped.std() else 'medium'
                }
                capacity_issues.append(capacity_issue)
        
        return capacity_issues
    
    def _generate_network_recommendations(self, coverage_gaps: List[Dict], 
                                        interference_areas: List[Dict],
                                        capacity_issues: List[Dict]) -> List[str]:
        """Generate network planning recommendations"""
        recommendations = []
        
        # Coverage gap recommendations
        if coverage_gaps:
            recommendations.append(
                f"Add {len(coverage_gaps)} new cell sites to address coverage gaps"
            )
        
        # Interference recommendations
        if interference_areas:
            recommendations.append(
                f"Optimize antenna configurations in {len(interference_areas)} interference areas"
            )
        
        # Capacity recommendations
        if capacity_issues:
            recommendations.append(
                f"Consider capacity expansion in areas with high traffic load"
            )
        
        # General recommendations
        if not recommendations:
            recommendations.append("Network performance is generally good")
        
        return recommendations 