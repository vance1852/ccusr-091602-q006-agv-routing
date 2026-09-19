"""演示场景：三台 AGV 在环形窄巷互相等待，覆盖

1. 等待环检测与解环（低危险载荷车辆退让至避让支线）
2. 并发抢占（高优先级申请抢占“已授予未进入”的租约）
3. 心跳丢失 -> uncertain -> 位置确认 -> 重建
4. 人工接管优先（manual 车辆新申请被拒）
5. 事件回放 + 不变量校验（冲突资源无双车同时获准）

运行：python -m app.demo
"""

from __future__ import annotations

from .clock import ManualClock
from .coordinator import RightOfWayCoordinator, replay
from .topology import Topology

HEARTBEAT_TTL = 30.0


def build_scenario(coord: RightOfWayCoordinator) -> None:
    """拓扑与任务：环形窄巷 S1/S2/S3（容量1）+ 三条避让支线 + 消防区。"""
    coord.add_segment("S1", "N0", "N1")
    coord.add_segment("S2", "N1", "N2")
    coord.add_segment("S3", "N2", "N0")
    coord.add_segment("H0", "N9", "N0")   # 避让支线
    coord.add_segment("H1", "N8", "N1")
    coord.add_segment("H2", "N7", "N2")
    coord.add_fire_zone("FZ1", segments=["S3"], exits=["S1"])  # 区内只能经S1驶离

    coord.register_vehicle("AGV1", hazard_level=2, battery=0.9, node="N0")
    coord.register_vehicle("AGV2", hazard_level=0, battery=0.8, node="N1")
    coord.register_vehicle("AGV3", hazard_level=1, battery=0.6, node="N2")
    coord.assign_task("AGV1", "T1", goal_node="N0",
                      route=["S1", "S2", "S3"], deadline=3000.0)
    coord.assign_task("AGV2", "T2", goal_node="N1",
                      route=["S2", "S3", "S1"], deadline=3000.0)
    coord.assign_task("AGV3", "T3", goal_node="N2",
                      route=["S3", "S1", "S2"], deadline=3000.0)


def run_demo() -> dict:
    clock = ManualClock(0.0)
    coord = RightOfWayCoordinator(Topology(), clock, heartbeat_ttl=HEARTBEAT_TTL)
    build_scenario(coord)

    print("== 1. 三车各占一段窄巷 ==")
    clock.advance(1)
    for vid, seg in [("AGV1", "S1"), ("AGV2", "S2"), ("AGV3", "S3")]:
        r = coord.request_reservation(f"req-{vid}-{seg}", vid, seg, 1.0, 1000.0)
        coord.enter(r["lease"]["lease_id"], vid)
        print(f"  {vid} 进入 {seg}")

    print("== 2. 各自申请下一路段 -> 互相等待 ==")
    clock.advance(1)
    for vid, seg in [("AGV1", "S2"), ("AGV2", "S3"), ("AGV3", "S1")]:
        r = coord.request_reservation(f"req-{vid}-{seg}", vid, seg, 2.0, 1000.0)
        print(f"  {vid} 申请 {seg}: {r['lease']['state']}")
    graph = coord.wait_graph()
    print(f"  等待环: {graph['cycles']}")
    coord.tick()
    for rec in coord.resolution_log:
        print(f"  解环: {rec['action']['kind']} {rec['action']['vehicle_id']}"
              f" -> {rec['action']['detail']}  理由: {rec['action']['rationale']}")

    print("== 3. 并发抢占：高优先级申请抢占未进入的租约 ==")
    clock.advance(9)  # t=11
    r_low = coord.request_reservation("req-agv2-h2", "AGV2", "H2", 11.0, 500.0)
    print(f"  AGV2 申请 H2: {r_low['lease']['state']}")
    r_high = coord.request_reservation("req-agv1-h2", "AGV1", "H2", 11.0, 500.0)
    print(f"  AGV1 申请 H2: {r_high['lease']['state']}（AGV2 的租约被抢占）")
    # 幂等重试：返回原预约
    retry = coord.request_reservation("req-agv1-h2", "AGV1", "H2", 11.0, 500.0)
    assert retry["idempotent_replay"] and retry["lease"]["lease_id"] == r_high["lease"]["lease_id"]
    print("  通信重试返回原预约 ✓")

    print("== 4. 心跳丢失 -> uncertain -> 位置确认 -> 重建 ==")
    clock.advance(1)   # t=12
    coord.heartbeat("AGV1")
    coord.heartbeat("AGV2")
    clock.advance(28)  # t=40，AGV3 超过 TTL
    coord.tick()
    uncertain = [l for l in coord.leases.values() if l.state.value == "uncertain"]
    print(f"  uncertain 租约: {[(l.lease_id, l.resource_id) for l in uncertain]}")
    clock.advance(1)   # t=41
    coord.confirm_position("AGV3", segment_id="S3")
    print("  AGV3 位置确认在 S3，租约重建 ✓")

    print("== 5. 人工接管优先 ==")
    clock.advance(1)   # t=42
    coord.set_control_mode("AGV1", "manual")
    r = coord.request_reservation("req-agv1-s3", "AGV1", "S3", 42.0, 600.0)
    print(f"  manual 车辆新申请: {r['rejection']['reason']} ✓")

    print("== 6. 回放全部事件并校验不变量 ==")
    result = replay(coord.events.to_list(), heartbeat_ttl=HEARTBEAT_TTL)
    print(f"  回放命令事件 {result['commands_replayed']} 条，"
          f"不变量违反 {len(result['violations'])} 起 -> "
          f"{'通过 ✓' if result['ok'] else '失败 ✗'}")
    return {"coordinator": coord, "replay": result}


if __name__ == "__main__":
    run_demo()
