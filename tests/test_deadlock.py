"""等待环检测与解环策略。"""

import unittest

from app.deadlock import effective_priorities
from app.models import LeaseState

from helpers import enter_cycle, make_coordinator, ring_with_three_vehicles


class DeadlockDetectionTest(unittest.TestCase):
    def setUp(self):
        self.coord, self.clock = make_coordinator()
        ring_with_three_vehicles(self.coord)

    def test_wait_graph_shows_cycle(self):
        enter_cycle(self.coord, self.clock)
        graph = self.coord.wait_graph()
        self.assertEqual(len(graph["edges"]), 3)
        self.assertEqual(graph["cycles"], [["AGV1", "AGV2", "AGV3"]])

    def test_resolution_retreats_cheapest_feasible_vehicle(self):
        enter_cycle(self.coord, self.clock)
        self.coord.tick()
        self.assertEqual(len(self.coord.resolution_log), 1)
        action = self.coord.resolution_log[0]["action"]
        # AGV2 危险级最低、可倒车、有空闲支线 -> 被选为退让者
        self.assertEqual(action["kind"], "retreat")
        self.assertEqual(action["vehicle_id"], "AGV2")
        self.assertEqual(action["detail"]["to"], "H1")
        # 环已破：AGV1 对 S2 的申请获准
        s2 = [l for l in self.coord.leases.values()
              if l.vehicle_id == "AGV1" and l.resource_id == "S2"][0]
        self.assertEqual(s2.state, LeaseState.GRANTED)
        # AGV2 物理位置在退让位
        self.assertEqual(self.coord.vehicles["AGV2"].position, "H1")

    def test_fire_zone_vehicle_never_chosen_to_retreat(self):
        # AGV3 在消防区 S3 内：只能经 S1 撤离，不能退到 H2
        enter_cycle(self.coord, self.clock)
        self.coord.tick()
        action = self.coord.resolution_log[0]["action"]
        self.assertNotEqual(action["vehicle_id"], "AGV3")

    def test_manual_vehicle_never_moved_by_resolver(self):
        enter_cycle(self.coord, self.clock)
        self.coord.set_control_mode("AGV2", "manual")  # 人工接管优先
        self.coord.tick()
        action = self.coord.resolution_log[0]["action"]
        self.assertEqual(action["vehicle_id"], "AGV1")  # 只能选 AGV1
        self.assertEqual(action["detail"]["to"], "H0")

    def test_all_manual_cycle_requires_human(self):
        enter_cycle(self.coord, self.clock)
        for vid in ("AGV1", "AGV2", "AGV3"):
            self.coord.set_control_mode(vid, "manual")
        self.coord.tick()
        action = self.coord.resolution_log[0]["action"]
        self.assertEqual(action["kind"], "manual")

    def test_retreat_target_never_fire_exit(self):
        # S1 是消防区指定出口：AGV1 退让不得占用 S1（这里 H0 空闲可选）
        enter_cycle(self.coord, self.clock)
        self.coord.set_control_mode("AGV2", "safe_stop")
        self.coord.tick()
        action = self.coord.resolution_log[0]["action"]
        self.assertEqual(action["vehicle_id"], "AGV1")
        self.assertNotEqual(action["detail"]["to"], "S1")

    def test_fire_exit_excluded_from_retreat_candidates(self):
        # 专门构造：消防出口段 E 恰好是车辆身后的可倒车路段，必须被排除
        coord, clock = make_coordinator()
        coord.add_segment("M", "N0", "N1")
        coord.add_segment("E", "N5", "N0")   # 消防区指定出口，终点在 M 的起点
        coord.add_segment("P", "N6", "N0")   # 普通避让支线
        coord.add_fire_zone("FZ", ["Z1"], ["E"])
        coord.register_vehicle("V", hazard_level=0, battery=1.0, node="N0")
        coord.assign_task("V", "T", "N1", route=["M"])
        r = coord.request_reservation("r1", "V", "M", 0.0, 100.0)
        coord.enter(r["lease"]["lease_id"], "V")
        from app.deadlock import retreat_target
        self.assertEqual(retreat_target(coord, coord.vehicles["V"]), "P")

    def test_priority_inheritance_relieves_inversion(self):
        enter_cycle(self.coord, self.clock)
        eff = effective_priorities(self.coord)
        # AGV1（危险级2）等待 AGV2（危险级0）持有的 S2 -> AGV2 继承高优先级
        own = max(l.priority for l in self.coord.leases.values()
                  if l.vehicle_id == "AGV2")
        self.assertGreater(eff["AGV2"], own)
        suggestions = self.coord.suggestions()
        self.assertTrue(any(s["type"] == "priority_inheritance"
                            and s["holder"] == "AGV2" for s in suggestions))

    def test_suggestions_include_uncertain_confirmation(self):
        enter_cycle(self.coord, self.clock)
        self.coord.heartbeat("AGV2")
        self.coord.heartbeat("AGV3")
        self.clock.advance(31.0)  # AGV1 心跳丢失
        self.coord.tick()
        suggestions = self.coord.suggestions()
        self.assertTrue(any(s["type"] == "confirm_position"
                            and s["vehicle_id"] == "AGV1" for s in suggestions))
        self.assertTrue(any(s["type"] == "heartbeat_lost"
                            and s["vehicle_id"] == "AGV1" for s in suggestions))


if __name__ == "__main__":
    unittest.main()
