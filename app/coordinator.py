"""路权协调器：时窗预约、租约生命周期、等待环检测与解环、局部重规划。

安全要点：
- 车辆只能凭有效租约（granted 且窗口未过）进入下一资源。
- 预约按 request_id 幂等：通信重试返回原预约（或原拒绝）。
- 租约超时一律先转 uncertain 并等待位置确认；只有确认车辆不在资源上
  才允许 expired 释放。已 granted 但从未进入的租约窗口过期可直接
  expired（车辆从未物理占用）。
- safe_stop / manual 车辆的租约不可被抢占，解环动作也不挪动它们。
"""

from __future__ import annotations

from .clock import Clock, ManualClock
from .deadlock import (
    RETREAT_HOLD,
    ResolutionAction,
    choose_action,
    effective_priorities,
    find_cycles,
    move_cost,
    propose_resolutions,
)
from .events import Event, EventLog
from .models import (
    ACTIVE_STATES,
    ControlMode,
    Lease,
    LeaseState,
    Rejection,
    RejectReason,
    RouteRevision,
    Task,
    Vehicle,
)
from .topology import Topology

# 抢占门槛：申请方优先级须高出持有者该值，避免来回抖动
PREEMPT_MARGIN = 1.0
MAX_RESOLUTION_ROUNDS = 8


