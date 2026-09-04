#!/usr/bin/env python3
"""
RF Planner System Test Script
Tests the core components of the RF Planner system
"""

import sys
import os
import json
import tempfile
import shutil
from datetime import datetime, timedelta

# Add the current directory to the path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_imports():
    """Test that all modules can be imported"""
    print("Testing imports...")
    
    try:
        from config import RFPlannerConfig, DEFAULT_CONFIG
        print("✓ Config module imported successfully")
        
        from data_processor import TelemetryProcessor
        print("✓ Data processor module imported successfully")
        
        from isac_analyzer import ISACAnalyzer
        print("✓ ISAC analyzer module imported successfully")
        
        from placement_engine import PlacementEngine
        print("✓ Placement engine module imported successfully")
        
        from visualization import RFVisualizer
        print("✓ Visualization module imported successfully")
        
        from core import RFPlanner
        print("✓ Core module imported successfully")
        
        return True
        
    except ImportError as e:
        print(f"✗ Import error: {e}")
        return False

def test_config():
    """Test configuration system"""
    print("\nTesting configuration system...")
    
    try:
        from config import RFPlannerConfig
        
        # Test default configuration
        config = RFPlannerConfig()
        print(f"✓ Default config created: telemetry_path={config.telemetry_data_path}")
        
        # Test custom configuration
        custom_config = RFPlannerConfig(
            telemetry_data_path="./test_data",
            output_path="./test_output",
            isac_enabled=True
        )
        print(f"✓ Custom config created: isac_enabled={custom_config.isac_enabled}")
        
        # Test save/load
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            config_path = f.name
        
        try:
            custom_config.save_to_file(config_path)
            loaded_config = RFPlannerConfig.from_file(config_path)
            print(f"✓ Config save/load test passed")
        finally:
            os.unlink(config_path)
        
        return True
        
    except Exception as e:
        print(f"✗ Configuration test failed: {e}")
        return False

def test_data_processor():
    """Test data processor with sample data"""
    print("\nTesting data processor...")
    
    try:
        from data_processor import TelemetryProcessor
        from config import RFPlannerConfig
        
        # Create sample data
        sample_data = [
            {
                "timestamp_ms": int(datetime.now().timestamp() * 1000),
                "latitude": 40.7128,
                "longitude": -74.0060,
                "altitude": 0.0,
                "pci": 123,
                "ssb_index": 0,
                "channel_level_db": [-75.2, -78.1],
                "mrc_weights": [0.6, 0.4],
                "antenna_quality": [0.8, 0.7],
                "llr_energy": 45.2,
                "freq_offset_hz": 1250.5,
                "current_timing_offset_us": 15,
                "initial_timing_offset_us": 10,
                "motion_magnitude": 5.0,
                "iso_timestamp": datetime.now().isoformat() + "Z"
            }
        ]
        
        # Create temporary test file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(sample_data, f)
            test_file = f.name
        
        try:
            # Create test directory structure
            test_dir = tempfile.mkdtemp()
            test_data_dir = os.path.join(test_dir, "test_data")
            os.makedirs(test_data_dir, exist_ok=True)
            
            # Copy test file to test data directory
            shutil.copy(test_file, os.path.join(test_data_dir, "pbch_telemetry_test.json"))
            
            # Test data processor
            config = RFPlannerConfig(telemetry_data_path=test_data_dir)
            processor = TelemetryProcessor(config)
            
            # Load data
            data = processor.load_telemetry_data()
            print(f"✓ Data processor loaded {len(data)} records")
            
            # Test data processing functions
            motion_data = processor.get_motion_data()
            quality_data = processor.get_signal_quality_data()
            stats = processor.get_statistics()
            
            print(f"✓ Motion data: {len(motion_data)} records")
            print(f"✓ Quality data: {len(quality_data)} records")
            print(f"✓ Statistics: {stats.get('total_records', 0)} total records")
            
        finally:
            # Cleanup
            os.unlink(test_file)
            shutil.rmtree(test_dir, ignore_errors=True)
        
        return True
        
    except Exception as e:
        print(f"✗ Data processor test failed: {e}")
        return False

