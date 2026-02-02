#!/usr/bin/env python3
"""
RF Planner Main Application
Command-line interface for RF planning and UE placement system
"""

import argparse
import sys
import os
import json
from datetime import datetime
from typing import Dict, Any

from .core import RFPlanner
from .config import RFPlannerConfig

def main():
    """Main application entry point"""
    parser = argparse.ArgumentParser(
        description="RF Planner - ISAC-based UE Placement and RF Planning System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run complete analysis
  python -m rfplanner.main analyze --output ./results

  # Run replay analysis with GPS trajectory
  python -m rfplanner.main replay --gps-file ue_position/00:33:50-01:14:34.csv --output ./replay_results

  # Run real-time monitoring
  python -m rfplanner.main monitor --interval 30

  # Generate visualizations only
  python -m rfplanner.main visualize --output ./plots

  # Export data
  python -m rfplanner.main export --output ./data
        """
    )
    
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Analyze command
    analyze_parser = subparsers.add_parser('analyze', help='Run complete RF analysis')
    analyze_parser.add_argument('--output', '-o', default='./rfplanner_output',
                               help='Output directory for results')
    analyze_parser.add_argument('--config', '-c', help='Configuration file path')
    analyze_parser.add_argument('--save-config', action='store_true',
                               help='Save current configuration to output directory')
    
    # Replay command
    replay_parser = subparsers.add_parser('replay', help='Run replay analysis with GPS trajectory')
    replay_parser.add_argument('--gps-file', '-g', required=True,
                              help='GPS trajectory CSV file path')
    replay_parser.add_argument('--output', '-o', default='./replay_output',
                              help='Output directory for replay results')
    replay_parser.add_argument('--config', '-c', help='Configuration file path')
    replay_parser.add_argument('--telemetry-pattern', '-t', 
                              help='Telemetry file pattern (default: pbch_telemetry_*.json)')
    
    # Monitor command
    monitor_parser = subparsers.add_parser('monitor', help='Run real-time monitoring')
    monitor_parser.add_argument('--interval', '-i', type=int, default=60,
                               help='Update interval in seconds')
    monitor_parser.add_argument('--config', '-c', help='Configuration file path')
    
    # Visualize command
    viz_parser = subparsers.add_parser('visualize', help='Generate visualizations')
    viz_parser.add_argument('--output', '-o', default='./rfplanner_output',
                           help='Output directory for plots')
    viz_parser.add_argument('--config', '-c', help='Configuration file path')
    
    # Export command
    export_parser = subparsers.add_parser('export', help='Export analysis results')
    export_parser.add_argument('--output', '-o', default='./rfplanner_output',
                              help='Output directory for data')
    export_parser.add_argument('--config', '-c', help='Configuration file path')
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show system status')
    status_parser.add_argument('--config', '-c', help='Configuration file path')
    
    # Motion command
    motion_parser = subparsers.add_parser('motion', help='Analyze motion and radial velocity')
    motion_parser.add_argument('--gps-file', '-g', required=True,
                              help='GPS trajectory CSV file path')
    motion_parser.add_argument('--output', '-o', default='./motion_results',
                              help='Output directory for motion analysis results')
    motion_parser.add_argument('--config', '-c', help='Configuration file path')
    motion_parser.add_argument('--telemetry-pattern', '-t', 
                              help='Telemetry file pattern (default: pbch_telemetry_*.json)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    # Load configuration
    config = None
    if args.config:
        config = RFPlannerConfig.from_file(args.config)
    else:
        # Try to load default config file
        default_config_path = "rfplanner/config.json"
        if os.path.exists(default_config_path):
            config = RFPlannerConfig.from_file(default_config_path)
        else:
            print(f"Warning: No config file found at {default_config_path}, using default configuration")
    
    # Initialize RF Planner
    rf_planner = RFPlanner(config)
    
    try:
        if args.command == 'analyze':
            run_analysis(rf_planner, args)
        elif args.command == 'replay':
            run_replay_analysis(rf_planner, args)
        elif args.command == 'monitor':
            run_monitoring(rf_planner, args)
        elif args.command == 'visualize':
            run_visualization(rf_planner, args)
        elif args.command == 'export':
            run_export(rf_planner, args)
        elif args.command == 'status':
            run_status(rf_planner, args)
        elif args.command == 'motion':
            run_motion_analysis(rf_planner, args)
        else:
            print(f"Unknown command: {args.command}")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

def run_analysis(rf_planner: RFPlanner, args):
    """Run complete RF analysis"""
    print("Running complete RF analysis...")
    
    # Set output directory
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    
    # Run analysis
    results = rf_planner.run_complete_analysis(output_dir)
    
    # Save configuration if requested
    if args.save_config:
        config_file = os.path.join(output_dir, 'rfplanner_config.json')
        rf_planner.save_configuration(config_file)
        print(f"Configuration saved to: {config_file}")
    
    # Print summary
    print("\nAnalysis completed successfully!")
    print(f"Results saved to: {output_dir}")
    
    if 'isac_results' in results:
        motion_events = results['isac_results'].get('motion_events', [])
        print(f"Motion events detected: {len(motion_events)}")
    
    if 'planning_results' in results:
        coverage_areas = results['planning_results'].get('coverage_areas', [])
        print(f"Coverage areas analyzed: {len(coverage_areas)}")
    
    if 'recommendations' in results:
        recommendations = results['recommendations']
        if recommendations:
            print(f"\nRecommendations ({len(recommendations)}):")
            for i, rec in enumerate(recommendations, 1):
                print(f"  {i}. [{rec['priority'].upper()}] {rec['message']}")

def run_replay_analysis(rf_planner: RFPlanner, args):
    """Run replay analysis with GPS trajectory"""
    print("Running replay analysis...")
    
    # Check if GPS file exists
    if not os.path.exists(args.gps_file):
        print(f"Error: GPS file not found: {args.gps_file}")
        sys.exit(1)
    
    # Set output directory
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Run replay analysis
        result = rf_planner.run_replay_analysis(
            gps_file=args.gps_file,
            output_dir=output_dir
        )
        
        # Print results
        print("\nReplay analysis completed successfully!")
        print(f"Results saved to: {output_dir}")
        print(f"\nAnalysis Summary:")
        print(f"  - GPS file: {args.gps_file}")
        print(f"  - Total matches: {result.total_matches}")
        print(f"  - Best UE positions: {len(result.best_ue_positions)}")
        print(f"  - Time range: {result.time_range[0]} to {result.time_range[1]}")
        
        # Quality distribution
        if result.quality_distribution:
            dist = result.quality_distribution
            print(f"\nQuality Distribution:")
            print(f"  - Excellent positions (≥0.8): {dist.get('excellent_positions', 0)}")
            print(f"  - Good positions (0.6-0.8): {dist.get('good_positions', 0)}")
            print(f"  - Fair positions (0.4-0.6): {dist.get('fair_positions', 0)}")
            print(f"  - Poor positions (<0.4): {dist.get('poor_positions', 0)}")
        
        # gNB estimation results
        if result.estimated_gnb_position:
            gnb_lat, gnb_lon, confidence = result.estimated_gnb_position
            print(f"\ngNB Position Estimation:")
            print(f"  - Estimated position: ({gnb_lat:.6f}, {gnb_lon:.6f})")
            print(f"  - Confidence: {confidence:.3f}")
            print(f"  - Triangulation quality: {result.triangulation_quality:.3f}")
        
        # Get recommendations
        recommendations = rf_planner.replay_analyzer.get_replay_recommendations(result)
        if recommendations:
            print(f"\nRecommendations ({len(recommendations)}):")
            for i, rec in enumerate(recommendations, 1):
                print(f"  {i}. {rec}")
        
    except Exception as e:
        print(f"Error during replay analysis: {e}")
        sys.exit(1)

def run_monitoring(rf_planner: RFPlanner, args):
    """Run real-time monitoring"""
    print(f"Starting real-time monitoring (update interval: {args.interval}s)")
    print("Press Ctrl+C to stop monitoring")
    
    try:
        rf_planner.run_real_time_monitor(args.interval)
    except KeyboardInterrupt:
        print("\nMonitoring stopped")

def run_visualization(rf_planner: RFPlanner, args):
    """Generate visualizations"""
    print("Generating visualizations...")
    
    # Load data first
    if not rf_planner.load_data():
        print("Error: No telemetry data available for visualization")
        sys.exit(1)
    
    # Generate visualizations
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    
    visualization_files = rf_planner.generate_visualizations(output_dir)
    
    print(f"\nVisualizations generated successfully!")
    print(f"Output directory: {output_dir}")
    print(f"Generated {len(visualization_files)} visualization files:")
    
    for viz_type, file_path in visualization_files.items():
        print(f"  - {viz_type}: {file_path}")

def run_export(rf_planner: RFPlanner, args):
    """Export analysis results"""
    print("Exporting analysis results...")
    
    # Load data first
    if not rf_planner.load_data():
        print("Error: No telemetry data available for export")
        sys.exit(1)
    
    # Export results
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    
    export_files = rf_planner.export_results(output_dir)
    
    print(f"\nExport completed successfully!")
    print(f"Output directory: {output_dir}")
    print(f"Exported {len(export_files)} files:")
    
    for export_type, file_path in export_files.items():
        print(f"  - {export_type}: {file_path}")

def run_status(rf_planner: RFPlanner, args):
    """Show system status"""
    print("RF Planner System Status")
    print("=" * 40)
    
    # Check telemetry data
    if rf_planner.load_data():
        data = rf_planner.telemetry_data
        print(f"Telemetry Data:")
        print(f"  - Records: {len(data)}")
        print(f"  - Time range: {data['timestamp'].min()} to {data['timestamp'].max()}")
        print(f"  - Unique cells (PCI): {data['pci'].nunique()}")
        print(f"  - Antennas: {data['nb_antennas_rx'].iloc[0] if not data.empty else 'N/A'}")
    else:
        print("Telemetry Data: No data available")
    
    # Check configuration
    print(f"\nConfiguration:")
    print(f"  - Telemetry path: {rf_planner.config.telemetry_data_path}")
    print(f"  - Output path: {rf_planner.config.output_path}")
    print(f"  - ISAC enabled: {rf_planner.config.isac_enabled}")
    print(f"  - Motion threshold: {rf_planner.config.motion_detection_threshold_us} μs")
    
    # Check GPS files
    gps_dir = "ue_position"
    if os.path.exists(gps_dir):
        gps_files = [f for f in os.listdir(gps_dir) if f.endswith('.csv')]
        print(f"\nGPS Files:")
        print(f"  - Directory: {gps_dir}")
        print(f"  - Available files: {len(gps_files)}")
        for file in gps_files[:5]:  # Show first 5 files
            print(f"    - {file}")
        if len(gps_files) > 5:
            print(f"    - ... and {len(gps_files) - 5} more")
    else:
        print(f"\nGPS Files: Directory '{gps_dir}' not found")
    
    print("\nSystem ready for analysis!")

def run_motion_analysis(rf_planner: RFPlanner, args):
    """Analyze motion and radial velocity"""
    print("Running motion analysis...")
    
    # Check if GPS file exists
    if not os.path.exists(args.gps_file):
        print(f"Error: GPS file not found: {args.gps_file}")
        sys.exit(1)
    
    # Set output directory
    output_dir = args.output
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Run motion analysis
        result = rf_planner.run_motion_analysis(
            gps_file=args.gps_file,
            output_dir=output_dir
        )
        
        # Print results
        print("\nMotion analysis completed successfully!")
        print(f"Results saved to: {output_dir}")
        print(f"\nAnalysis Summary:")
        print(f"  - GPS file: {args.gps_file}")
        print(f"  - Total motion events: {result.get('total_motion_events', 0)}")
        print(f"  - Average radial velocity: {result.get('average_radial_velocity', 0.0):.2f} m/s")
        print(f"  - Max radial velocity: {result.get('max_radial_velocity', 0.0):.2f} m/s")
        print(f"  - Time range: {result.get('time_range', ['', ''])[0]} to {result.get('time_range', ['', ''])[1]}")
        
        # Radial velocity distribution
        if 'radial_velocity_distribution' in result:
            dist = result['radial_velocity_distribution']
            print(f"\nRadial Velocity Distribution:")
            print(f"  - High radial velocity (≥10 m/s): {dist.get('high_radial_velocity', 0)}")
            print(f"  - Medium radial velocity (5-10 m/s): {dist.get('medium_radial_velocity', 0)}")
            print(f"  - Low radial velocity (<5 m/s): {dist.get('low_radial_velocity', 0)}")
        
        # HTML visualization
        if 'html_visualization' in result:
            html_file = result['html_visualization']
            print(f"\nVisualization:")
            print(f"  - Motion Direction Map: {html_file}")
            print(f"  - UE Position Quality Heatmap: {html_file.replace('motion_direction_map.html', 'ue_position_quality_heatmap.html')}")
            print(f"  - Open in browser to view:")
            print(f"    * Motion direction (towards/away from gNB) - Dark mode")
            print(f"    * UE position quality assessment - Dark mode heatmap")
        
    except Exception as e:
        print(f"Error during motion analysis: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 