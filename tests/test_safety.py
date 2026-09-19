"""心跳丢失、安全急停/人工接管与重规划测试。"""

import unittest

from app import scenarios
from app.models import LeaseState, VehicleSpec


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = scenarios.build_service()
        g = self.svc.request_resource("V1", "A2", request_id="hold")
        self.svc.enter_resource("V1", g["lease_id"])
        self.lease_id = g["lease_id"]

    def test_timeout_goes_uncertain_not_released(self):
        self.clock.advance(13)
        out = self.svc.tick()
        self.assertEqual(out["heartbeat_lost"], [self.lease_id])
        lease = self.svc.ledger.get(self.lease_id)
        self.assertEqual(lease.state, LeaseState.UNCERTAIN)
        # 待确认期间其他车拿不到租约
        r = self.svc.request_resource("V2", "A2", request_id="want")
        self.assertEqual(r["status"], "waiting")

    def test_confirm_still_occupied_keeps_resource(self):
        self.clock.advance(13)
        self.svc.tick()
        resp = self.svc.confirm_position("V1", "A2", occupied=True)
        self.assertEqual(resp["state"], "entered")
        self.assertEqual(len(self.svc.ledger.active_on("A2")), 1)

    def test_release_requires_confirmation_of_empty(self):
        self.clock.advance(13)
        self.svc.tick()
        # 心跳恢复但上报位置仍在 A2 -> 保持占用
        hb = self.svc.heartbeat("V1", "A2")
        self.assertEqual(hb["position"], "A2")
        self.assertEqual(self.svc.ledger.get(self.lease_id).state,
                         LeaseState.ENTERED)
        # 人工确认已空才释放
        self.clock.advance(20)
        self.svc.tick()
        self.svc.confirm_position("V1", "A2", occupied=False)
        self.assertEqual(self.svc.ledger.get(self.lease_id).state,
                         LeaseState.RELEASED)


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = scenarios.build_service()

    def test_safe_stop_blocks_new_grants_keeps_occupied(self):
        g = self.svc.request_resource("V1", "A2", request_id="hold")
        self.svc.enter_resource("V1", g["lease_id"])
        pending = self.svc.request_resource("V2", "A3", request_id="p")
        self.svc.safe_stop()  # 全场急停
        # 已占用租约保留
        self.assertEqual(self.svc.ledger.get(g["lease_id"]).state,
                         LeaseState.ENTERED)
        again = self.svc.request_resource("V1", "A3", request_id="after-stop")
        self.assertEqual(again["status"], "denied")
        self.assertEqual(again["reason"], "safe_stop")

    def test_manual_takeover_has_priority(self):
        self.svc.manual_takeover("V2", reason="司机远程介入")
        r = self.svc.request_resource("V2", "C2", request_id="x")
        self.assertEqual(r["status"], "denied")
        self.assertEqual(r["reason"], "manual_takeover")
        # 恢复自动后可继续
        self.svc.resume_automatic("V2")
        g = self.svc.request_resource("V2", "C2", request_id="y")
        self.assertEqual(g["status"], "granted")


class ReplanTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = scenarios.build_service()

    def test_close_only_replans_unpassed_and_keeps_history(self):
        # V1 路线 A1-A2-A3-D1；先走到 A2
        self.svc.submit_plan("V1", resources=("S1", "A1", "A2", "A3", "D1"),
                             reason="配送 D1")
        for rid in ["A1", "A2"]:
            g = self.svc.request_resource("V1", rid, request_id=f"V1-{rid}")
            self.svc.enter_resource("V1", g["lease_id"])
            self.clock.advance(1)
        old = self.svc.active_plan("V1")
        self.assertEqual(old.plan_id, "P1-V1")
        out = self.svc.close_segment("A3", reason="消防巡检封闭")
        self.assertIn("V1", out["replanned"])
        # 旧计划保留且记录理由；新计划起点是当前位置，已通过路段不动
        old = next(p for p in self.svc.plan_history("V1")
                   if p["plan_id"] == "P1-V1")
        self.assertEqual(old["status"], "superseded")
        self.assertIn("原路线已归档", old["reason"])
        new = self.svc.active_plan("V1")
        self.assertEqual(new.parent_id, "P1-V1")
        self.assertEqual(new.resources[0], "A2")
        self.assertNotIn("A3", new.resources)
        self.assertEqual(new.destination, "D1")
        self.assertEqual(self.svc.fleet.get("V1").progress, 0)

    def test_vehicle_fault_blocks_resource_and_replans_others(self):
        # V2 故障在 C2；V3 尚未出发、原计划经过 C2，应绕行
        self.svc.submit_plan("V3", resources=("S1", "A1", "A2", "C2", "C3", "D2"),
                             reason="配送 D2")
        g = self.svc.request_resource("V2", "C2", request_id="v2-hold")
        self.svc.enter_resource("V2", g["lease_id"])
        out = self.svc.vehicle_fault("V2", reason="驱动轮抱死")
        self.assertEqual(out["blocked_resources"], ["C2"])
        self.assertEqual(self.svc.ledger.get(g["lease_id"]).state,
                         LeaseState.UNCERTAIN)
        self.assertIn("V3", out["others_replanned"])
        new = self.svc.active_plan("V3")
        self.assertNotIn("C2", new.resources)


if __name__ == "__main__":
    unittest.main()
