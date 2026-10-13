#!/usr/bin/env python
"""Serve the backtest dashboard on loopback.

    python serve_dashboard.py               # http://127.0.0.1:8765
    python serve_dashboard.py --port 9000 --open
    python serve_dashboard.py --results other_results
"""

from __future__ import annotations

import argparse
from pathlib import Path

from quant.dashboard.server import DEFAULT_PORT, DEFAULT_RESULTS_DIR, find_free_port, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="serve_dashboard.py",
        description="Browse and compare saved backtest runs in a local web UI.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (keep it on loopback)")
    parser.add_argument("--results", default=str(DEFAULT_RESULTS_DIR), help="directory of run JSON files")
    parser.add_argument("--open", action="store_true", help="open a browser window")
    parser.add_argument("--auto-port", action="store_true", help="pick the next free port if busy")
    args = parser.parse_args(argv)

    port = args.port
    if args.auto_port:
        port = find_free_port(args.port, args.host)

    httpd = serve(
        port=port,
        results_dir=Path(args.results),
        host=args.host,
        open_browser=args.open,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n dashboard stopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
