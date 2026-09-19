"""HTTP 服务：车辆侧接口 + 值守员告警界面查询接口。

仅用标准库实现，可独立运行：
    python -m app --port 8080 [--demo]

所有写操作先过 tick() 推进超时/死锁派生逻辑，再处理请求；
协调器非线程安全，这里用一把锁串行化。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .clock import SystemClock
from .coordinator import RightOfWayCoordinator, replay
from .topology import Topology


class Service:
    """协调器的 HTTP 封装，便于测试时替换时钟/拓扑。"""

    def __init__(self, coordinator: RightOfWayCoordinator):
        self.coordinator = coordinator
        self.lock = threading.RLock()

    # -- 请求分发：返回 (status, body) --
    def handle(self, method: str, path: str, query: dict, body: dict | None):
        c = self.coordinator
        with self.lock:
            c.tick()  # 惰性推进超时与死锁检测
            try:
                return self._route(method, path, query, body or {})
            except KeyError as e:
                return 400, {"error": f"缺少字段: {e}"}
            except (ValueError, TypeError) as e:
                return 400, {"error": str(e)}

    def _route(self, method, path, query, body):
        c = self.coordinator
        parts = [p for p in path.split("/") if p]

        if method == "GET" and parts == ["health"]:
            return 200, {"ok": True}

        # ---- 拓扑 ----
        if method == "POST" and parts == ["topology", "segments"]:
            seg = c.add_segment(body["segment_id"], body["start_node"],
                                body["end_node"], body.get("length", 1.0),
                                body.get("capacity", 1))
            return 200, {"ok": True, "segment_id": seg.resource_id}
        if method == "POST" and parts == ["topology", "intersections"]:
            res = c.add_intersection(body["resource_id"], body.get("capacity", 1))
            return 200, {"ok": True, "resource_id": res.resource_id}
        if method == "POST" and parts == ["topology", "fire-zones"]:
            zone = c.add_fire_zone(body["zone_id"], body["segments"], body["exits"])
            return 200, {"ok": True, "zone_id": zone.zone_id}
        if method == "POST" and len(parts) == 4 and parts[:2] == ["topology", "segments"]:
            if parts[3] == "close":
                return 200, c.close_segment(parts[2])
            if parts[3] == "open":
                return 200, c.open_segment(parts[2])

        # ---- 车辆 ----
        if method == "POST" and parts == ["vehicles"]:
            v = c.register_vehicle(
                body["vehicle_id"], can_reverse=body.get("can_reverse", True),
                hazard_level=body.get("hazard_level", 0),
                battery=body.get("battery", 1.0),
                position=body.get("position"), node=body.get("node"),
            )
            return 200, {"ok": True, "vehicle": v.to_dict()}
        if len(parts) >= 2 and parts[0] == "vehicles":
            vid, sub = parts[1], parts[2:]
            if method == "POST" and sub == ["task"]:
                task = c.assign_task(vid, body["task_id"], body["goal_node"],
                                     route=body.get("route"),
                                     deadline=body.get("deadline"))
                if task is None:
                    return 400, {"error": "车辆不存在或无可达路径"}
                return 200, {"ok": True, "task": task.to_dict()}
            if method == "POST" and sub == ["heartbeat"]:
                return 200, c.heartbeat(vid)
            if method == "POST" and sub == ["position"]:
                return 200, c.confirm_position(vid, segment_id=body.get("segment"),
                                               node_id=body.get("node"))
            if method == "POST" and sub == ["control"]:
                return 200, c.set_control_mode(vid, body["mode"])
            if method == "POST" and sub == ["fault"]:
                return 200, c.vehicle_fault(vid)

        # ---- 预约与租约 ----
        if method == "POST" and parts == ["reservations"]:
            result = c.request_reservation(
                body["request_id"], body["vehicle_id"], body["resource_id"],
                float(body["start"]), float(body["end"]),
            )
            return (200 if result["ok"] else 409), result
        if method == "POST" and len(parts) == 3 and parts[0] == "leases":
            if parts[2] == "enter":
                r = c.enter(parts[1], body["vehicle_id"])
                return (200 if r["ok"] else 409), r
            if parts[2] == "release":
                r = c.release(parts[1], body["vehicle_id"])
                return (200 if r["ok"] else 409), r

        # ---- 值守员视图 ----
        if method == "GET" and parts == ["operator", "wait-graph"]:
            return 200, c.wait_graph()
        if method == "GET" and parts == ["operator", "timeline"]:
            rid = query.get("resource_id", [None])[0]
            return 200, {"timeline": c.timeline(rid)}
        if method == "GET" and parts == ["operator", "rejections"]:
            return 200, {"rejections": [r.to_dict() for r in c.rejections]}
        if method == "GET" and parts == ["operator", "suggestions"]:
            return 200, {"suggestions": c.suggestions()}
        if method == "GET" and parts == ["operator", "leases"]:
            return 200, {"leases": [l.to_dict() for l in c.leases.values()]}
        if method == "GET" and parts == ["operator", "vehicles"]:
            return 200, {"vehicles": [v.to_dict() for v in c.vehicles.values()]}
        if method == "GET" and parts == ["operator", "events"]:
            return 200, {"events": c.events.to_list()}
        if method == "GET" and parts == ["operator", "invariants"]:
            return 200, {"violations": c.check_invariants()}
        if method == "POST" and parts == ["operator", "replay"]:
            return 200, replay(c.events.to_list(), heartbeat_ttl=c.heartbeat_ttl)

        return 404, {"error": f"未知接口: {method} {path}"}


def make_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        def _json_response(self, status, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
            status, resp = service.handle(method, parsed.path, query, body)
            self._json_response(status, resp)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, fmt, *args):  # 静默访问日志
            pass

    return Handler


def run_server(host: str = "127.0.0.1", port: int = 8080, demo: bool = False,
               heartbeat_ttl: float = 30.0) -> None:
    coordinator = RightOfWayCoordinator(Topology(), SystemClock(),
                                        heartbeat_ttl=heartbeat_ttl)
    if demo:
        from .demo import build_scenario
        build_scenario(coordinator)
    service = Service(coordinator)
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"路权协调服务已启动: http://{host}:{port}"
          + ("（已加载演示场景）" if demo else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
