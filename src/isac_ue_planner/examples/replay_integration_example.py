#!/usr/bin/env python3
"""
Replay Analysis Integration Example
Demonstrates how to use the integrated replay analysis functionality in RF Planner
"""

import os
import sys
from datetime import datetime

from rfplanner import RFPlanner, RFPlannerConfig

def main():
    """Main example function"""
    print("RF Planner - Replay Analysis Integration Example")
    print("=" * 60)
    
    # Initialize RF Planner
    print("Initializing RF Planner...")
    rf_planner = RFPlanner()
    
    # Check if GPS file exists
    gps_file = "ue_position/00:33:50-01:14:34.csv"
    if not os.path.exists(gps_file):
        print(f"Error: GPS file not found: {gps_file}")
        print("Please ensure the GPS trajectory file exists in the ue_position directory")
        return
    
    print(f"GPS file found: {gps_file}")
    
    # Set output directory
    output_dir = "./replay_integration_results"
    os.makedirs(output_dir, exist_ok=True)
    
    try:
        # Run replay analysis
        print("\nRunning replay analysis...")
        result = rf_planner.run_replay_analysis(
            gps_file=gps_file,
            output_dir=output_dir
        )
        
        # Print comprehensive results
        print("\n" + "=" * 60)
        print("REPLAY ANALYSIS RESULTS")
        print("=" * 60)
        
        print(f"\n📊 Analysis Summary:")
        print(f"  • GPS File: {result.gps_file}")
        print(f"  • Telemetry Files: {len(result.telemetry_files)}")
        print(f"  • Time Range: {result.time_range[0]} to {result.time_range[1]}")
        print(f"  • Total Matches: {result.total_matches}")
        print(f"  • Best UE Positions: {len(result.best_ue_positions)}")
        
        # Quality distribution
        if result.quality_distribution:
            dist = result.quality_distribution
            print(f"\n📈 Quality Distribution:")
            print(f"  • Excellent (≥0.8): {dist.get('excellent_positions', 0)}")
            print(f"  • Good (0.6-0.8): {dist.get('good_positions', 0)}")
            print(f"  • Fair (0.4-0.6): {dist.get('fair_positions', 0)}")
            print(f"  • Poor (<0.4): {dist.get('poor_positions', 0)}")
            print(f"  • Mean Quality Score: {dist.get('mean_score', 0):.3f}")
        
        # gNB estimation results
        if result.estimated_gnb_position:
            gnb_lat, gnb_lon, confidence = result.estimated_gnb_position
            print(f"\n📍 gNB Position Estimation:")
            print(f"  • Estimated Position: ({gnb_lat:.6f}, {gnb_lon:.6f})")
            print(f"  • Confidence: {confidence:.3f}")
            print(f"  • Triangulation Quality: {result.triangulation_quality:.3f}")
            print(f"  • Positions Used: {len(result.best_ue_positions)}")
        
        # Top 5 best UE positions
        if result.best_ue_positions:
            print(f"\n🏆 Top 5 Best UE Positions for gNB Estimation:")
            print("-" * 80)
            for i, pos in enumerate(result.best_ue_positions[:5], 1):
                print(f"\n{i}. Quality Score: {pos.overall_quality_score:.3f}")
                print(f"   Position: ({pos.gps_lat:.6f}, {pos.gps_lon:.6f})")
                print(f"   Altitude: {pos.gps_altitude:.1f} m")
                print(f"   GPS Accuracy: {pos.gps_horizontal_accuracy:.1f} m")
                print(f"   Signal Strength: {max(pos.signal_strength_db):.1f} dBm")
                print(f"   Timing Offset: {pos.timing_offset_us} μs")
                print(f"   Estimated Distance: {pos.estimated_distance_m:.0f} m")
                print(f"   Quality Breakdown:")
                print(f"     - Position: {pos.position_quality_score:.3f}")
                print(f"     - Signal: {pos.signal_quality_score:.3f}")
                print(f"     - Timing: {pos.timing_quality_score:.3f}")
                print(f"     - Motion: {pos.motion_quality_score:.3f}")
        
        # Analysis statistics
        if result.analysis_stats:
            stats = result.analysis_stats
            print(f"\n📋 Detailed Statistics:")
            
            if 'gps_stats' in stats:
                gps_stats = stats['gps_stats']
                print(f"  GPS Data:")
                print(f"    • Total measurements: {gps_stats.get('total_gps_measurements', 0)}")
                print(f"    • Time range: {gps_stats.get('gps_time_range_hours', 0):.1f} hours")
                print(f"    • Avg accuracy: {gps_stats.get('avg_gps_accuracy_m', 0):.1f} m")
                print(f"    • Avg velocity: {gps_stats.get('avg_velocity_mps', 0):.2f} m/s")
            
            if 'telemetry_stats' in stats:
                telemetry_stats = stats['telemetry_stats']
                print(f"  Telemetry Data:")
                print(f"    • Total measurements: {telemetry_stats.get('total_telemetry_measurements', 0)}")
                print(f"    • Unique PCIs: {telemetry_stats.get('unique_pcis', 0)}")
                print(f"    • Avg signal strength: {telemetry_stats.get('avg_signal_strength_db', 0):.1f} dBm")
                print(f"    • Avg timing offset: {telemetry_stats.get('avg_timing_offset_us', 0):.1f} μs")
            
            if 'matching_stats' in stats:
                matching_stats = stats['matching_stats']
                print(f"  Matching Results:")
                print(f"    • Match rate: {matching_stats.get('match_rate_percent', 0):.1f}%")
                print(f"    • Avg quality score: {matching_stats.get('avg_quality_score', 0):.3f}")
                print(f"    • Excellent positions: {matching_stats.get('excellent_positions', 0)}")
                print(f"    • Good positions: {matching_stats.get('good_positions', 0)}")
        
        # Recommendations
        recommendations = rf_planner.replay_analyzer.get_replay_recommendations(result)
        if recommendations:
            print(f"\n💡 Recommendations ({len(recommendations)}):")
            print("-" * 50)
            for i, rec in enumerate(recommendations, 1):
                print(f"  {i}. {rec}")
        
        # Output files
        print(f"\n📁 Output Files:")
        print(f"  • Results directory: {output_dir}")
        print(f"  • Matched measurements: {output_dir}/matched_measurements.csv")
        print(f"  • Best UE positions: {output_dir}/best_ue_positions.csv")
        if result.estimated_gnb_position:
            print(f"  • gNB estimation: {output_dir}/gnb_estimation_results.json")
        print(f"  • Analysis summary: {output_dir}/replay_analysis_summary.json")
        
        print(f"\n✅ Replay analysis completed successfully!")
        print(f"\n🎯 Key Insights:")
        print(f"  • Higher quality scores indicate better UE positions for gNB estimation")
        print(f"  • GPS accuracy and low velocity improve positioning quality")
        print(f"  • Strong signal strength and stable timing are crucial")
        print(f"  • Motion reduces positioning quality for gNB estimation")
        print(f"  • Best positions can be used for triangulation or gNB location estimation")
        
    except Exception as e:
        print(f"❌ Error during replay analysis: {e}")
        return False
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 