class RightOfWayCoordinator:
    def __init__(
        self,
        topology: Topology,
        clock: Clock,
        heartbeat_ttl: float = 30.0,
        event_log: EventLog | None = None,
    ):
        self.topology = topology
        self.clock = clock
        self.heartbeat_ttl = heartbeat_ttl
        self.events = event_log or EventLog()
        self.vehicles: dict[str, Vehicle] = {}
        self.leases: dict[str, Lease] = {}
        self.rejections: list[Rejection] = []
        self.resolution_log: list[dict] = []
        self._request_index: dict[str, str] = {}       # request_id -> lease_id
        self._rejection_index: dict[str, Rejection] = {}
        self._lease_seq = 0
        self._heartbeat_lost: set[str] = set()
        self._active_cycles: set[frozenset[str]] = set()
        self.blocked_segments: set[str] = set()        # 故障车等动态阻塞

    # ------------------------------------------------------------------
    # 拓扑构建（命令事件，可回放）
    # ------------------------------------------------------------------

    def add_segment(self, segment_id, start_node, end_node, length=1.0, capacity=1):
        seg = self.topology.add_segment(segment_id, start_node, end_node, length, capacity)
        self.events.record(
            self.clock.now(), "segment_added",
            segment_id=segment_id, start_node=start_node, end_node=end_node,
            length=length, capacity=capacity,
        )
        return seg

    def add_intersection(self, resource_id, capacity=1):
        res = self.topology.add_intersection(resource_id, capacity)
        self.events.record(
            self.clock.now(), "intersection_added",
            resource_id=resource_id, capacity=capacity,
        )
        return res

    def add_fire_zone(self, zone_id, segments, exits):
        zone = self.topology.add_fire_zone(zone_id, list(segments), list(exits))
        self.events.record(
            self.clock.now(), "fire_zone_added",
            zone_id=zone_id, segments=list(segments), exits=list(exits),
        )
        return zone

    # ------------------------------------------------------------------
    # 车辆与任务
    # ------------------------------------------------------------------

    def register_vehicle(
        self,
        vehicle_id: str,
        can_reverse: bool = True,
        hazard_level: int = 0,
        battery: float = 1.0,
        position: str | None = None,
        node: str | None = None,
    ) -> Vehicle:
        vehicle = Vehicle(
            vehicle_id=vehicle_id,
            can_reverse=can_reverse,
            hazard_level=hazard_level,
            battery=battery,
            position=position,
            node=node,
            last_heartbeat=self.clock.now(),
        )
        self.vehicles[vehicle_id] = vehicle
        self.events.record(
            self.clock.now(), "vehicle_registered",
            vehicle_id=vehicle_id, can_reverse=can_reverse,
            hazard_level=hazard_level, battery=battery,
            position=position, node=node,
        )
        return vehicle

    def assign_task(
        self,
        vehicle_id: str,
        task_id: str,
        goal_node: str,
        route: list[str] | None = None,
        deadline: float | None = None,
    ) -> Task | None:
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            return None
        rationale = []
        if route is None:
            start = self._vehicle_node(vehicle)
            route = self.topology.shortest_path(start, goal_node) if start else None
            if route is None:
                return None
            rationale.append(f"初始规划 {start}->{goal_node}: {'>'.join(route)}")
        else:
            route = list(route)
            rationale.append(f"任务事件给定路线: {'>'.join(route)}")
        task = Task(task_id=task_id, goal_node=goal_node, route=route,
                    deadline=deadline, rationale=rationale)
        vehicle.task = task
        self.events.record(
            self.clock.now(), "task_assigned",
            vehicle_id=vehicle_id, task_id=task_id, goal_node=goal_node,
            route=list(route), deadline=deadline,
        )
        return task

    def _vehicle_node(self, vehicle: Vehicle) -> str | None:
        if vehicle.position:
            seg = self.topology.segment(vehicle.position)
            if seg:
                return seg.end_node
        return vehicle.node

    def compute_priority(self, vehicle: Vehicle) -> float:
        """优先级 = 危险级、电量、时限的加权和（与解环代价同构）。"""
        return move_cost(vehicle, self.clock.now())

    # ------------------------------------------------------------------
    # 预约（幂等）
    # ------------------------------------------------------------------

    def request_reservation(
        self, request_id: str, vehicle_id: str, resource_id: str,
        start: float, end: float,
    ) -> dict:
        # 幂等：同一 request_id 的重试返回原预约/原拒绝，不产生新记录
        if request_id in self._request_index:
            lease = self.leases[self._request_index[request_id]]
            return {"ok": True, "lease": lease.to_dict(), "idempotent_replay": True}
        if request_id in self._rejection_index:
            rej = self._rejection_index[request_id]
            return {"ok": False, "rejection": rej.to_dict(), "idempotent_replay": True}

        now = self.clock.now()
        self.events.record(
            now, "reservation_requested",
            request_id=request_id, vehicle_id=vehicle_id,
            resource_id=resource_id, start=start, end=end,
        )

        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            return self._reject(request_id, vehicle_id, resource_id,
                                RejectReason.UNKNOWN_VEHICLE, "车辆未注册")
        resource = self.topology.get(resource_id)
        if resource is None:
            return self._reject(request_id, vehicle_id, resource_id,
                                RejectReason.UNKNOWN_RESOURCE, "资源不存在")
        if resource.closed:
            return self._reject(request_id, vehicle_id, resource_id,
                                RejectReason.RESOURCE_CLOSED, "资源已封闭")
        if end <= start or end <= now:
            return self._reject(request_id, vehicle_id, resource_id,
                                RejectReason.INVALID_WINDOW,
                                f"窗口[{start},{end})无效或已过期")
        if vehicle.control_mode != ControlMode.AUTOMATIC:
            return self._reject(request_id, vehicle_id, resource_id,
                                RejectReason.CONTROL_MODE,
                                f"车辆处于{vehicle.control_mode.value}，安全急停/人工接管优先")
        if vehicle.fault:
            return self._reject(request_id, vehicle_id, resource_id,
                                RejectReason.VEHICLE_FAULT, "车辆故障")
        # 消防区：区内车辆只能沿指定出口撤离
        if vehicle.position:
            zone = self.topology.fire_zone_of(vehicle.position)
            if (zone is not None and resource_id != vehicle.position
                    and resource_id not in zone.exits):
                return self._reject(
                    request_id, vehicle_id, resource_id,
                    RejectReason.FIRE_ZONE_EXIT_ONLY,
                    f"车辆位于消防区{zone.zone_id}，只能经指定出口{list(zone.exits)}撤离",
                )

        self._lease_seq += 1
        lease = Lease(
            lease_id=f"L{self._lease_seq}",
            request_id=request_id,
            vehicle_id=vehicle_id,
            resource_id=resource_id,
            start=start,
            end=end,
            priority=self.compute_priority(vehicle),
        )
        self.leases[lease.lease_id] = lease
        self._request_index[request_id] = lease.lease_id
        self._try_grant(lease)
        return {"ok": True, "lease": lease.to_dict(), "idempotent_replay": False}

    def _reject(self, request_id, vehicle_id, resource_id, reason, detail) -> dict:
        rej = Rejection(request_id=request_id, vehicle_id=vehicle_id,
                        resource_id=resource_id, reason=reason.value,
                        detail=detail, at=self.clock.now())
        self.rejections.append(rej)
        self._rejection_index[request_id] = rej
        self.events.record(
            self.clock.now(), "reservation_rejected",
            request_id=request_id, vehicle_id=vehicle_id,
            resource_id=resource_id, reason=reason.value, detail=detail,
        )
        return {"ok": False, "rejection": rej.to_dict(), "idempotent_replay": False}

    def _conflicts(self, lease: Lease) -> list[Lease]:
        return [
            other for other in self.leases.values()
            if other.lease_id != lease.lease_id
            and other.resource_id == lease.resource_id
            and other.state in ACTIVE_STATES
            and other.overlaps(lease)
        ]

    def _try_grant(self, lease: Lease) -> bool:
        conflicts = self._conflicts(lease)
        now = self.clock.now()
        if conflicts:
            preemptable = all(
                c.state == LeaseState.GRANTED
                and self.vehicles[c.vehicle_id].control_mode == ControlMode.AUTOMATIC
                and not self.vehicles[c.vehicle_id].fault
                and lease.priority > c.priority + PREEMPT_MARGIN
                for c in conflicts
            )
            if not preemptable:
                self.events.record(
                    now, "reservation_waiting",
                    lease_id=lease.lease_id, request_id=lease.request_id,
                    vehicle_id=lease.vehicle_id, resource_id=lease.resource_id,
                    blocked_by=[c.lease_id for c in conflicts],
                )
                return False
            for c in conflicts:  # 只抢占“已授予未进入”的租约
                c.state = LeaseState.REQUESTED
                c.note = f"被更高优先级申请 {lease.lease_id} 抢占"
                self.events.record(
                    now, "reservation_preempted",
                    lease_id=c.lease_id, by_lease_id=lease.lease_id,
                    vehicle_id=c.vehicle_id, resource_id=c.resource_id,
                )
        lease.state = LeaseState.GRANTED
        lease.granted_at = now
        self.events.record(
            now, "reservation_granted",
            lease_id=lease.lease_id, request_id=lease.request_id,
            vehicle_id=lease.vehicle_id, resource_id=lease.resource_id,
            start=lease.start, end=lease.end, priority=round(lease.priority, 3),
        )
        return True

    def _retry_waiting(self, resource_id: str) -> None:
        """资源释放后按有效优先级（含继承）重试等待中的申请。"""
        now = self.clock.now()
        eff = effective_priorities(self)
        waiting = [
            l for l in self.leases.values()
            if l.state == LeaseState.REQUESTED
            and l.resource_id == resource_id
            and l.end > now
        ]
        waiting.sort(key=lambda l: (-eff.get(l.vehicle_id, l.priority), l.lease_id))
        for lease in waiting:
            self._try_grant(lease)

    # ------------------------------------------------------------------
    # 进入 / 释放
    # ------------------------------------------------------------------

    def enter(self, lease_id: str, vehicle_id: str) -> dict:
        lease = self.leases.get(lease_id)
        now = self.clock.now()
        if lease is None or lease.vehicle_id != vehicle_id:
            return {"ok": False, "error": "租约不存在或车辆不匹配"}
        if lease.state != LeaseState.GRANTED:
            return {"ok": False, "error": f"租约状态{lease.state.value}不可进入"}
        if now >= lease.end:
            return {"ok": False, "error": "预约窗口已过，需重新申请"}
        lease.state = LeaseState.ENTERED
        lease.entered_at = now
        vehicle = self.vehicles[vehicle_id]
        vehicle.position = lease.resource_id
        vehicle.node = None
        self.events.record(
            now, "lease_entered",
            lease_id=lease_id, vehicle_id=vehicle_id, resource_id=lease.resource_id,
        )
        return {"ok": True, "lease": lease.to_dict()}

    def release(self, lease_id: str, vehicle_id: str) -> dict:
        lease = self.leases.get(lease_id)
        now = self.clock.now()
        if lease is None or lease.vehicle_id != vehicle_id:
            return {"ok": False, "error": "租约不存在或车辆不匹配"}
        if lease.state not in (LeaseState.GRANTED, LeaseState.ENTERED):
            return {"ok": False, "error": f"租约状态{lease.state.value}不可释放"}
        was_entered = lease.state == LeaseState.ENTERED
        lease.state = LeaseState.RELEASED
        lease.released_at = now
        vehicle = self.vehicles[vehicle_id]
        if was_entered:
            seg = self.topology.segment(lease.resource_id)
            if seg:
                vehicle.node = seg.end_node
            vehicle.position = None
            task = vehicle.task
            if task and task.passed < len(task.route) \
                    and task.route[task.passed] == lease.resource_id:
                task.passed += 1
        self.events.record(
            now, "lease_released",
            lease_id=lease_id, vehicle_id=vehicle_id,
            resource_id=lease.resource_id, was_entered=was_entered,
        )
        self._retry_waiting(lease.resource_id)
        return {"ok": True, "lease": lease.to_dict()}

    # ------------------------------------------------------------------
    # 心跳、超时与位置确认
    # ------------------------------------------------------------------

    def heartbeat(self, vehicle_id: str) -> dict:
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            return {"ok": False, "error": "车辆未注册"}
        vehicle.last_heartbeat = self.clock.now()
        self._heartbeat_lost.discard(vehicle_id)
        self.events.record(self.clock.now(), "heartbeat_received", vehicle_id=vehicle_id)
        return {"ok": True}

    def tick(self) -> None:
        """推进超时检测与死锁处理。派生事件，不作为回放命令。"""
        now = self.clock.now()
        # 1) 心跳超时：活跃租约转 uncertain（不释放资源）
        for vehicle in self.vehicles.values():
            active = [
                l for l in self.leases.values()
                if l.vehicle_id == vehicle.vehicle_id and l.state in ACTIVE_STATES
            ]
            if not active:
                continue
            if now - vehicle.last_heartbeat > self.heartbeat_ttl:
                if vehicle.vehicle_id not in self._heartbeat_lost:
                    self._heartbeat_lost.add(vehicle.vehicle_id)
                    self.events.record(now, "heartbeat_lost", vehicle_id=vehicle.vehicle_id)
                for lease in active:
                    self._to_uncertain(lease, "heartbeat_lost")
        # 2) 窗口超时
        for lease in list(self.leases.values()):
            if lease.end > now:
                continue
            if lease.state == LeaseState.ENTERED:
                # 已进入但窗口已过：车辆可能仍在资源上，先确认位置
                self._to_uncertain(lease, "window_exceeded")
            elif lease.state in (LeaseState.GRANTED, LeaseState.REQUESTED):
                # 从未进入：资源从未被物理占用，可安全终结
                lease.state = LeaseState.EXPIRED
                lease.released_at = now
                self.events.record(
                    now, "lease_expired", lease_id=lease.lease_id,
                    vehicle_id=lease.vehicle_id, resource_id=lease.resource_id,
                    reason="window_unused",
                )
                self._retry_waiting(lease.resource_id)
        # 3) 死锁检测与解环
        self.detect_and_resolve_deadlocks()

    def _to_uncertain(self, lease: Lease, reason: str) -> None:
        if lease.state not in (LeaseState.GRANTED, LeaseState.ENTERED):
            return
        lease.resume_state = lease.state
        lease.state = LeaseState.UNCERTAIN
        self.events.record(
            self.clock.now(), "lease_uncertain",
            lease_id=lease.lease_id, vehicle_id=lease.vehicle_id,
            resource_id=lease.resource_id, reason=reason,
        )

    def confirm_position(
        self, vehicle_id: str, segment_id: str | None = None, node_id: str | None = None
    ) -> dict:
        """位置确认：uncertain 租约按确认结果重建或终结。"""
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            return {"ok": False, "error": "车辆未注册"}
        now = self.clock.now()
        vehicle.position = segment_id
        vehicle.node = node_id
        vehicle.last_heartbeat = now
        self._heartbeat_lost.discard(vehicle_id)
        self.events.record(
            now, "position_confirmed",
            vehicle_id=vehicle_id, segment=segment_id, node=node_id,
        )
        for lease in list(self.leases.values()):
            if lease.vehicle_id != vehicle_id or lease.state != LeaseState.UNCERTAIN:
                continue
            if segment_id is not None and lease.resource_id == segment_id:
                lease.state = lease.resume_state or LeaseState.ENTERED
                lease.resume_state = None
                self.events.record(
                    now, "lease_reestablished",
                    lease_id=lease.lease_id, vehicle_id=vehicle_id,
                    resource_id=lease.resource_id, state=lease.state.value,
                )
            else:
                # 确认车辆不在该资源上，才允许释放
                lease.state = LeaseState.EXPIRED
                lease.released_at = now
                self.events.record(
                    now, "lease_expired",
                    lease_id=lease.lease_id, vehicle_id=vehicle_id,
                    resource_id=lease.resource_id, reason="confirmed_elsewhere",
                )
                self._retry_waiting(lease.resource_id)
        return {"ok": True, "vehicle": vehicle.to_dict()}

    # ------------------------------------------------------------------
    # 控制模式与故障
    # ------------------------------------------------------------------

    def set_control_mode(self, vehicle_id: str, mode: str) -> dict:
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            return {"ok": False, "error": "车辆未注册"}
        vehicle.control_mode = ControlMode(mode)
        self.events.record(
            self.clock.now(), "control_mode_changed",
            vehicle_id=vehicle_id, mode=mode,
        )
        return {"ok": True, "vehicle": vehicle.to_dict()}

    def vehicle_fault(self, vehicle_id: str) -> dict:
        """车辆故障：租约转 uncertain 等待位置确认，他人绕开其所在段重规划。"""
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            return {"ok": False, "error": "车辆未注册"}
        now = self.clock.now()
        vehicle.fault = True
        self.events.record(now, "vehicle_fault", vehicle_id=vehicle_id,
                           position=vehicle.position)
        for lease in list(self.leases.values()):
            if lease.vehicle_id != vehicle_id:
                continue
            if lease.state in (LeaseState.GRANTED, LeaseState.ENTERED):
                self._to_uncertain(lease, "vehicle_fault")
            elif lease.state == LeaseState.REQUESTED:
                lease.state = LeaseState.EXPIRED
                lease.released_at = now
                self.events.record(
                    now, "lease_expired", lease_id=lease.lease_id,
                    vehicle_id=vehicle_id, resource_id=lease.resource_id,
                    reason="vehicle_fault",
                )
        if vehicle.position:
            self.blocked_segments.add(vehicle.position)
            self._replan_around(vehicle.position, exclude=vehicle_id,
                                reason=f"vehicle_fault:{vehicle_id}")
        return {"ok": True, "vehicle": vehicle.to_dict()}

    # ------------------------------------------------------------------
    # 路段封闭与局部重规划
    # ------------------------------------------------------------------

    def close_segment(self, resource_id: str) -> dict:
        self.topology.close(resource_id)
        self.events.record(self.clock.now(), "segment_closed", resource_id=resource_id)
        self._replan_around(resource_id, exclude=None,
                            reason=f"segment_closed:{resource_id}")
        return {"ok": True}

    def open_segment(self, resource_id: str) -> dict:
        self.topology.open(resource_id)
        self.events.record(self.clock.now(), "segment_opened", resource_id=resource_id)
        return {"ok": True}

    def _replan_around(self, resource_id: str, exclude: str | None, reason: str) -> None:
        for vehicle in self.vehicles.values():
            if vehicle.vehicle_id == exclude:
                continue
            task = vehicle.task
            if task and resource_id in task.remaining:
                self.replan(vehicle.vehicle_id, avoid=set(self.blocked_segments),
                            reason=reason)

    def replan(self, vehicle_id: str, avoid: set[str], reason: str) -> list[str] | None:
        """仅重规划尚未通过的路段；已通过前缀与原路线（含理由）保留。"""
        vehicle = self.vehicles[vehicle_id]
        task = vehicle.task
        now = self.clock.now()
        if task is None:
            return None
        start = self._vehicle_node(vehicle)
        if start is None:
            self.events.record(now, "route_replan_failed",
                               vehicle_id=vehicle_id, reason="position_unknown")
            return None
        avoid = set(avoid) | {s for s in self.topology.resources
                              if self.topology.is_closed(s)}
        new_tail = self.topology.shortest_path(start, task.goal_node, frozenset(avoid))
        if new_tail is None:
            self.events.record(now, "route_replan_failed",
                               vehicle_id=vehicle_id, reason=reason)
            task.rationale.append(f"{now:.0f} 重规划失败({reason})：无可达路径")
            return None
        old_tail = task.remaining
        task.route = task.route[: task.passed] + new_tail
        task.revisions.append(RouteRevision(at=now, reason=reason,
                                            old_tail=old_tail, new_tail=new_tail))
        task.rationale.append(
            f"{now:.0f} 因{reason}重规划：保留已通过{task.passed}段，"
            f"{'/'.join(old_tail)} -> {'/'.join(new_tail)}"
        )
        self.events.record(
            now, "route_replanned", vehicle_id=vehicle_id, reason=reason,
            kept_prefix=task.route[: task.passed],
            old_tail=old_tail, new_tail=new_tail,
        )
        return new_tail

    # ------------------------------------------------------------------
    # 等待图与死锁
    # ------------------------------------------------------------------

    def wait_edges(self) -> list[dict]:
        edges = []
        for lease in self.leases.values():
            if lease.state != LeaseState.REQUESTED:
                continue
            for other in self._conflicts(lease):
                edges.append({
                    "from": lease.vehicle_id,
                    "to": other.vehicle_id,
                    "resource": lease.resource_id,
                    "request_id": lease.request_id,
                })
        return edges

    def wait_graph(self) -> dict:
        edges = self.wait_edges()
        nodes = sorted({e["from"] for e in edges} | {e["to"] for e in edges})
        return {"nodes": nodes, "edges": edges,
                "cycles": find_cycles(nodes, edges)}

    def detect_and_resolve_deadlocks(self) -> list[dict]:
        now = self.clock.now()
        resolved: list[dict] = []
        for _ in range(MAX_RESOLUTION_ROUNDS):
            graph = self.wait_graph()
            cycles = [c for c in graph["cycles"]]
            current = {frozenset(c) for c in cycles}
            self._active_cycles &= current
            new_cycles = [c for c in cycles if frozenset(c) not in self._active_cycles]
            if not new_cycles:
                break
            for cycle in new_cycles:
                self._active_cycles.add(frozenset(cycle))
                self.events.record(now, "deadlock_detected", cycle=list(cycle))
                actions = propose_resolutions(self, cycle)
                action = choose_action(actions)
                record = {"cycle": list(cycle), "action": action.to_dict(),
                          "candidates": [a.to_dict() for a in actions]}
                self.resolution_log.append(record)
                self.events.record(
                    now, "deadlock_resolved", cycle=list(cycle),
                    kind=action.kind, vehicle_id=action.vehicle_id,
                    rationale=action.rationale, detail=action.detail,
                )
                self._apply_action(action)
                resolved.append(record)
        return resolved

    def _apply_action(self, action: ResolutionAction) -> None:
        now = self.clock.now()
        vehicle = self.vehicles[action.vehicle_id]
        if action.kind == "yield":
            lease = self.leases[action.detail["lease_id"]]
            if lease.state == LeaseState.GRANTED:
                lease.state = LeaseState.RELEASED
                lease.released_at = now
                lease.note = "解环让出（未进入）"
                self._retry_waiting(lease.resource_id)
        elif action.kind == "retreat":
            target = action.detail["to"]
            lease = self.leases[action.detail["lease_id"]]
            # 退让位租约：立即授予并进入
            self._lease_seq += 1
            hold = Lease(
                lease_id=f"L{self._lease_seq}",
                request_id=f"retreat-{vehicle.vehicle_id}-{self._lease_seq}",
                vehicle_id=vehicle.vehicle_id, resource_id=target,
                start=now, end=now + RETREAT_HOLD,
                state=LeaseState.ENTERED, granted_at=now, entered_at=now,
                priority=lease.priority, note="解环退让位",
            )
            self.leases[hold.lease_id] = hold
            self._request_index[hold.request_id] = hold.lease_id
            if lease.state == LeaseState.ENTERED:
                lease.state = LeaseState.RELEASED
                lease.released_at = now
                lease.note = f"解环退让至{target}"
            vehicle.position = target
            vehicle.node = None
            if vehicle.task and vehicle.task.passed > 0:
                vehicle.task.passed -= 1
            self._retry_waiting(lease.resource_id)
        # wait / manual：不改变状态，由值守员或后续 tick 处理

    # ------------------------------------------------------------------
    # 值守员视图
    # ------------------------------------------------------------------

    def timeline(self, resource_id: str | None = None) -> dict:
        leases = sorted(self.leases.values(),
                        key=lambda l: (l.resource_id, l.start, l.lease_id))
        out: dict[str, list[dict]] = {}
        for lease in leases:
            if resource_id and lease.resource_id != resource_id:
                continue
            out.setdefault(lease.resource_id, []).append(lease.to_dict())
        return out

    def suggestions(self) -> list[dict]:
        now = self.clock.now()
        out: list[dict] = []
        graph = self.wait_graph()
        for cycle in graph["cycles"]:
            action = choose_action(propose_resolutions(self, cycle))
            out.append({"type": "deadlock_resolution", "cycle": list(cycle),
                        "action": action.to_dict()})
        for lease in self.leases.values():
            if lease.state == LeaseState.UNCERTAIN:
                out.append({
                    "type": "confirm_position",
                    "vehicle_id": lease.vehicle_id,
                    "lease_id": lease.lease_id,
                    "resource_id": lease.resource_id,
                    "detail": "租约超时，释放前须先确认车辆实际位置",
                })
        eff = effective_priorities(self)
        for edge in self.wait_edges():
            holder, waiter = edge["to"], edge["from"]
            own = max((l.priority for l in self.leases.values()
                       if l.vehicle_id == holder and l.state in ACTIVE_STATES),
                      default=0.0)
            if eff.get(holder, 0.0) > own + 1e-9:
                out.append({
                    "type": "priority_inheritance",
                    "holder": holder, "waiter": waiter,
                    "effective_priority": round(eff[holder], 3),
                    "detail": f"{waiter}优先级更高，{holder}临时继承其优先级以缓解反转",
                })
        for vid in sorted(self._heartbeat_lost):
            out.append({"type": "heartbeat_lost", "vehicle_id": vid,
                        "detail": "心跳丢失，确认位置前其占用资源不释放"})
        return out

    # ------------------------------------------------------------------
    # 不变量校验
    # ------------------------------------------------------------------

    def check_invariants(self) -> list[dict]:
        """安全不变量：任何资源在任何时刻不得超过容量。

        1) 预约窗口维度：活跃租约的 [start,end) 并发数 <= 容量
        2) 物理占用维度：已进入租约的 [entered_at, released_at] 并发数 <= 容量
        """
        violations: list[dict] = []
        by_resource: dict[str, list[Lease]] = {}
        for lease in self.leases.values():
            by_resource.setdefault(lease.resource_id, []).append(lease)
        for resource_id, leases in by_resource.items():
            res = self.topology.get(resource_id)
            capacity = res.capacity if res else 1
            window_points = []
            for l in leases:
                if l.state in ACTIVE_STATES:
                    window_points.append((l.start, 1, l.lease_id))
                    window_points.append((l.end, -1, l.lease_id))
            violations += self._sweep(resource_id, capacity, window_points,
                                      "window_overlap")
            physical_points = []
            for l in leases:
                if l.entered_at is None:
                    continue
                end = l.released_at if l.released_at is not None else float("inf")
                physical_points.append((l.entered_at, 1, l.lease_id))
                physical_points.append((end, -1, l.lease_id))
            violations += self._sweep(resource_id, capacity, physical_points,
                                      "physical_overlap")
        return violations

    @staticmethod
    def _sweep(resource_id, capacity, points, kind) -> list[dict]:
        # 左闭右开：同一时刻先离场后入场
        points.sort(key=lambda p: (p[0], p[1]))
        count, peak, at = 0, 0, None
        holders: set[str] = set()
        peak_holders: set[str] = set()
        for t, delta, lid in points:
            count += delta
            holders.add(lid) if delta > 0 else holders.discard(lid)
            if count > peak:
                peak, at, peak_holders = count, t, set(holders)
        if peak > capacity:
            return [{"type": kind, "resource_id": resource_id, "at": at,
                     "concurrency": peak, "capacity": capacity,
                     "leases": sorted(peak_holders)}]
        return []


