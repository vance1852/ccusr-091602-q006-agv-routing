"""并发抢占压力测试：多车同时申请同一冲突资源，只能有一张有效租约。"""

import threading
import unittest

from app import scenarios
from app.models import LeaseState


class ConcurrentRequestsTest(unittest.TestCase):
    def test_only_one_grant_under_parallel_requests(self):
        svc, clock = scenarios.build_service()
        barrier = threading.Barrier(8)
        results: list[dict] = []
        lock = threading.Lock()

        def worker(i):
            vid = f"V{(i % 3) + 1}"
            # 三台真实车 + 各自重复请求；用屏障制造真正并发
            req_id = f"race-{i}"
            barrier.wait()
            r = svc.request_resource(vid, "A2", request_id=req_id)
            with lock:
                results.append(r)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 服务本身无锁（API 层串行化），这里直接验证台账不变量：
        # 同一资源上 granted 租约至多一张
        grants = [l for l in svc.ledger.leases.values()
                  if l.resource_id == "A2" and l.state == LeaseState.GRANTED]
        self.assertLessEqual(len(grants), 1)
        self.assertEqual(svc.integrity_violations(), [])

    def test_http_lock_serializes_conflicting_grants(self):
        import http.client
        import json
        import threading as th
        from http.server import ThreadingHTTPServer
        from app.api import Handler
        from app import api as api_module

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thd = th.Thread(target=server.serve_forever, daemon=True)
        thd.start()
        try:
            # 置为干净状态（车辆已注册但尚无位置，资源均空闲）
            with api_module.HOLDER.lock:
                api_module.HOLDER.svc, api_module.HOLDER.clock = \
                    scenarios.build_service()

            def call(method, path, body=None):
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                data = json.dumps(body).encode() if body else None
                c.request(method, path, data,
                          {"Content-Type": "application/json"} if data else {})
                r = c.getresponse()
                out = json.loads(r.read().decode())
                c.close()
                return out

            barrier = th.Barrier(6)
            outcomes = []
            olock = th.Lock()

            def race(vid, idx):
                barrier.wait()
                out = call("POST", "/api/leases/request",
                           {"vehicle_id": vid, "resource_id": "B1",
                            "request_id": f"http-race-{idx}"})
                with olock:
                    outcomes.append(out)

            vids = ["V1", "V2", "V3"] * 2
            ts = [th.Thread(target=race, args=(v, i))
                  for i, v in enumerate(vids)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            # 同车重复申请会回显既有租约；按租约号去重后必须只有一张授权
            granted_ids = {o["lease_id"] for o in outcomes
                           if o.get("status") == "granted"}
            self.assertEqual(len(granted_ids), 1, outcomes)
            self.assertEqual(call("GET", "/api/status")["integrity_violations"],
                             [])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
