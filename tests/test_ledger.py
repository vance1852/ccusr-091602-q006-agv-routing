"""时间窗与台账状态机测试。"""

import unittest

from app.ledger import Ledger, IllegalTransition
from app.models import LeaseState


class WindowSemanticsTest(unittest.TestCase):
    def test_half_open_windows(self):
        ledger = Ledger()
        a = ledger.open_request("r1", "V1", "A2", 0)
        ledger.grant(a.lease_id, 10, 20)
        # 首尾相接 [20,30) 与 [10,20) 不冲突
        self.assertEqual(ledger.conflicts("A2", 20, 30), [])
        # 任意重叠都冲突
        self.assertEqual(len(ledger.conflicts("A2", 19.9, 25)), 1)
        self.assertEqual(len(ledger.conflicts("A2", 5, 10.1)), 1)

    def test_uncertain_blocks_everybody(self):
        ledger = Ledger()
        a = ledger.open_request("r1", "V1", "A2", 0)
        ledger.grant(a.lease_id, 0, 30)
        ledger.enter(a.lease_id, 1)
        ledger.mark_uncertain(a.lease_id, 5, "心跳丢失")
        self.assertEqual(ledger.active_on("A2")[0].state, LeaseState.UNCERTAIN)
        self.assertEqual(len(ledger.conflicts("A2", 10, 40)), 1)

    def test_only_never_entered_grant_may_expire(self):
        ledger = Ledger()
        a = ledger.open_request("r1", "V1", "A2", 0)
        ledger.grant(a.lease_id, 0, 30)
        ledger.expire(a.lease_id, 31)
        self.assertEqual(a.state, LeaseState.EXPIRED)

        b = ledger.open_request("r2", "V2", "A3", 0)
        ledger.grant(b.lease_id, 0, 30)
        ledger.enter(b.lease_id, 1)
        with self.assertRaises(IllegalTransition):
            ledger.expire(b.lease_id, 31)  # 物理占用不能直接过期回收

    def test_integrity_scan_detects_overlap(self):
        ledger = Ledger()
        a = ledger.open_request("r1", "V1", "A2", 0)
        ledger.grant(a.lease_id, 0, 30)
        # 人为制造台账层不应出现的重叠
        b = ledger.open_request("r2", "V2", "A2", 0)
        ledger.grant(b.lease_id, 10, 40)
        self.assertEqual(len(ledger.integrity_violations()), 1)


if __name__ == "__main__":
    unittest.main()