# ----------------------------------------------------------------------
# 回放
# ----------------------------------------------------------------------

def replay(events: list[dict], heartbeat_ttl: float = 30.0) -> dict:
    """按命令事件重建状态机，逐步校验不变量。

    派生事件由同一套逻辑重新推导；任何“两车同时获准进入冲突资源”
    都会被不变量校验捕获。
    """
    topology = Topology()
    clock = ManualClock(0.0)
    coord = RightOfWayCoordinator(topology, clock, heartbeat_ttl=heartbeat_ttl)
    violations: list[dict] = []
    applied = 0
    for raw in events:
        ev = Event(seq=raw["seq"], at=raw["at"], type=raw["type"],
                   payload=raw["payload"])
        if not ev.is_command:
            continue
        clock.set(ev.at)
        coord.tick()  # 先推进该时刻的超时/死锁派生逻辑
        _apply_command(coord, ev)
        applied += 1
        for v in coord.check_invariants():
            violations.append({**v, "after_seq": ev.seq})
    return {
        "ok": not violations,
        "violations": violations,
        "commands_replayed": applied,
        "leases": {lid: l.to_dict() for lid, l in coord.leases.items()},
    }


def _apply_command(coord: RightOfWayCoordinator, ev: Event) -> None:
    p = ev.payload
    if ev.type == "segment_added":
        coord.add_segment(p["segment_id"], p["start_node"], p["end_node"],
                          p.get("length", 1.0), p.get("capacity", 1))
    elif ev.type == "intersection_added":
        coord.add_intersection(p["resource_id"], p.get("capacity", 1))
    elif ev.type == "fire_zone_added":
        coord.add_fire_zone(p["zone_id"], p["segments"], p["exits"])
    elif ev.type == "vehicle_registered":
        coord.register_vehicle(
            p["vehicle_id"], can_reverse=p.get("can_reverse", True),
            hazard_level=p.get("hazard_level", 0), battery=p.get("battery", 1.0),
            position=p.get("position"), node=p.get("node"),
        )
    elif ev.type == "task_assigned":
        coord.assign_task(p["vehicle_id"], p["task_id"], p["goal_node"],
                          route=p.get("route"), deadline=p.get("deadline"))
    elif ev.type == "reservation_requested":
        coord.request_reservation(p["request_id"], p["vehicle_id"],
                                  p["resource_id"], p["start"], p["end"])
    elif ev.type == "lease_entered":
        coord.enter(p["lease_id"], p["vehicle_id"])
    elif ev.type == "lease_released":
        coord.release(p["lease_id"], p["vehicle_id"])
    elif ev.type == "heartbeat_received":
        coord.heartbeat(p["vehicle_id"])
    elif ev.type == "position_confirmed":
        coord.confirm_position(p["vehicle_id"], segment_id=p.get("segment"),
                               node_id=p.get("node"))
    elif ev.type == "control_mode_changed":
        coord.set_control_mode(p["vehicle_id"], p["mode"])
    elif ev.type == "segment_closed":
        coord.close_segment(p["resource_id"])
    elif ev.type == "segment_opened":
        coord.open_segment(p["resource_id"])
    elif ev.type == "vehicle_fault":
        coord.vehicle_fault(p["vehicle_id"])
    else:  # pragma: no cover - 防御未知命令
        raise ValueError(f"未知命令事件: {ev.type}")
