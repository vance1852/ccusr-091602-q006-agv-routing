"""值守员 HTTP API 端到端冒烟测试。"""

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer

from app.api import Handler
from app import scenarios
from app import api as api_module


def _free_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = _free_server()
        api_module.HOLDER.svc, api_module.HOLDER.clock = scenarios.build_service()
        scenarios.seed_three_way_cycle(api_module.HOLDER.svc,
                                       api_module.HOLDER.clock)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, payload,
                     {"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        return resp.status, data

    def test_status_shows_wait_graph(self):
        status, data = self._req("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["wait_graph"]["edges"])
        self.assertEqual(data["integrity_violations"], [])
        cycle = next(i for i in data["open_incidents"]
                     if i["kind"] == "wait_cycle")
        self.assertTrue(cycle["suggested_actions"])

    def test_timeline_denials_and_apply(self):
        _, denials = self._req("GET", "/api/denials")
        self.assertTrue(all("reason" in d for d in denials))
        _, status = self._req("GET", "/api/status")
        inc = next(i for i in status["open_incidents"]
                   if i["kind"] == "wait_cycle")
        code, out = self._req("POST",
                              f"/api/incidents/{inc['incident_id']}/apply",
                              {"action": "retreat"})
        self.assertEqual(code, 200)
        self.assertEqual(out["status"], "retreat_directed")

    def test_safe_stop_endpoint(self):
        code, out = self._req("POST", "/api/safe-stop", {"vehicle_id": "V3"})
        self.assertEqual(code, 200)
        self.assertTrue(out["safe_stop"])
        _, data = self._req("GET", "/api/status")
        v3 = next(v for v in data["vehicles"] if v["vehicle_id"] == "V3")
        self.assertEqual(v3["control"], "safe_stop")


if __name__ == "__main__":
    unittest.main()