def test_isac_analyzer():
    """Test ISAC analyzer"""
    print("\nTesting ISAC analyzer...")
    
    try:
        from isac_analyzer import ISACAnalyzer
        from config import RFPlannerConfig
        import pandas as pd
        
        # Create sample data
        sample_data = []
        base_time = datetime.now()
        
        for i in range(10):
            record = {
                "timestamp": base_time + timedelta(minutes=i),
                "latitude": 40.7128 + i * 0.0001,
                "longitude": -74.0060 + i * 0.0001,
                "altitude": 0.0,
                "pci": 123,
                "current_timing_offset_us": 10 + i * 2,
                "initial_timing_offset_us": 10,
                "channel_level_db": [-75.0 + i, -78.0 + i],
                "llr_energy": 45.0 + i,
                "motion_magnitude": 5.0 + i * 0.5
            }
            sample_data.append(record)
        
        df = pd.DataFrame(sample_data)
        
        # Test ISAC analyzer
        config = RFPlannerConfig()
        analyzer = ISACAnalyzer(config)
        
        # Test motion analysis
        motion_events = analyzer.analyze_motion(df)
        print(f"✓ Motion analysis: {len(motion_events)} events detected")
        
        # Test position estimation
        position_estimates = analyzer.estimate_position(df)
        print(f"✓ Position estimation: {len(position_estimates)} estimates")
        
        # Test trajectory analysis
        trajectory = analyzer.analyze_trajectory(df)
        print(f"✓ Trajectory analysis completed")
        
        # Test motion summary
        summary = analyzer.get_motion_summary()
        print(f"✓ Motion summary: {summary.get('total_events', 0)} total events")
        
        return True
        
    except Exception as e:
        print(f"✗ ISAC analyzer test failed: {e}")
        return False

def test_placement_engine():
    """Test placement engine"""
    print("\nTesting placement engine...")
    
    try:
        from placement_engine import PlacementEngine
        from config import RFPlannerConfig
        import pandas as pd
        
        # Create sample data
        sample_data = []
        for i in range(10):
            record = {
                "timestamp": datetime.now() + timedelta(minutes=i),
                "latitude": 40.7128 + i * 0.001,
                "longitude": -74.0060 + i * 0.001,
                "altitude": 0.0,
                "pci": 123 + (i % 3),
                "channel_level_db": [-75.0 + i, -78.0 + i],
                "llr_energy": 45.0 + i
            }
            sample_data.append(record)
        
        df = pd.DataFrame(sample_data)
        
        # Test placement engine
        config = RFPlannerConfig()
        engine = PlacementEngine(config)
        
        # Add cell sites
        for pci in df['pci'].unique():
            cell_data = df[df['pci'] == pci]
            engine.add_cell_site(
                pci=pci,
                lat=cell_data['latitude'].mean(),
                lon=cell_data['longitude'].mean()
            )
        
        print(f"✓ Added {len(engine.cell_sites)} cell sites")
        
        # Test coverage analysis
        coverage_areas = engine.analyze_coverage(df)
        print(f"✓ Coverage analysis: {len(coverage_areas)} areas")
        
        # Test placement recommendations
        recommendations = engine.recommend_placement(df)
        print(f"✓ Placement recommendations: {len(recommendations)} recommendations")
        
        # Test network optimization
        optimization = engine.optimize_network_planning(df)
        print(f"✓ Network optimization completed")
        
        return True
        
    except Exception as e:
        print(f"✗ Placement engine test failed: {e}")
        return False

def test_visualization():
    """Test visualization module"""
    print("\nTesting visualization module...")
    
    try:
        from visualization import RFVisualizer
        from config import RFPlannerConfig
        import pandas as pd
        
        # Create sample data
        sample_data = []
        base_time = datetime.now()
        
        for i in range(20):
            record = {
                "timestamp": base_time + timedelta(minutes=i),
                "latitude": 40.7128 + i * 0.0001,
                "longitude": -74.0060 + i * 0.0001,
                "altitude": 0.0,
                "pci": 123 + (i % 3),
                "channel_level_db": [-75.0 + i, -78.0 + i],
                "llr_energy": 45.0 + i,
                "motion_magnitude": 5.0 + i * 0.5,
                "freq_offset_hz": 1250.5 + i
            }
            sample_data.append(record)
        
        df = pd.DataFrame(sample_data)
        
        # Test visualizer
        config = RFPlannerConfig()
        visualizer = RFVisualizer(config)
        
        # Test overview plot
        overview_fig = visualizer.plot_telemetry_overview(df)
        print(f"✓ Overview plot created: {overview_fig is not None}")
        
        # Test coverage map
        coverage_map = visualizer.plot_coverage_map(df)
        print(f"✓ Coverage map created: {coverage_map is not None}")
        
        # Test motion analysis plot
        motion_data = df.copy()
        motion_data['motion_magnitude'] = [5.0 + i * 0.5 for i in range(len(df))]
        motion_fig = visualizer.plot_motion_analysis(motion_data)
        print(f"✓ Motion analysis plot created: {motion_fig is not None}")
        
        # Test interactive dashboard
        dashboard_fig = visualizer.create_interactive_dashboard(df)
        print(f"✓ Interactive dashboard created: {dashboard_fig is not None}")
        
        return True
        
    except Exception as e:
        print(f"✗ Visualization test failed: {e}")
        return False

