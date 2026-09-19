"""示例拓扑与预置场景，供 API、CLI 与测试使用。

拓扑（窄巷单向资源，F1/F2 为消防区，B* 为避让位）：

    S1 -- A1 -- A2 -- A3 -- D1
     |              |
    B1(bay)       F1 -- F2 -- X1(指定出口)
                    |
    S2 -- C1 -- C2 -- C3 -- D2

三台 AGV 的经典等待环：V1 占 A2 申请 A3，V2 占 A3 申请 C3 方向的 C2，
V3 占 C2 申请 A2（经路口 C3/A3 区域）。示例中用资源环
``A2 -> A3 -> C2 -> A2`` 构造互相等待。
"""

from __future__ import annotations

from .clock import SimClock
from .models import FireZone, Resource, ResourceKind, VehicleSpec
from .service import RightOfWayService
from .topology import Topology


def build_topology() -> Topology:
    topo = Topology()
    ids = {
        "S1": ResourceKind.DEPOT, "S2": ResourceKind.DEPOT,
        "A1": ResourceKind.SEGMENT, "A2": ResourceKind.SEGMENT,
        "A3": ResourceKind.SEGMENT,
        "C1": ResourceKind.SEGMENT, "C2": ResourceKind.SEGMENT,
        "C3": ResourceKind.SEGMENT,
        "B1": ResourceKind.BAY, "B2": ResourceKind.BAY,
        "F1": ResourceKind.SEGMENT, "F2": ResourceKind.SEGMENT,
        "X1": ResourceKind.SEGMENT,
        "R1": ResourceKind.SEGMENT,
        "D1": ResourceKind.DEPOT, "D2": ResourceKind.DEPOT,
        "J1": ResourceKind.INTERSECTION,
    }
    for rid, kind in ids.items():
        topo.add_resource(Resource(rid, kind))
    edges = [
        ("S1", "A1"), ("A1", "A2"), ("A2", "A3"), ("A3", "D1"),
        ("A1", "B1"),
        ("S2", "C1"), ("C1", "C2"), ("C2", "C3"), ("C3", "D2"),
        ("C2", "B2"),
        ("A3", "J1"), ("J1", "C3"), ("J1", "F1"),
        ("A2", "C2"), ("A3", "C2"),   # 构成死锁环的关键邻接
        ("A2", "R1"), ("R1", "D1"), ("R1", "C3"),  # 绕行车道
        ("F1", "F2"), ("F2", "X1"),
    ]
    for a, b in edges:
        topo.connect(a, b)
    topo.add_fire_zone(FireZone("FZ", frozenset({"F1", "F2", "X1"}), "X1"))
    return topo


def build_service(*, auto_resolve: bool = False,
                  start: float = 1000.0) -> tuple[RightOfWayService, SimClock]:
    clock = SimClock(start)
    svc = RightOfWayService(build_topology(), clock=clock,
                            grant_ttl=30.0, heartbeat_timeout=12.0,
                            auto_resolve=auto_resolve)
    specs = [
        VehicleSpec("V1", hazard=0, battery=82.0, deadline=start + 600,
                    retreat_cost=5.0),
        VehicleSpec("V2", hazard=2, battery=18.0, deadline=start + 120,
                    retreat_cost=5.0),
        VehicleSpec("V3", hazard=1, battery=91.0, deadline=None,
                    retreat_cost=5.0),
    ]
    for s in specs:
        svc.register_vehicle(s)
    return svc, clock


def seed_three_way_cycle(svc: RightOfWayService, clock: SimClock) -> dict:
    """布置三台 AGV：各占一段窄巷并申请环上的下一路段。"""
    layout = [
        ("V1", ("A1", "A2", "A3", "J1", "C3", "D2"), "A2", "A3"),
        ("V2", ("S2", "C1", "C2", "A2", "A3", "D1"), "C2", "A2"),
        ("V3", ("S1", "A1", "A2", "C2", "A3", "D1"), "A3", "C2"),
    ]
    leases = {}
    t = clock.now()
    for i, (vid, route, hold, want) in enumerate(layout):
        svc.submit_plan(vid, resources=route, reason="初始任务路线")
        g = svc.request_resource(vid, hold, request_id=f"seed-{vid}-hold")
        svc.enter_resource(vid, g["lease_id"])
        leases[vid] = {"hold": hold, "want": want}
        t += 1
        clock.set(t)
    t += 1
    clock.set(t)
    for vid, _route, _hold, want in layout:
        r = svc.request_resource(vid, want, request_id=f"seed-{vid}-want")
        leases[vid]["request_status"] = r["status"]
    return leases
