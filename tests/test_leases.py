"""租约生命周期：授予、幂等、超时、位置确认、抢占、消防区。"""

import unittest

from app.models import LeaseState

from helpers import make_coordinator


def grant(coord, req, vid, res, start, end):
    r = coord.request_reservation(req, vid, res, start, end)
    assert r["ok"], r
    return r["lease"]


class LeaseLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.coord, self.clock = make_coordinator(ttl=10.0)
        self.coord.add_segment("S1", "N0", "N1")
        self.coord.add_segment("S2", "N1", "N2")
        self.coord.register_vehicle("A", node="N0")
        self.coord.register_vehicle("B", node="N0")

    def test_grant_on_free_resource(self):
        lease = grant(self.coord, "r1", "A", "S1", 0.0, 10.0)
        self.assertEqual(lease["state"], "granted")

    def test_adjacent_windows_do_not_conflict(self):
        # 左闭右开：[0,10) 与 [10,20) 不冲突
        a = grant(self.coord, "r1", "A", "S1", 0.0, 10.0)
        b = grant(self.coord, "r2", "B", "S1", 10.0, 20.0)
        self.assertEqual(a["state"], "granted")
        self.assertEqual(b["state"], "granted")

    def test_overlapping_request_waits(self):
        grant(self.coord, "r1", "A", "S1", 0.0, 10.0)
        b = grant(self.coord, "r2", "B", "S1", 5.0, 15.0)
        self.assertEqual(b["state"], "requested")

    def test_idempotent_retry_returns_original(self):
        first = grant(self.coord, "r1", "A", "S1", 0.0, 10.0)
        retry = self.coord.request_reservation("r1", "A", "S1", 0.0, 10.0)
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(retry["lease"]["lease_id"], first["lease_id"])
        self.assertEqual(len(self.coord.leases), 1)

    def test_idempotent_retry_returns_original_rejection(self):
        r1 = self.coord.request_reservation("r1", "A", "S1", 5.0, 5.0)  # 无效窗口
        self.assertFalse(r1["ok"])
        r2 = self.coord.request_reservation("r1", "A", "S1", 5.0, 5.0)
        self.assertFalse(r2["ok"])
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(len(self.coord.rejections), 1)

    def test_enter_requires_valid_lease(self):
        lease = grant(self.coord, "r1", "A", "S1", 0.0, 10.0)
        bad = self.coord.enter(lease["lease_id"], "B")
        self.assertFalse(bad["ok"])
        ok = self.coord.enter(lease["lease_id"], "A")
        self.assertTrue(ok["ok"])
        self.assertEqual(self.coord.vehicles["A"].position, "S1")

    def test_release_frees_resource_for_waiter(self):
        la = grant(self.coord, "r1", "A", "S1", 0.0, 10.0)
        lb = grant(self.coord, "r2", "B", "S1", 0.0, 10.0)
        self.assertEqual(lb["state"], "requested")
        self.coord.release(la["lease_id"], "A")
        self.assertEqual(self.coord.leases[lb["lease_id"]].state, LeaseState.GRANTED)

    def test_heartbeat_timeout_makes_uncertain_but_not_free(self):
        la = grant(self.coord, "r1", "A", "S1", 0.0, 100.0)
        self.coord.enter(la["lease_id"], "A")
        self.clock.advance(11.0)  # 超过 TTL=10
        self.coord.tick()
        lease = self.coord.leases[la["lease_id"]]
        self.assertEqual(lease.state, LeaseState.UNCERTAIN)
        # 资源不释放：B 的重叠申请仍然只能等待
        lb = grant(self.coord, "r2", "B", "S1", 11.0, 50.0)
        self.assertEqual(lb["state"], "requested")

    def test_position_confirmed_same_segment_reestablishes(self):
        la = grant(self.coord, "r1", "A", "S1", 0.0, 100.0)
        self.coord.enter(la["lease_id"], "A")
        self.clock.advance(11.0)
        self.coord.tick()
        self.coord.confirm_position("A", segment_id="S1")
        self.assertEqual(self.coord.leases[la["lease_id"]].state, LeaseState.ENTERED)

    def test_position_confirmed_elsewhere_expires_and_frees(self):
        la = grant(self.coord, "r1", "A", "S1", 0.0, 100.0)
        self.coord.enter(la["lease_id"], "A")
        self.clock.advance(11.0)
        self.coord.tick()
        # A 被确认在别处（不在 S1 上）才允许释放
        self.coord.confirm_position("A", node_id="N9")
        self.assertEqual(self.coord.leases[la["lease_id"]].state, LeaseState.EXPIRED)
        lb = grant(self.coord, "r2", "B", "S1", 11.0, 50.0)
        self.assertEqual(lb["state"], "granted")

    def test_entered_lease_window_timeout_goes_uncertain_not_released(self):
        la = grant(self.coord, "r1", "A", "S1", 0.0, 5.0)
        self.coord.enter(la["lease_id"], "A")
        self.clock.advance(6.0)  # 窗口已过但未报告离开
        self.coord.tick()
        self.assertEqual(self.coord.leases[la["lease_id"]].state, LeaseState.UNCERTAIN)

    def test_unused_granted_window_expires_safely(self):
        # 已授予但从未进入：资源从未被物理占用，窗口过期可直接终结
        la = grant(self.coord, "r1", "A", "S1", 0.0, 5.0)
        self.clock.advance(6.0)
        self.coord.tick()
        self.assertEqual(self.coord.leases[la["lease_id"]].state, LeaseState.EXPIRED)

    def test_safe_stop_and_manual_requests_rejected(self):
        self.coord.set_control_mode("A", "safe_stop")
        r = self.coord.request_reservation("r1", "A", "S1", 0.0, 10.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["rejection"]["reason"], "control_mode_not_automatic")
        self.coord.set_control_mode("A", "manual")
        r = self.coord.request_reservation("r2", "A", "S1", 0.0, 10.0)
        self.assertFalse(r["ok"])

    def test_fire_zone_exit_only(self):
        self.coord.add_fire_zone("FZ", ["S1"], ["S2"])
        la = grant(self.coord, "r0", "A", "S1", 0.0, 100.0)
        self.coord.enter(la["lease_id"], "A")  # A 进入消防区
        # 区内车辆申请非出口路段被拒
        self.coord.add_segment("S3", "N1", "N3")
        r = self.coord.request_reservation("r1", "A", "S3", 1.0, 10.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["rejection"]["reason"], "fire_zone_exit_only")
        # 指定出口允许
        r = self.coord.request_reservation("r2", "A", "S2", 1.0, 10.0)
        self.assertTrue(r["ok"])


