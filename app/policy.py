"""等待图、环检测与解环决策。

等待图边 ``A -> B`` 表示：A 申请的下一资源被 B 的有效租约挡住。
解环只在真实等待环上触发；动作选择综合：

- 载荷危险级（高危车不倒车退让）
- 剩余电量（退让耗电必须付得起）
- 任务时限（时限紧的车不让）
- 退让可行性（身后有空闲资源/避让位，且不在消防区逆行）

安全急停与人工接管的车辆不参与自动解环；其存在本身意味着必须上报人工。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .fleet import Fleet, VehicleRuntime
from .ledger import Ledger
from .models import ControlMode, LeaseState
from .topology import Topology


@dataclass
class WaitEdge:
    waiter: str
    blocker: str
    resource: str
    blocker_state: str


@dataclass
class Cycle:
    vehicles: tuple[str, ...]
    edges: tuple[WaitEdge, ...]


@dataclass
class ActionScore:
    vehicle_id: str
    feasible: bool
    score: float
    factors: dict
    retreat_to: Optional[str] = None
    releases: Optional[str] = None
    infeasible_reason: Optional[str] = None


@dataclass
class Resolution:
    cycle: tuple[str, ...]
    action: str                    # retreat | evacuate | human_assist
    target_vehicle: Optional[str]
    retreat_to: Optional[str]
    releases: Optional[str]
    rationale: str
    scores: list[dict] = field(default_factory=list)


class WaitGraph:
    def __init__(self, ledger: Ledger, fleet: Fleet) -> None:
        self.ledger = ledger
        self.fleet = fleet
        self.edges: list[WaitEdge] = []

    def rebuild(self, next_requests: dict[str, str]) -> "WaitGraph":
        """``next_requests``: vehicle_id -> 它正在申请（requested 租约）的资源。"""
        self.edges = []
        for waiter, rid in next_requests.items():
            holders = [l for l in self.ledger.active_on(rid)
                       if l.vehicle_id != waiter]
            for l in holders:
                self.edges.append(WaitEdge(
                    waiter=waiter, blocker=l.vehicle_id,
                    resource=rid, blocker_state=l.state.value,
                ))
        return self

    def adjacency(self) -> dict[str, list[WaitEdge]]:
        out: dict[str, list[WaitEdge]] = {}
        for e in self.edges:
            out.setdefault(e.waiter, []).append(e)
        return out

    def cycles(self) -> list[Cycle]:
        adj = self.adjacency()
        found: list[Cycle] = []
        seen_signatures: set[tuple[str, ...]] = set()

        def dfs(start: str, node: str, stack: list[str], estack: list[WaitEdge]):
            for e in adj.get(node, []):
                if e.blocker == start and len(stack) >= 1:
                    cyc = tuple(stack)
                    sig = tuple(sorted(cyc))
                    if sig not in seen_signatures:
                        seen_signatures.add(sig)
                        found.append(Cycle(vehicles=cyc, edges=tuple(estack + [e])))
                elif e.blocker not in stack and e.blocker in adj:
                    dfs(start, e.blocker, stack + [e.blocker], estack + [e])

        for v in sorted(adj):
            dfs(v, v, [v], [])
        return found

    def snapshot(self) -> dict:
        return {
            "nodes": sorted({e.waiter for e in self.edges}
                            | {e.blocker for e in self.edges}),
            "edges": [
                {"from": e.waiter, "to": e.blocker,
                 "resource": e.resource, "blocker_state": e.blocker_state}
                for e in self.edges
            ],
        }


class ResolutionPolicy:
    """给等待环上的每台车打分，选出退让代价最小、最安全的解环动作。"""

    HAZARD_WEIGHT = 1000.0     # 危险级主导：高危车原则上不倒退
    BATTERY_RESERVE = 15.0     # 退让后必须保留的电量

    def __init__(self, topology: Topology, ledger: Ledger, fleet: Fleet):
        self.topo = topology
        self.ledger = ledger
        self.fleet = fleet

    # ---- 退让可行性 ---------------------------------------------------------

    def retreat_target(self, rt: VehicleRuntime, held_resource: str,
                       plan_resources: tuple[str, ...], progress: int) -> Optional[str]:
        if rt.in_fire_zone:
            return None                                # 消防区内只能向出口撤离
        candidates: list[str] = []
        if progress > 0:
            candidates.append(plan_resources[progress - 1])
        for n in self.topo.neighbors(held_resource):
            res = self.topo.resources.get(n)
            if res is not None and res.kind.value == "bay":
                candidates.append(n)
        for c in candidates:
            res = self.topo.resources.get(c)
            if res is None or res.closed or self.topo.in_fire_zone(c):
                continue
            blockers = [l for l in self.ledger.active_on(c)
                        if l.vehicle_id != rt.id]
            if not blockers:
                return c
        return None

    # ---- 单车打分 -----------------------------------------------------------

    def score(self, rt: VehicleRuntime, held_resource: str,
              plan_resources: tuple[str, ...], progress: int, now: float) -> ActionScore:
        factors: dict = {}
        # 安全模式不可被自动调度要求退让
        if rt.control != ControlMode.AUTOMATIC:
            return ActionScore(
                rt.id, False, float("inf"), {"control": rt.control.value},
                infeasible_reason=f"车辆处于 {rt.control.value}，需人工处置",
            )
        if not rt.online or rt.fault:
            return ActionScore(
                rt.id, False, float("inf"),
                {"online": rt.online, "fault": rt.fault},
                infeasible_reason="车辆离线或故障，无法自动退让",
            )

        target = self.retreat_target(rt, held_resource, plan_resources, progress)
        if target is None:
            reason = ("消防区内禁止倒车退让，只能按指定出口撤离" if rt.in_fire_zone
                      else "身后无空闲资源或避让位")
            return ActionScore(
                rt.id, False, float("inf"),
                {"hazard": rt.spec.hazard, "battery": rt.spec.battery},
                infeasible_reason=reason,
            )

        cost = rt.spec.retreat_cost
        if rt.spec.battery < cost + self.BATTERY_RESERVE:
            return ActionScore(
                rt.id, False, float("inf"), {"battery": rt.spec.battery},
                infeasible_reason=(
                    f"电量 {rt.spec.battery:.0f}% 不足以支付退让 {cost:.0f}%"
                    f"并保留 {self.BATTERY_RESERVE:.0f}% 余量"),
            )

        hazard_c = rt.spec.hazard * self.HAZARD_WEIGHT
        battery_c = (100.0 - rt.spec.battery)          # 电越多越适合作为退让车
        if rt.spec.deadline is not None:
            slack = max(0.0, rt.spec.deadline - now)
            deadline_c = -slack / 60.0                 # 时限越松，越适合退让
            factors["deadline_slack_s"] = round(slack, 1)
        else:
            deadline_c = 0.0
        factors.update(hazard=rt.spec.hazard,
                       battery=rt.spec.battery,
                       retreat_cost=cost,
                       hazard_cost=hazard_c,
                       battery_cost=battery_c,
                       deadline_cost=round(deadline_c, 2))
        total = hazard_c + battery_c + deadline_c
        return ActionScore(
            rt.id, True, total, factors,
            retreat_to=target, releases=held_resource)

    # ---- 整环决策 -----------------------------------------------------------

    def resolve(self, cycle: Cycle, plans: dict[str, tuple[tuple[str, ...], int]],
                now: float) -> Resolution:
        scores: list[ActionScore] = []
        held: dict[str, str] = {}
        for e in cycle.edges:
            held[e.waiter] = e.resource
        for vid in cycle.vehicles:
            rt = self.fleet.get(vid)
            resources, progress = plans.get(vid, ((), -1))
            current = resources[progress] if 0 <= progress < len(resources) else \
                self._current_resource(vid)
            if current is None:
                scores.append(ActionScore(
                    vid, False, float("inf"), {},
                    infeasible_reason="无法确认车辆当前位置"))
                continue
            scores.append(self.score(rt, current, resources, progress, now))

        feasible = [s for s in scores if s.feasible]
        score_dicts = [
            {"vehicle_id": s.vehicle_id, "feasible": s.feasible,
             "score": None if s.score == float("inf") else round(s.score, 2),
             "factors": s.factors, "retreat_to": s.retreat_to,
             "releases": s.releases, "infeasible_reason": s.infeasible_reason}
            for s in scores
        ]

        safety_block = [s for s in scores if not s.feasible
                        and (self.fleet.get(s.vehicle_id).control != ControlMode.AUTOMATIC
                             or not self.fleet.get(s.vehicle_id).online
                             or self.fleet.get(s.vehicle_id).fault
                             or self.fleet.get(s.vehicle_id).in_fire_zone)]
        if not feasible:
            who = next((s for s in scores
                        if self.fleet.get(s.vehicle_id).in_fire_zone), None)
            if who is not None:
                rt = self.fleet.get(who.vehicle_id)
                return Resolution(
                    cycle.vehicles, "evacuate_fire_zone", who.vehicle_id,
                    rt.fire_exit, held.get(who.vehicle_id),
                    "消防区内车辆只能沿指定出口撤离，须人工疏导其余车辆让行",
                    score_dicts)
            return Resolution(
                cycle.vehicles, "human_assist", None, None, None,
                "环上无车辆具备安全退让条件，请求人工现场处置（牵引/封控）",
                score_dicts)

        chosen = min(feasible, key=lambda s: s.score)
        # 安全模式车辆即便被打分也不能自动选中（上面已过滤，双保险）
        victim = self.fleet.get(chosen.vehicle_id)
        assert victim.control == ControlMode.AUTOMATIC and victim.online
        rationale = (
            f"选择 {chosen.vehicle_id} 退让至 {chosen.retreat_to}："
            f"危险级 {victim.spec.hazard}、电量 {victim.spec.battery:.0f}%"
            + (f"、时限余量 {victim.spec.deadline - now:.0f}s"
               if victim.spec.deadline else "、无任务时限")
            + "；其余车辆退让不可行或代价更高。")
        return Resolution(cycle.vehicles, "retreat", chosen.vehicle_id,
                          chosen.retreat_to, chosen.releases, rationale, score_dicts)

    def _current_resource(self, vid: str) -> Optional[str]:
        leases = self.ledger.vehicle_leases(
            vid, frozenset({LeaseState.ENTERED, LeaseState.UNCERTAIN}))
        return leases[0].resource_id if leases else None
