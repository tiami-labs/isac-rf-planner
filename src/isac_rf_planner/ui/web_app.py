"""Optional dev server wrapper for web app."""

# This file can be used to run the app during development
# For production, use: uvicorn isac_rf_planner.api.rest:app

from __future__ import annotations

import sys
from pathlib import Path

from uvicorn.config import Config
from uvicorn.server import Server
from uvicorn.supervisors.statreload import StatReload

# Package tree under src/ — limit reload scan to this tree.
_SRC_TREE = Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    _src = str(_SRC_TREE)
    if _src not in sys.path:
        sys.path.insert(0, _src)

    # StatReload polls *.py mtimes instead of inotify. Use this when
    # fs.inotify.max_user_watches is exhausted (e.g. IDE + many projects).
    config = Config(
        "isac_rf_planner.api.rest:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_dirs=[_src],
        timeout_keep_alive=300,
    )
    server = Server(config=config)
    sock = config.bind_socket()
    StatReload(config, target=server.run, sockets=[sock]).run()
