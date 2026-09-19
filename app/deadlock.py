"""死锁检测与解环策略。

等待图：边 w -> h 表示车辆 w 的等待申请被车辆 h 持有的活跃租约挡住。
环即互相等待的死锁。解环动作的选择综合考虑：
- 载荷危险级（越高越不宜挪动）
- 剩余电量（越低越不宜绕行/退让）
- 任务时限（越紧越不宜延迟）
- 退让可行性（控制模式必须为 automatic、可倒车、有空闲退让目标）

硬约束（不可被评分打破）：
- safe_stop / manual 车辆永远不被调度动作挪动（人工接管与安全急停优先）
- 消防区内车辆只能沿指定出口撤离
- 退让目标不得占用任何消防区指定出口（避免堵住消防通道）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import ACTIVE_STATES, ControlMode, Lease, LeaseState, Vehicle

# 评分权重（与 coordinator.compute_priority 同构，文档见 README）
W_HAZARD = 10.0
W_BATTERY = 5.0
W_DEADLINE = 20.0
DEADLINE_HORIZON = 3600.0

# 退让后持有退让位的时长（秒）
RETREAT_HOLD = 120.0
# “等待即可”判定的窗口剩余阈值（秒）
WAIT_HORIZON = 60.0


def find_cycles(nodes: list[str], edges: list[dict]) -> list[list[str]]:
    """枚举等待图中的基本环。规模小（车队量级），用 DFS 即可。"""
    adj: dict[str, set[str]] = {n: set() for n in nodes}
    for e in edges:
        adj.setdefault(e["from"], set()).add(e["to"])

    cycles: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()

    def normalize(cycle: list[str]) -> tuple[str, ...]:
        i = cycle.index(min(cycle))
        return tuple(cycle[i:] + cycle[:i])

    def dfs(start: str, node: str, path: list[str]) -> None:
        for nxt in adj.get(node, ()):
            if nxt == start and len(path) >= 1:
                key = normalize(path)
                if key not in seen:
                    seen.add(key)
                    cycles.append(list(key))
            elif nxt not in path and nxt > start:
                # 只从环上最小节点出发，避免重复枚举
                dfs(start, nxt, path + [nxt])

    for n in nodes:
        dfs(n, n, [n])
    return cycles


def _deadline_urgency(vehicle: Vehicle, now: float) -> float:
    task = vehicle.task
    if not task or task.deadline is None:
        return 0.0
    slack = task.deadline - now
    return W_DEADLINE * (1.0 - max(slack, 0.0) / DEADLINE_HORIZON)


def move_cost(vehicle: Vehicle, now: float) -> float:
    """让某车挪动/延迟的代价：危险高、电量低、时限紧都抬高代价。"""
    return (
        W_HAZARD * vehicle.hazard_level
        + W_BATTERY * (1.0 - vehicle.battery)
        + _deadline_urgency(vehicle, now)
    )


@dataclass
class ResolutionAction:
    kind: str                 # retreat | yield | wait | manual
    vehicle_id: str           # 需要动作的车辆
    detail: dict = field(default_factory=dict)
    cost: float = 0.0
    rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "vehicle_id": self.vehicle_id,
            "detail": self.detail,
            "cost": round(self.cost, 3),
            "rationale": self.rationale,
        }


def propose_resolutions(coordinator, cycle: list[str]) -> list[ResolutionAction]:
    """对一个等待环生成候选解环动作（已过滤硬约束）。"""
    now = coordinator.clock.now()
    topo = coordinator.topology
    actions: list[ResolutionAction] = []

    holders: dict[str, Lease] = {}   # vehicle -> 它在环上持有的租约
    for lease in coordinator.leases.values():
        if lease.state in ACTIVE_STATES and lease.vehicle_id in cycle:
            holders.setdefault(lease.vehicle_id, lease)

    for vid in cycle:
        vehicle = coordinator.vehicles[vid]
        lease = holders.get(vid)
        if lease is None:
            continue
        cost = move_cost(vehicle, now)
        factors = (
            f"危险级={vehicle.hazard_level} 电量={vehicle.battery:.2f} "
            f"时限={vehicle.task.deadline if vehicle.task else None}"
        )

        if vehicle.control_mode != ControlMode.AUTOMATIC:
            # 安全急停/人工接管：绝不自动挪动
            continue

        if lease.state == LeaseState.GRANTED:
            # 尚未进入：直接让出授予即可破环，代价最低
            actions.append(
                ResolutionAction(
                    kind="yield",
                    vehicle_id=vid,
                    detail={"lease_id": lease.lease_id, "resource_id": lease.resource_id},
                    cost=cost * 0.5,
                    rationale=f"未进入可让出 {lease.resource_id}；{factors}",
                )
            )
            continue

        if lease.state != LeaseState.ENTERED:
            continue

        # 已进入：需要退让到空闲路段
        target = retreat_target(coordinator, vehicle)
        if target is not None:
            actions.append(
                ResolutionAction(
                    kind="retreat",
                    vehicle_id=vid,
                    detail={
                        "lease_id": lease.lease_id,
                        "from": lease.resource_id,
                        "to": target,
                    },
                    cost=cost,
                    rationale=f"退让 {lease.resource_id} -> {target}；{factors}",
                )
            )

    # 等待候选：环上若有“已授予未进入”的租约窗口即将结束
    for lease in coordinator.leases.values():
        if (
            lease.vehicle_id in cycle
            and lease.state == LeaseState.GRANTED
            and 0 < lease.end - now <= WAIT_HORIZON
        ):
            actions.append(
                ResolutionAction(
                    kind="wait",
                    vehicle_id=lease.vehicle_id,
                    detail={"lease_id": lease.lease_id, "until": lease.end},
                    cost=1.0,
                    rationale=f"租约 {lease.lease_id} 窗口 {lease.end - now:.0f}s 后结束，可等待",
                )
            )

    if not actions:
        actions.append(
            ResolutionAction(
                kind="manual",
                vehicle_id=cycle[0],
                detail={"cycle": list(cycle)},
                cost=float("inf"),
                rationale="无可行自动解环动作（急停/接管/消防区约束），需人工介入",
            )
        )
    return actions


def retreat_target(coordinator, vehicle: Vehicle) -> str | None:
    """为已进入车辆寻找退让目标。

    候选顺序：路线上一段，然后是当前段起点处可倒车进入的其他路段
    （如避让支线）。目标须空闲、未封闭；区外车辆不得占用消防区指定
    出口，消防区内车辆只能沿指定出口撤离。
    """
    topo = coordinator.topology
    task = vehicle.task
    if not vehicle.can_reverse or vehicle.position is None:
        return None
    current = topo.segment(vehicle.position)
    if current is None:
        return None

    candidates: list[str] = []
    if task is not None and task.passed >= 1:
        candidates.append(task.route[task.passed - 1])
    predecessors = sorted(
        res.resource_id for res in topo.resources.values()
        if hasattr(res, "end_node") and res.end_node == current.start_node
        and res.resource_id != vehicle.position
    )
    candidates += [c for c in predecessors if c not in candidates]

    zone = topo.fire_zone_of(vehicle.position)
    now = coordinator.clock.now()
    hold_end = now + RETREAT_HOLD
    for candidate in candidates:
        res = topo.get(candidate)
        if res is None or res.closed:
            continue
        if zone is not None:
            if candidate not in zone.exits:
                continue  # 区内车辆只能沿指定出口撤离
        elif topo.is_fire_exit(candidate):
            continue  # 区外车辆不得堵消防通道
        occupied = any(
            other.resource_id == candidate
            and other.state in ACTIVE_STATES
            and other.start < hold_end and now < other.end
            for other in coordinator.leases.values()
        )
        if not occupied:
            return candidate
    return None


def choose_action(actions: list[ResolutionAction]) -> ResolutionAction:
    """代价最小者优先，并列时按车辆 id 保证确定性。"""
    return min(actions, key=lambda a: (a.cost, a.vehicle_id, a.kind))


def effective_priorities(coordinator) -> dict[str, float]:
    """优先级继承：被高优先级等待者阻塞的持有者临时继承其优先级。

    用于缓解优先级反转；沿等待边反向迭代至不动点（环收敛到环内最大值）。
    """
    base: dict[str, float] = {}
    for vid, vehicle in coordinator.vehicles.items():
        own = [l.priority for l in coordinator.leases.values()
               if l.vehicle_id == vid and l.state in ACTIVE_STATES | {LeaseState.REQUESTED}]
        base[vid] = max(own) if own else 0.0

    waiters_of: dict[str, set[str]] = {}
    for edge in coordinator.wait_edges():
        waiters_of.setdefault(edge["to"], set()).add(edge["from"])

    eff = dict(base)
    for _ in range(len(coordinator.vehicles) + 1):
        changed = False
        for holder, waiters in waiters_of.items():
            inherited = max((eff[w] for w in waiters), default=0.0)
            if inherited > eff.get(holder, 0.0):
                eff[holder] = inherited
                changed = True
        if not changed:
            break
    return eff
