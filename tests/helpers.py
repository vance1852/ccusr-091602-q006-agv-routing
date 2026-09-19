"""测试公共搭建：环形窄巷 + 支线 + 消防区。"""

from app.clock import ManualClock
from app.coordinator import RightOfWayCoordinator
from app.topology import Topology


def make_coordinator(ttl: float = 30.0) -> tuple[RightOfWayCoordinator, ManualClock]:
    clock = ManualClock(0.0)
    coord = RightOfWayCoordinator(Topology(), clock, heartbeat_ttl=ttl)
    return coord, clock


def build_ring(coord: RightOfWayCoordinator) -> None:
    """S1/S2/S3 环形窄巷（容量1）+ 避让支线 H0/H1/H2 + 消防区 FZ1(S3)。"""
    coord.add_segment("S1", "N0", "N1")
    coord.add_segment("S2", "N1", "N2")
    coord.add_segment("S3", "N2", "N0")
    coord.add_segment("H0", "N9", "N0")
    coord.add_segment("H1", "N8", "N1")
    coord.add_segment("H2", "N7", "N2")
    coord.add_fire_zone("FZ1", segments=["S3"], exits=["S1"])


def ring_with_three_vehicles(coord: RightOfWayCoordinator) -> None:
    build_ring(coord)
    coord.register_vehicle("AGV1", hazard_level=2, battery=0.9, node="N0")
    coord.register_vehicle("AGV2", hazard_level=0, battery=0.8, node="N1")
    coord.register_vehicle("AGV3", hazard_level=1, battery=0.6, node="N2")
    coord.assign_task("AGV1", "T1", "N0", route=["S1", "S2", "S3"], deadline=3000.0)
    coord.assign_task("AGV2", "T2", "N1", route=["S2", "S3", "S1"], deadline=3000.0)
    coord.assign_task("AGV3", "T3", "N2", route=["S3", "S1", "S2"], deadline=3000.0)


def enter_cycle(coord: RightOfWayCoordinator, clock: ManualClock) -> None:
    """三车各占一段并申请下一段，形成等待环。"""
    clock.advance(1)
    for vid, seg in [("AGV1", "S1"), ("AGV2", "S2"), ("AGV3", "S3")]:
        r = coord.request_reservation(f"req-{vid}-{seg}", vid, seg, 1.0, 1000.0)
        assert r["ok"], r
        coord.enter(r["lease"]["lease_id"], vid)
    clock.advance(1)
    for vid, seg in [("AGV1", "S2"), ("AGV2", "S3"), ("AGV3", "S1")]:
        r = coord.request_reservation(f"req-{vid}-{seg}", vid, seg, 2.0, 1000.0)
        assert r["ok"] and r["lease"]["state"] == "requested", r
