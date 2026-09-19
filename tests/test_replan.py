"""局部重规划：路段封闭与车辆故障只影响未通过路段，保留原路线与理由。"""

import unittest

from app.models import LeaseState

from helpers import make_coordinator


class ReplanTest(unittest.TestCase):
    def setUp(self):
        self.coord, self.clock = make_coordinator()
        # 主路 A-B-C-D；E/F 绕过 C，G/H 绕过 B
        self.coord.add_segment("A", "N0", "N1")
        self.coord.add_segment("B", "N1", "N2")
        self.coord.add_segment("C", "N2", "N3")
        self.coord.add_segment("D", "N3", "N4")
        self.coord.add_segment("E", "N2", "N5")
        self.coord.add_segment("F", "N5", "N3")
        self.coord.add_segment("G", "N1", "N6")
        self.coord.add_segment("H", "N6", "N2")
        self.coord.register_vehicle("V1", node="N0")
        self.coord.assign_task("V1", "T1", "N4",
                               route=["A", "B", "C", "D"], deadline=5000.0)

    def _pass_segment_a(self):
        r = self.coord.request_reservation("r-a", "V1", "A", 0.0, 100.0)
        self.coord.enter(r["lease"]["lease_id"], "V1")
        self.coord.release(r["lease"]["lease_id"], "V1")  # 驶离 A -> passed=1

    def test_segment_closed_replans_only_unpassed_tail(self):
        self._pass_segment_a()
        self.coord.close_segment("C")
        task = self.coord.vehicles["V1"].task
        self.assertEqual(task.route[:1], ["A"])            # 已通过前缀保留
        self.assertEqual(task.route, ["A", "B", "E", "F", "D"])
        self.assertEqual(len(task.revisions), 1)
        rev = task.revisions[0]
        self.assertEqual(rev.old_tail, ["B", "C", "D"])    # 原路线保留
        self.assertEqual(rev.new_tail, ["B", "E", "F", "D"])
        self.assertIn("segment_closed:C", rev.reason)
        self.assertTrue(any("重规划" in r for r in task.rationale))  # 决策理由

    def test_unaffected_vehicle_not_replanned(self):
        self.coord.register_vehicle("V2", node="N0")
        self.coord.assign_task("V2", "T2", "N2", route=["A", "B"])
        self.coord.close_segment("C")
        self.assertEqual(self.coord.vehicles["V2"].task.revisions, [])

    def test_vehicle_fault_blocks_segment_without_release(self):
        # V2 在 B 上故障
        self.coord.register_vehicle("V2", node="N1")
        self.coord.assign_task("V2", "T2", "N3", route=["B", "C"])
        r = self.coord.request_reservation("r-b", "V2", "B", 0.0, 100.0)
        self.coord.enter(r["lease"]["lease_id"], "V2")
        lease_id = r["lease"]["lease_id"]

        self.coord.vehicle_fault("V2")
        # 故障车租约转 uncertain 等待位置确认，绝不直接释放
        self.assertEqual(self.coord.leases[lease_id].state, LeaseState.UNCERTAIN)
        # V1 的路线绕开故障车所在的 B
        task = self.coord.vehicles["V1"].task
        self.assertNotIn("B", task.remaining)
        self.assertTrue(any("vehicle_fault:V2" in rev.reason
                            for rev in task.revisions))
        # 故障车新申请被拒
        r = self.coord.request_reservation("r-c", "V2", "C", 1.0, 50.0)
        self.assertFalse(r["ok"])
        self.assertEqual(r["rejection"]["reason"], "vehicle_fault")

    def test_replan_failure_keeps_route_and_records_reason(self):
        self._pass_segment_a()
        self.coord.close_segment("D")  # 唯一通往终点 N4 的路段
        task = self.coord.vehicles["V1"].task
        # 无可达路径：路线保持，理由记录失败
        self.assertEqual(task.remaining, ["B", "C", "D"])
        self.assertTrue(any("重规划失败" in r for r in task.rationale))


if __name__ == "__main__":
    unittest.main()
