#!/usr/bin/env python3
"""Dev API server using stat-based reload (no inotify).

Use when Linux raises OSError: OS file watch limit reached with
uvicorn --reload (WatchFiles), e.g. when IDEs or other tools already
consume most of fs.inotify.max_user_watches.

From repo root:
  python scripts/run_api_dev_stat_reload.py

Alternative system fix (persistent):
  sudo sysctl fs.inotify.max_user_watches=524288
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    src = repo_root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

    from uvicorn.config import Config
    from uvicorn.server import Server
    from uvicorn.supervisors.statreload import StatReload

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--reload-dir",
        dest="reload_dirs",
        action="append",
        default=None,
        help="Directory to scan for .py changes (default: ./src). Repeat to add more.",
    )
    args = parser.parse_args()
    reload_dirs = args.reload_dirs if args.reload_dirs else [str(src)]

    config = Config(
        "agentic_rf_planner.api.rest:app",
        host=args.host,
        port=args.port,
        reload=True,
        reload_dirs=reload_dirs,
    )
    server = Server(config=config)
    sock = config.bind_socket()
    StatReload(config, target=server.run, sockets=[sock]).run()


if __name__ == "__main__":
    main()