def test_core_system():
    """Test the complete core system"""
    print("\nTesting complete core system...")
    
    try:
        from core import RFPlanner
        from config import RFPlannerConfig
        import pandas as pd
        
        # Create comprehensive sample data
        sample_data = []
        base_time = datetime.now()
        
        for i in range(50):
            record = {
                "timestamp": base_time + timedelta(minutes=i),
                "latitude": 40.7128 + i * 0.0001,
                "longitude": -74.0060 + i * 0.0001,
                "altitude": 0.0,
                "pci": 123 + (i % 3),
                "ssb_index": i % 8,
                "channel_level_db": [-75.0 + i, -78.0 + i],
                "mrc_weights": [0.6, 0.4],
                "antenna_quality": [0.8, 0.7],
                "llr_energy": 45.0 + i,
                "freq_offset_hz": 1250.5 + i,
                "current_timing_offset_us": 10 + i * 2,
                "initial_timing_offset_us": 10,
                "motion_magnitude": 5.0 + i * 0.5,
                "iso_timestamp": (base_time + timedelta(minutes=i)).isoformat() + "Z"
            }
            sample_data.append(record)
        
        df = pd.DataFrame(sample_data)
        
        # Create temporary test environment
        test_dir = tempfile.mkdtemp()
        test_data_dir = os.path.join(test_dir, "test_data")
        os.makedirs(test_data_dir, exist_ok=True)
        
        try:
            # Save sample data
            with open(os.path.join(test_data_dir, "pbch_telemetry_test.json"), 'w') as f:
                json.dump(sample_data, f)
            
            # Test RF Planner
            config = RFPlannerConfig(
                telemetry_data_path=test_data_dir,
                output_path=test_dir
            )
            rf_planner = RFPlanner(config)
            
            # Test data loading
            success = rf_planner.load_data()
            print(f"✓ Data loading: {success}")
            
            if success:
                # Test ISAC analysis
                isac_results = rf_planner.analyze_isac_data()
                print(f"✓ ISAC analysis: {len(isac_results.get('motion_events', []))} motion events")
                
                # Test RF planning
                planning_results = rf_planner.perform_rf_planning()
                print(f"✓ RF planning: {len(planning_results.get('placement_recommendations', []))} recommendations")
                
                # Test recommendations
                recommendations = rf_planner.get_recommendations()
                print(f"✓ Recommendations: {len(recommendations)} generated")
                
                # Test export
                export_files = rf_planner.export_results()
                print(f"✓ Export: {len(export_files)} files exported")
            
        finally:
            # Cleanup
            shutil.rmtree(test_dir, ignore_errors=True)
        
        return True
        
    except Exception as e:
        print(f"✗ Core system test failed: {e}")
        return False

def main():
    """Run all tests"""
    print("RF Planner System Test Suite")
    print("="*40)
    
    tests = [
        ("Import Test", test_imports),
        ("Configuration Test", test_config),
        ("Data Processor Test", test_data_processor),
        ("ISAC Analyzer Test", test_isac_analyzer),
        ("Placement Engine Test", test_placement_engine),
        ("Visualization Test", test_visualization),
        ("Core System Test", test_core_system)
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        print(f"\n{'='*20}")
        print(f"Running {test_name}")
        print(f"{'='*20}")
        
        try:
            if test_func():
                print(f"✓ {test_name} PASSED")
                passed += 1
            else:
                print(f"✗ {test_name} FAILED")
        except Exception as e:
            print(f"✗ {test_name} FAILED with exception: {e}")
    
    print(f"\n{'='*40}")
    print(f"TEST SUMMARY")
    print(f"{'='*40}")
    print(f"Passed: {passed}/{total}")
    print(f"Failed: {total - passed}/{total}")
    
    if passed == total:
        print("🎉 All tests passed! The RF Planner system is working correctly.")
        return 0
    else:
        print("❌ Some tests failed. Please check the errors above.")
        return 1

if __name__ == '__main__':
    sys.exit(main()) 