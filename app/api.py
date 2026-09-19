"""路权协调服务的 HTTP API（仅依赖标准库，可独立运行）。

启动示例（预置三车等待环演示数据）：

    python -m app.api --demo --port 8080

值守员界面常用查询：

    GET  /api/status                 总览（等待图、车辆、开放告警）
    GET  /api/wait-graph             等待图
    GET  /api/timeline               预约时间轴（?resource=A2 过滤）
    GET  /api/denials                被拒绝/排队原因
    GET  /api/incidents              告警与建议动作
    POST /api/incidents/<id>/apply   执行建议动作
    GET  /api/vehicles / /api/plans/V1
    POST /api/tick                   推进超时/心跳检测
    POST /api/safe-stop              {"vehicle_id": "V1"} 或全场急停
    POST /api/manual-takeover        {"vehicle_id": "V2"}
    POST /api/segments/close         {"resource_id": "C2"}
    POST /api/heartbeat              {"vehicle_id": "V1", "position": "A2"}
    POST /api/position-confirm       {"vehicle_id": "V1", "resource_id": "A2", "occupied": false}
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import scenarios


class ApiHolder:
    def __init__(self) -> None:
        self.svc, self.clock = scenarios.build_service()
        self.demo = False
        self.lock = threading.RLock()


HOLDER = ApiHolder()


class Handler(BaseHTTPRequestHandler):
    server_version = "RoW/1.0"

    def log_message(self, fmt, *args):  # noqa: N802 - 安静一点
        return

    # ---- 工具 ---------------------------------------------------------------

    def _json(self, obj, status: int = 200):
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _error(self, status: int, code: str, detail: str):
        self._json({"error": code, "detail": detail}, status)

    # ---- 路由 ---------------------------------------------------------------

    def do_GET(self):  # noqa: N802
        with HOLDER.lock:
            try:
                parsed = urlparse(self.path)
                path, qs = parsed.path, parse_qs(parsed.query)
                svc = HOLDER.svc
                if path == "/api/status":
                    self._json({
                        "ts": svc.now,
                        "demo": HOLDER.demo,
                        "wait_graph": svc.wait_graph(),
                        "vehicles": svc.vehicles_view(),
                        "open_incidents": [i.snapshot() for i in svc.incidents
                                           if i.status == "open"],
                        "integrity_violations": svc.integrity_violations(),
                    })
                elif path == "/api/wait-graph":
                    self._json(svc.wait_graph())
                elif path == "/api/timeline":
                    self._json(svc.timeline(qs.get("resource", [None])[0]))
                elif path == "/api/denials":
                    self._json(svc.denials_view())
                elif path == "/api/incidents":
                    self._json(svc.incidents_view())
                elif path.startswith("/api/incidents/"):
                    inc_id = path.rsplit("/", 1)[-1]
                    inc = next((i for i in svc.incidents
                                if i.incident_id == inc_id), None)
                    if inc:
                        self._json(inc.snapshot())
                    else:
                        self._error(404, "not_found", f"无告警 {inc_id}")
                elif path == "/api/vehicles":
                    self._json(svc.vehicles_view())
                elif path.startswith("/api/plans/"):
                    vid = path.rsplit("/", 1)[-1]
                    if vid not in svc.fleet:
                        self._error(404, "not_found", f"无车辆 {vid}")
                    else:
                        self._json({"vehicle_id": vid,
                                    "plans": svc.plan_history(vid)})
                elif path == "/api/events":
                    self._json([{"seq": e.seq, "ts": e.ts, "type": e.type,
                                 "payload": e.payload}
                                for e in svc.events.all()])
                elif path == "/health":
                    self._json({"ok": True})
                else:
                    self._error(404, "not_found", path)
            except Exception as exc:  # noqa: BLE001
                self._error(500, "internal", f"{type(exc).__name__}: {exc}")

    def do_POST(self):  # noqa: N802
        with HOLDER.lock:
            try:
                path = urlparse(self.path).path
                svc = HOLDER.svc
                body = self._body()

                if path == "/api/tick":
                    dt = float(body.get("dt", 1.0))
                    HOLDER.clock.advance(dt)
                    self._json(svc.tick())
                elif path == "/api/safe-stop":
                    self._json(svc.safe_stop(body.get("vehicle_id"),
                                             body.get("reason", "API 安全急停")))
                elif path == "/api/manual-takeover":
                    self._json(svc.manual_takeover(
                        body["vehicle_id"], body.get("reason", "API 人工接管")))
                elif path == "/api/resume":
                    self._json(svc.resume_automatic(body["vehicle_id"]))
                elif path == "/api/segments/close":
                    self._json(svc.close_segment(
                        body["resource_id"], body.get("reason", "API 封闭")))
                elif path == "/api/heartbeat":
                    self._json(svc.heartbeat(body["vehicle_id"],
                                             body["position"]))
                elif path == "/api/position-confirm":
                    self._json(svc.confirm_position(
                        body["vehicle_id"], body["resource_id"],
                        bool(body["occupied"])))
                elif path == "/api/leases/request":
                    self._json(svc.request_resource(
                        body["vehicle_id"], body["resource_id"],
                        request_id=body.get("request_id"),
                        duration=body.get("duration")))
                elif path == "/api/leases/enter":
                    self._json(svc.enter_resource(body["vehicle_id"],
                                                  body["lease_id"]))
                elif path == "/api/leases/release":
                    self._json(svc.release_lease(body["vehicle_id"],
                                                 body["lease_id"]))
                elif path.startswith("/api/incidents/") and path.endswith("/apply"):
                    inc_id = path.split("/")[3]
                    self._json(svc.apply_resolution(inc_id, body.get("action")))
                elif path == "/api/demo/reset":
                    HOLDER.svc, HOLDER.clock = scenarios.build_service()
                    scenarios.seed_three_way_cycle(HOLDER.svc, HOLDER.clock)
                    HOLDER.demo = True
                    self._json({"reset": True, "demo": "three_way_cycle",
                                "status": HOLDER.svc.wait_graph()})
                else:
                    self._error(404, "not_found", path)
            except KeyError as exc:
                self._error(400, "bad_request", f"缺少字段 {exc}")
            except Exception as exc:  # noqa: BLE001
                self._error(400, "bad_request", f"{type(exc).__name__}: {exc}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AGV 路权协调 API 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true",
                        help="预置三台 AGV 等待环场景")
    args = parser.parse_args(argv)

    if args.demo:
        scenarios.seed_three_way_cycle(HOLDER.svc, HOLDER.clock)
        HOLDER.demo = True

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"路权协调服务监听 http://{args.host}:{args.port}"
          f"（demo={args.demo}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
