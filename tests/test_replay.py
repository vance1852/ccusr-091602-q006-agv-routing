"""事件回放与安全不变量：并发抢占、心跳丢失、优先级反转、人工接管。"""

import unittest

from app.coordinator import replay
from app.demo import run_demo
from app.models import Lease, LeaseState

from helpers import enter_cycle, make_coordinator, ring_with_three_vehicles


class ReplayInvariantTest(unittest.TestCase):
    def test_demo_scenario_replays_without_violations(self):
        result = run_demo()["replay"]
        self.assertTrue(result["ok"], result["violations"])
        self.assertGreater(result["commands_replayed"], 0)

    def test_preemption_heartbeat_inversion_takeover_replay(self):
        coord, clock = make_coordinator(ttl=10.0)
        ring_with_three_vehicles(coord)
        enter_cycle(coord, clock)          # 等待环 + 优先级反转（继承）
        coord.tick()                        # 解环
        clock.advance(9)                    # t=11
        # 并发抢占
        coord.request_reservation("p1", "AGV2", "H2", 11.0, 500.0)
        coord.request_reservation("p2", "AGV1", "H2", 11.0, 500.0)
        # 心跳丢失与恢复
        coord.heartbeat("AGV1")
        coord.heartbeat("AGV2")
        clock.advance(29)                   # t=40，AGV3 超时
        coord.tick()
        coord.confirm_position("AGV3", segment_id="S3")
        # 人工接管
        coord.set_control_mode("AGV1", "manual")
        result = replay(coord.events.to_list(), heartbeat_ttl=10.0)
        self.assertTrue(result["ok"], result["violations"])

    def test_invariant_checker_catches_double_entry(self):
        # 手工构造“两车同时进入同一资源”的非法状态，校验器必须捕获
        coord, _ = make_coordinator()
        coord.add_segment("S1", "N0", "N1")
        coord.register_vehicle("A", node="N0")
        coord.register_vehicle("B", node="N0")
        for i, vid in enumerate(("A", "B"), start=1):
            lease = Lease(lease_id=f"X{i}", request_id=f"x{i}", vehicle_id=vid,
                          resource_id="S1", start=0.0, end=100.0,
                          state=LeaseState.ENTERED, granted_at=0.0, entered_at=1.0)
            coord.leases[lease.lease_id] = lease
        violations = coord.check_invariants()
        kinds = {v["type"] for v in violations}
        self.assertIn("physical_overlap", kinds)
        self.assertIn("window_overlap", kinds)

    def test_uncertain_lease_still_counts_as_holding(self):
        # uncertain 租约仍视为占用：回放不变量覆盖“超时未释放”场景
        coord, clock = make_coordinator(ttl=5.0)
        coord.add_segment("S1", "N0", "N1")
        coord.register_vehicle("A", node="N0")
        coord.register_vehicle("B", node="N0")
        r = coord.request_reservation("r1", "A", "S1", 0.0, 100.0)
        coord.enter(r["lease"]["lease_id"], "A")
        clock.advance(6.0)
        coord.tick()  # A 的租约 -> uncertain
        waiting = coord.request_reservation("r2", "B", "S1", 6.0, 100.0)
        self.assertEqual(waiting["lease"]["state"], "requested")
        result = replay(coord.events.to_list(), heartbeat_ttl=5.0)
        self.assertTrue(result["ok"], result["violations"])
        # 回放后 B 的租约仍在等待（资源未被错误释放）
        self.assertEqual(result["leases"][waiting["lease"]["lease_id"]]["state"],
                         "requested")


if __name__ == "__main__":
    unittest.main()
