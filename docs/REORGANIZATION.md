# UE Planner Reorganization

The UE planner has been reorganized to match the structure of the main RF planning project.

## New Structure

```
src/agentic_ue_planner/
├── __init__.py                 # Package exports
├── cli.py                      # Command-line interface (was main.py)
├── config.py                   # Configuration settings
├── setup.py                    # Package setup
│
├── agents/                     # Main orchestrators
│   ├── __init__.py
│   └── ue_planning_agent.py   # Main RFPlanner class (was core.py)
│
├── data/                       # Data processing
│   ├── __init__.py
│   └── telemetry_processor.py # Telemetry data processing (was data_processor.py)
│
├── isac/                       # ISAC analysis
│   ├── __init__.py
│   ├── isac_analyzer.py       # Motion detection and positioning
│   └── velocity_analyzer.py   # Velocity estimation
│
├── placement/                  # UE placement and RF planning
│   ├── __init__.py
│   ├── placement_engine.py    # RF planning and placement recommendations
│   └── ue_location_assessor.py # UE location quality assessment
│
├── geo/                        # Geographic/GPS functionality
│   ├── __init__.py
│   └── gps_telemetry_matcher.py # GPS-telemetry matching
│
├── replay/                     # Replay analysis
│   ├── __init__.py
│   └── replay_analyzer.py     # GPS trajectory replay analysis
│
├── ui/                         # Visualization
│   ├── __init__.py
│   └── visualization.py        # Plotting and interactive maps
│
├── examples/                   # Example scripts
│   ├── __init__.py
│   ├── example.py
│   ├── gps_telemetry_example.py
│   ├── replay_integration_example.py
│   ├── ue_location_example.py
│   └── velocity_integration_example.py
│
└── tests/                      # Test files
    ├── __init__.py
    ├── test_system.py
    └── test_velocity_fix.py
```

## File Mapping

| Old Location | New Location |
|-------------|--------------|
| `core.py` | `agents/ue_planning_agent.py` |
| `data_processor.py` | `data/telemetry_processor.py` |
| `isac_analyzer.py` | `isac/isac_analyzer.py` |
| `velocity_analyzer.py` | `isac/velocity_analyzer.py` |
| `placement_engine.py` | `placement/placement_engine.py` |
| `ue_location_assessor.py` | `placement/ue_location_assessor.py` |
| `gps_telemetry_matcher.py` | `geo/gps_telemetry_matcher.py` |
| `replay_analyzer.py` | `replay/replay_analyzer.py` |
| `visualization.py` | `ui/visualization.py` |
| `main.py` | `cli.py` |
| `config.py` | `config.py` (unchanged) |
| `*_example.py` | `examples/*.py` |
| `test_*.py` | `tests/test_*.py` |

## Import Changes

All relative imports have been updated to use the new structure:

- `from .config import ...` → `from ..config import ...` (in subdirectories)
- `from .data_processor import ...` → `from ..data.telemetry_processor import ...`
- `from .isac_analyzer import ...` → `from ..isac.isac_analyzer import ...`
- etc.

## Usage

The package can now be imported as:

```python
from agentic_ue_planner import RFPlanner, RFPlannerConfig
from agentic_ue_planner.data import TelemetryProcessor
from agentic_ue_planner.isac import ISACAnalyzer, VelocityAnalyzer
from agentic_ue_planner.placement import PlacementEngine
# etc.
```

Or use the CLI:

```bash
python -m agentic_ue_planner.cli analyze --output ./results
```