class PreemptionTest(unittest.TestCase):
    def setUp(self):
        self.coord, self.clock = make_coordinator()
        self.coord.add_segment("S1", "N0", "N1")
        self.coord.register_vehicle("LOW", hazard_level=0, battery=1.0, node="N0")
        self.coord.register_vehicle("HIGH", hazard_level=3, battery=0.5, node="N0")

    def test_high_priority_preempts_granted_not_entered(self):
        low = grant(self.coord, "r1", "LOW", "S1", 0.0, 100.0)
        self.assertEqual(low["state"], "granted")
        high = grant(self.coord, "r2", "HIGH", "S1", 0.0, 100.0)
        self.assertEqual(high["state"], "granted")
        self.assertEqual(self.coord.leases[low["lease_id"]].state,
                         LeaseState.REQUESTED)

    def test_entered_lease_is_never_preempted(self):
        low = grant(self.coord, "r1", "LOW", "S1", 0.0, 100.0)
        self.coord.enter(low["lease_id"], "LOW")
        high = grant(self.coord, "r2", "HIGH", "S1", 0.0, 100.0)
        self.assertEqual(high["state"], "requested")
        self.assertEqual(self.coord.leases[low["lease_id"]].state,
                         LeaseState.ENTERED)

    def test_manual_holder_not_preempted(self):
        low = grant(self.coord, "r1", "LOW", "S1", 0.0, 100.0)
        self.coord.set_control_mode("LOW", "manual")  # 人工接管永远优先
        high = grant(self.coord, "r2", "HIGH", "S1", 0.0, 100.0)
        self.assertEqual(high["state"], "requested")
        self.assertEqual(self.coord.leases[low["lease_id"]].state,
                         LeaseState.GRANTED)


if __name__ == "__main__":
    unittest.main()
