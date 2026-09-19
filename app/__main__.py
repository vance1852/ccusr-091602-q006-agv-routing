"""服务入口：python -m app [--host H] [--port P] [--demo]"""

from __future__ import annotations

import argparse

from .service import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="AGV 路权协调服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="加载三车窄巷演示场景")
    parser.add_argument("--heartbeat-ttl", type=float, default=30.0)
    args = parser.parse_args()
    run_server(host=args.host, port=args.port, demo=args.demo,
               heartbeat_ttl=args.heartbeat_ttl)


if __name__ == "__main__":
    main()
