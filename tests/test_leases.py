"""租约发放、幂等重试与安全门控测试。"""

import unittest

from app.clock import SimClock
from app.models import ControlMode, LeaseState, VehicleSpec
from app.service import RightOfWayService
from app.topology import Topology
from app.models import Resource, ResourceKind


def line_topo():
    t = Topology()
    for rid, kind in [("P0", ResourceKind.DEPOT), ("P1", ResourceKind.SEGMENT),
                      ("P2", ResourceKind.SEGMENT), ("P3", ResourceKind.DEPOT)]:
        t.add_resource(Resource(rid, kind))
    for a, b in [("P0", "P1"), ("P1", "P2"), ("P2", "P3")]:
        t.connect(a, b)
    return t


class LeaseGrantTest(unittest.TestCase):
    def setUp(self):
        self.clock = SimClock(1000)
        self.svc = RightOfWayService(line_topo(), clock=self.clock)
        self.svc.register_vehicle(VehicleSpec("V1"))
        self.svc.submit_plan("V1", resources=("P0", "P1", "P2", "P3"))

    def test_grant_then_enter_with_valid_lease_only(self):
        r = self.svc.request_resource("V1", "P1", request_id="q1")
        self.assertEqual(r["status"], "granted")
        with self.assertRaises(Exception):
            self.svc.enter_resource("V1", "L9999")          # 无有效租约
        # 未获准时（这里已 granted）不能凭旧授权超窗进入
        self.clock.advance(40)
        with self.assertRaises(Exception):
            self.svc.enter_resource("V1", r["lease_id"])    # 授权已失效
        # 重新申请才能继续（先由 tick 回收失效授权）
        self.svc.tick()
        r2 = self.svc.request_resource("V1", "P1", request_id="q2")
        self.assertEqual(r2["status"], "granted")
        self.svc.enter_resource("V1", r2["lease_id"])
        self.assertEqual(self.svc.fleet.get("V1").location, "P1")

    def test_retry_returns_original_reservation(self):
        r1 = self.svc.request_resource("V1", "P1", request_id="dup-1")
        r2 = self.svc.request_resource("V1", "P1", "dup-1")
        self.assertEqual(r1["lease_id"], r2["lease_id"])
        self.assertTrue(r2["communication_retry"])
        # 进入后重试同样返回原租约当前状态
        self.svc.enter_resource("V1", r1["lease_id"])
        r3 = self.svc.request_resource("V1", "P1", "dup-1")
        self.assertEqual(r3["state"], "entered")
        self.assertEqual(len([e for e in self.svc.events.all()
                              if e.type == "lease_requested"]), 1)

    def test_no_double_grant_on_conflicting_resource(self):
        self.svc.register_vehicle(VehicleSpec("V2"))
        r1 = self.svc.request_resource("V1", "P1", request_id="q1")
        self.assertEqual(r1["status"], "granted")
        r2 = self.svc.request_resource("V2", "P1", request_id="q2")
        self.assertEqual(r2["status"], "waiting")
        # 通信重试仍返回原排队预约
        self.assertEqual(self.svc.request_resource("V2", "P1", "q2")["lease_id"],
                         r2["lease_id"])
        self.svc.enter_resource("V1", r1["lease_id"])
        self.svc.release_lease("V1", r1["lease_id"])
        promoted = self.svc.ledger.by_request("q2")
        self.assertEqual(promoted.state, LeaseState.GRANTED)
