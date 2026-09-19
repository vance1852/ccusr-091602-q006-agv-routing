"""等待环检测、解环策略与消防区约束测试。"""

import unittest

from app import scenarios
from app.clock import SimClock
from app.models import LeaseState
from app.service import RightOfWayService
from app.topology import Topology


class ThreeWayCycleTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = scenarios.build_service()
        scenarios.seed_three_way_cycle(self.svc, self.clock)

    def test_wait_graph_has_one_cycle(self):
        g = self.svc.wait_graph()
        edges = {(e["from"], e["to"]) for e in g["edges"]}
        self.assertEqual(edges, {("V1", "V3"), ("V2", "V1"), ("V3", "V2")})
        cycles = [i for i in self.svc.incidents_view()
                  if i["kind"] == "wait_cycle" and i["status"] == "open"]
        self.assertEqual(len(cycles), 1)
        self.assertEqual(set(cycles[0]["vehicles"]), {"V1", "V2", "V3"})

    def test_resolution_chooses_feasible_low_hazard_vehicle(self):
        # V2 高危且电量不足、V3 无身后避让资源，均不可自动退让；
        # 只有 V1 低危、电量充足、身后 A1 空闲 -> 选中 V1
        inc = next(i for i in self.svc.incidents_view()
                   if i["kind"] == "wait_cycle")
        proposal = inc["applied_action"]["proposal"]
        self.assertEqual(proposal["action"], "retreat")
        self.assertEqual(proposal["target"], "V1")
        self.assertEqual(proposal["retreat_to"], "A1")
        scores = {s["vehicle_id"]: s for s in proposal["scores"]}
        self.assertFalse(scores["V2"]["feasible"])
        self.assertIn("电量", scores["V2"]["infeasible_reason"])
        self.assertFalse(scores["V3"]["feasible"])
        self.assertIn("避让", scores["V3"]["infeasible_reason"])
        self.assertTrue(scores["V1"]["feasible"])

    def test_apply_retreat_releases_only_after_confirmation(self):
        inc = next(i for i in self.svc.incidents
                   if i.kind == "wait_cycle")
        out = self.svc.apply_resolution(inc.incident_id)
        self.assertEqual(out["status"], "retreat_directed")
        # 原路段 A2 仍被 V1 占用，没有被直接释放
        held = [l for l in self.svc.ledger.vehicle_leases(
            "V1", frozenset({LeaseState.ENTERED}))]
        self.assertEqual({l.resource_id for l in held}, {"A2"})
        # 车确认进入避让位 A1 后，A2 才释放（此时 V2 的排队申请可被提升）
        self.svc.enter_resource("V1", out["lease"]["lease_id"])
        v1_holds = {l.resource_id for l in self.svc.ledger.vehicle_leases(
            "V1", frozenset({LeaseState.ENTERED, LeaseState.UNCERTAIN}))}
        self.assertNotIn("A2", v1_holds)
        self.assertEqual(v1_holds, {"A1"})
        self.assertTrue(all(i.status == "resolved"
                            for i in self.svc.incidents if i.kind == "wait_cycle"))
        # 环解除后等待者可以陆续获得授权
        self.svc.detect_situations()
        self.assertEqual(self.svc.integrity_violations(), [])

    def test_naive_priority_boost_does_not_grant_conflicting(self):
        # 即便高优先级 V2 催促，也不能给它发 A2（V1 占着）
        r = self.svc.request_resource("V2", "A2", request_id="retry-boost")
        self.assertEqual(r["status"], "waiting")
        self.assertEqual(self.svc.integrity_violations(), [])


