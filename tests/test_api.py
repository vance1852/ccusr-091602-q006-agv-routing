"""HTTP API 端到端：车辆侧写入 + 值守员查询 + 回放。"""

import json
import unittest
from http.client import HTTPConnection
from threading import Thread

from app.clock import ManualClock
from app.coordinator import RightOfWayCoordinator
from app.service import Service, make_handler
from app.topology import Topology

from http.server import ThreadingHTTPServer


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        coord = RightOfWayCoordinator(Topology(), ManualClock(0.0),
                                      heartbeat_ttl=30.0)
        cls.service = Service(coord)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.port = cls.server.server_address[1]
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body) if body is not None else None
        conn.request(method, path, body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data

    def test_full_flow_and_operator_views(self):
        # 拓扑与车辆
        self.assertEqual(self.call("GET", "/health")[1]["ok"], True)
        self.call("POST", "/topology/segments",
                  {"segment_id": "S1", "start_node": "N0", "end_node": "N1"})
        self.call("POST", "/topology/segments",
                  {"segment_id": "S2", "start_node": "N1", "end_node": "N2"})
        self.call("POST", "/vehicles", {"vehicle_id": "A", "node": "N0"})
        self.call("POST", "/vehicles", {"vehicle_id": "B", "node": "N0"})
        self.call("POST", "/vehicles/A/task",
                  {"task_id": "T1", "goal_node": "N2", "route": ["S1", "S2"]})

        # A 获准并进入 S1；B 申请同窗口只能等待
        st, r = self.call("POST", "/reservations",
                          {"request_id": "r1", "vehicle_id": "A",
                           "resource_id": "S1", "start": 0, "end": 100})
        self.assertEqual(st, 200)
        lease_a = r["lease"]["lease_id"]
        st, r = self.call("POST", "/reservations",
                          {"request_id": "r2", "vehicle_id": "B",
                           "resource_id": "S1", "start": 0, "end": 100})
        self.assertEqual(r["lease"]["state"], "requested")
        # 幂等重试
        st, retry = self.call("POST", "/reservations",
                              {"request_id": "r1", "vehicle_id": "A",
                               "resource_id": "S1", "start": 0, "end": 100})
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(retry["lease"]["lease_id"], lease_a)

        self.call("POST", f"/leases/{lease_a}/enter", {"vehicle_id": "A"})

        # 值守员视图
        st, graph = self.call("GET", "/operator/wait-graph")
        self.assertEqual(graph["edges"][0]["from"], "B")
        st, timeline = self.call("GET", "/operator/timeline?resource_id=S1")
        self.assertEqual(len(timeline["timeline"]["S1"]), 2)
        st, leases = self.call("GET", "/operator/leases")
        self.assertEqual(len(leases["leases"]), 2)

        # 非法申请产生被拒绝原因
        st, r = self.call("POST", "/reservations",
                          {"request_id": "r3", "vehicle_id": "A",
                           "resource_id": "S1", "start": 50, "end": 50})
        self.assertEqual(st, 409)
        st, rejections = self.call("GET", "/operator/rejections")
        self.assertEqual(rejections["rejections"][0]["reason"], "invalid_window")

        # 建议动作与回放
        st, suggestions = self.call("GET", "/operator/suggestions")
        self.assertIn("suggestions", suggestions)
        st, result = self.call("POST", "/operator/replay")
        self.assertTrue(result["ok"], result["violations"])

    def test_unknown_route_404(self):
        st, _ = self.call("GET", "/nope")
        self.assertEqual(st, 404)


if __name__ == "__main__":
    unittest.main()
