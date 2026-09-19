"""并发抢占、心跳丢失、优先级反转、人工接管的录制与确定性回放。

每一步后都校验台账不变量；回放结束后审计事件流，
确认从未向冲突资源同时发放两台车的有效租约。
"""

import unittest

from app import scenarios
from app.clock import SimClock
from app.replay import Recorder, assert_no_conflicting_grants
from app.service import RightOfWayService


def factory():
    return scenarios.build_service()


def build_recording():
    svc, clock = factory()
    rec = Recorder(svc, clock)
    t = 1000

    # 布置三车等待环
    routes = {
        "V1": ("A1", "A2", "A3", "J1", "C3", "D2"),
        "V2": ("S2", "C1", "C2", "A2", "A3", "D1"),
        "V3": ("S1", "A1", "A2", "C2", "A3", "D1"),
    }
    for vid, route in routes.items():
        rec.do(t, "submit_plan", vehicle_id=vid, resources=route,
               reason="初始任务路线")
    holds = {"V1": "A2", "V2": "C2", "V3": "A3"}
    for vid, rid in holds.items():
        t += 1
        g = rec.do(t, "request_resource", vehicle_id=vid, resource_id=rid,
                   request_id=f"{vid}-hold")
        rec.do(t, "enter_resource", vehicle_id=vid, lease_id=g["lease_id"])

    # 并发申请下一路段 -> 全部排队，环检测 + 优先级反转（高危 V2 等 V1）
    wants = {"V1": "A3", "V2": "A2", "V3": "C2"}
    t += 1
    want_responses = {}
    for vid, rid in wants.items():
        want_responses[vid] = rec.do(
            t, "request_resource", vehicle_id=vid, resource_id=rid,
            request_id=f"{vid}-want")

    # 通信重试必须返回原预约，不得产生第二张租约
    retry = rec.do(t, "request_resource", vehicle_id="V2",
                   resource_id="A2", request_id="V2-want")
    assert retry["lease_id"] == want_responses["V2"]["lease_id"]

    # 高危车/低电车/无退让位均不该被简单提权；策略选 V1 退让
    inc = next(i for i in svc.incidents if i.kind == "wait_cycle")
    t += 1
    directed = rec.do(t, "apply_resolution", incident_id=inc.incident_id)
    t += 1
    rec.do(t, "enter_resource", vehicle_id="V1",
           lease_id=directed["lease"]["lease_id"])  # 确认进入 A1，A2 才释放

    # 心跳丢失：静默超时
    t = 1020
    rec.do(t, "tick")
    # 人工接管永远优先
    t += 1
    rec.do(t, "manual_takeover", vehicle_id="V2", reason="值守员远程介入")
    denied = rec.do(t, "request_resource", vehicle_id="V2",
                    resource_id="A3", request_id="V2-while-manual")
    assert denied["status"] == "denied" and denied["reason"] == "manual_takeover"

    # 超时租约先确认位置：仍占用 -> 不释放
    t += 1
    rec.do(t, "confirm_position", vehicle_id="V3",
           resource_id="A3", occupied=True)
    # 之后确认已空 -> 才释放
    t += 1
    rec.do(t, "confirm_position", vehicle_id="V3",
           resource_id="A3", occupied=False)
    t += 1
    rec.do(t, "resume_automatic", vehicle_id="V2")
    return svc, rec


class ReplayScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.svc, cls.rec = build_recording()

    def test_no_grant_during_contention(self):
        incidents = self.svc.incidents_view()
        self.assertTrue(any(i["kind"] == "wait_cycle" for i in incidents))
        self.assertTrue(any(i["kind"] == "priority_inversion"
                            for i in incidents))
        # 全程台账无冲突
        self.assertEqual(self.svc.integrity_violations(), [])

    def test_uncertain_never_released_without_confirmation(self):
        events = self.svc.events.all()
        for e in events:
            if e.type == "heartbeat_lost":
                for lid in e.payload["leases"]:
                    lease = self.svc.ledger.get(lid)
                    # 最终要么被确认仍占用过，要么确认清空后才释放
                    self.assertNotEqual(lease.state.value, "granted")

    def test_replay_is_deterministic_and_conflict_free(self):
        report = self.rec.replay(factory)
        self.assertEqual(report["integrity_violations"], [])
        # 回放实例的事件数应与原录制一致
        self.assertEqual(report["final_events"], len(self.svc.events.all()))
        overlaps = assert_no_conflicting_grants(self.svc.events.all())
        self.assertEqual(overlaps, [])

    def test_timeline_and_denials_visible(self):
        timeline = self.svc.timeline()
        self.assertTrue(any(r["state"] == "released" and r["resource_id"] == "A2"
                            for r in timeline))
        reasons = {d["reason"] for d in self.svc.denials_view()}
        self.assertIn("manual_takeover", reasons)


if __name__ == "__main__":
    unittest.main()