class FireZoneTest(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = scenarios.build_service()

    def _drive_into_fire(self, vid="V1"):
        # 路线 S1..F1..F2..X1，车辆进入 F1 即标记消防区
        self.svc.submit_plan(
            vid, resources=("S1", "A1", "A2", "A3", "J1", "F1", "F2", "X1"),
            reason="穿越消防区任务")
        for rid in ["A1", "A2", "A3", "J1", "F1"]:
            g = self.svc.request_resource(vid, rid, request_id=f"{vid}-{rid}")
            self.assertEqual(g["status"], "granted", f"{rid}: {g}")
            self.svc.enter_resource(vid, g["lease_id"])
            self.clock.advance(2)

    def test_only_designated_exit_allowed(self):
        self._drive_into_fire()
        rt = self.svc.fleet.get("V1")
        self.assertTrue(rt.in_fire_zone)
        # F1 的邻居里 F2 朝出口、J1 回头，申请回头必须被拒
        back = self.svc.request_resource("V1", "J1", request_id="back")
        self.assertEqual(back["status"], "denied")
        self.assertEqual(back["reason"], "fire_zone_evacuation")
        # 通信重试必须返回同一次拒绝，不会变成排队
        back_retry = self.svc.request_resource("V1", "J1", "back")
        self.assertEqual(back_retry["status"], "denied")
        self.assertEqual(back_retry["reason"], "fire_zone_evacuation")
        self.assertEqual(back_retry["lease_id"], back["lease_id"])
        fwd = self.svc.request_resource("V1", "F2", request_id="fwd")
        self.assertEqual(fwd["status"], "granted")
        self.svc.enter_resource("V1", fwd["lease_id"])
        out = self.svc.request_resource("V1", "X1", request_id="exit")
        self.assertEqual(out["status"], "granted")
        self.svc.enter_resource("V1", out["lease_id"])
        self.assertFalse(self.svc.fleet.get("V1").in_fire_zone)


class FireCycleEvacuationTest(unittest.TestCase):
    def test_cycle_inside_fire_zone_forces_evacuation(self):
        from app.policy import Cycle, WaitEdge
        svc, clock = scenarios.build_service()
        # V1 进入消防区 F1；手工构造 V1 所在环并验证策略选择撤离
        svc.submit_plan("V1",
                        resources=("A3", "J1", "F1", "F2", "X1"),
                        reason="消防区任务")
        for rid in ["A3", "J1", "F1"]:
            g = svc.request_resource("V1", rid, request_id=f"V1-{rid}")
            svc.enter_resource("V1", g["lease_id"])
            clock.advance(1)
        self.assertTrue(svc.fleet.get("V1").in_fire_zone)

        cycle = Cycle(
            vehicles=("V1", "V2"),
            edges=(WaitEdge("V1", "V2", "F2", "entered"),
                   WaitEdge("V2", "V1", "C1", "entered")))
        # 对向车故障无法退让 -> 无可行自动退让 -> 消防车辆按指定出口撤离
        svc.fleet.get("V2").fault = "驱动轮抱死"
        svc.fleet.get("V2").online = False
        plans = {"V1": (("A3", "J1", "F1", "F2", "X1"), 2),
                 "V2": (("S2", "C1"), 0)}
        res = svc.policy.resolve(cycle, plans, clock.now())
        self.assertEqual(res.action, "evacuate_fire_zone")
        self.assertEqual(res.target_vehicle, "V1")
        self.assertEqual(res.retreat_to, "X1")  # 指定出口

        inc = svc._open_cycle_incident(cycle, res)
        out = svc.apply_resolution(inc.incident_id)
        self.assertEqual(out["status"], "evacuation_directed")
        self.assertEqual(out["lease"]["resource_id"], "F2")  # 撤离的下一步
        svc.enter_resource("V1", out["lease"]["lease_id"])
        # 身后 F1 已释放，车辆继续沿 F2 -> X1
        g = svc.request_resource("V1", "X1", request_id="V1-X1")
        self.assertEqual(g["status"], "granted")
        svc.enter_resource("V1", g["lease_id"])
        self.assertFalse(svc.fleet.get("V1").in_fire_zone)


if __name__ == "__main__":
    unittest.main()
