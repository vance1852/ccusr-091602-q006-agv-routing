"""路权协调服务：租约发放、安全门控、等待环处置、重规划与态势查询。

不变量（任何代码路径都必须维持）：

1. 车辆只能凭 ``granted`` 且在时间窗内的租约进入资源；
2. 同一资源上，不同车辆的有效（granted/entered/uncertain）租约时间窗不得重叠；
3. ``entered``/``uncertain`` 代表物理占用或占用待证，超时只转 ``uncertain``，
   绝不直接释放——释放必须先有位置确认；
4. 安全急停/人工接管期间停止一切新授权；
5. 消防区内车辆的每一步都必须位于前往指定出口的路径上。
"""

from __future__ import annotations

import threading
from typing import Optional

from .clock import Clock, SystemClock
from .fleet import EventStore, Fleet, VehicleRuntime
from .ledger import Ledger
from .models import (
    ControlMode, Denial, Incident, LeaseState, Reservation,
    RouteEventType, RoutePlan, VehicleSpec,
)
from .policy import Cycle, Resolution, ResolutionPolicy, WaitGraph

REQUEST_ID_PREFIX = "REQ"


def _synchronized(fn):
    import functools
    import types

    if isinstance(fn, property):
        return fn
    if not isinstance(fn, types.FunctionType):
        return fn

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper


def synchronized_class(cls):
    """类装饰器：给公共方法统一加上实例级可重入锁。"""
    for name, fn in list(vars(cls).items()):
        if not name.startswith("_"):
            wrapped = _synchronized(fn)
            if wrapped is not fn:
                setattr(cls, name, wrapped)
    return cls


class RequestDenied(Exception):
    pass


@synchronized_class
class RightOfWayService:
    def __init__(self, topology, *, clock: Optional[Clock] = None,
                 grant_ttl: float = 30.0,
                 heartbeat_timeout: float = 12.0,
                 auto_resolve: bool = False):
        self.topo = topology
        self.clock = clock or SystemClock()
        self.fleet = Fleet()
        self.ledger = Ledger()
        self.events = EventStore()
        self.grant_ttl = grant_ttl
        self.heartbeat_timeout = heartbeat_timeout
        self.auto_resolve = auto_resolve

        self.plans: dict[str, list[RoutePlan]] = {}
        self.denials: list[Denial] = []
        self.incidents: list[Incident] = []
        self.directives: dict[str, dict] = {}      # vehicle_id -> 退让/撤离指令
        self._inc_seq = 0
        self._req_seq = 0
        self._denied_leases: set[str] = set()  # 硬拒绝的申请，不再自动提升
        self._lock = threading.RLock()
        self.policy = ResolutionPolicy(topology, self.ledger, self.fleet)

    @property
    def now(self) -> float:
        return self.clock.now()

    def _log(self, etype: str, payload: Optional[dict] = None):
        return self.events.append(self.now, etype, payload)

    def _new_incident_id(self) -> str:
        self._inc_seq += 1
        return f"INC{self._inc_seq:04d}"

    def _new_request_id(self) -> str:
        self._req_seq += 1
        return f"{REQUEST_ID_PREFIX}-{self.now:.0f}-{self._req_seq}"

    def _raise_incident(self, kind, severity, vehicles, summary,
                        suggested=None, related=()) -> Incident:
        inc = Incident(self._new_incident_id(), kind, severity, self.now,
                       tuple(vehicles), summary, suggested or [],
                       related_resources=tuple(related))
        self.incidents.append(inc)
        self._log("incident_opened", {"incident_id": inc.incident_id,
                                      "kind": kind, "vehicles": list(vehicles)})
        return inc

    # =====================================================================
    # 注册与路线
    # =====================================================================

    def register_vehicle(self, spec: VehicleSpec) -> dict:
        rt = self.fleet.register(spec)
        rt.last_heartbeat = self.now
        self._log("vehicle_registered", {"vehicle_id": spec.id,
                                         "hazard": spec.hazard})
        return {"vehicle_id": rt.id}

    def submit_plan(self, vehicle_id: str, *,
                    resources: Optional[tuple[str, ...]] = None,
                    destination: Optional[str] = None,
                    plan_kind: str = "mission",
                    reason: str = "初始任务路线") -> RoutePlan:
        rt = self.fleet.get(vehicle_id)
        if resources is None:
            if rt.location is None or destination is None:
                raise ValueError("需要显式 resources，或已知车辆位置与 destination")
            path = self.topo.shortest_path(
                rt.location, destination,
                capabilities=set(rt.spec.capabilities) or None)
            if path is None:
                raise RequestDenied(f"从 {rt.location} 到 {destination} 无可达路径")
            resources = tuple(path)
        else:
            resources = tuple(resources)
            for rid in resources:
                if rid not in self.topo.resources:
                    raise KeyError(f"未知资源 {rid}")

        old_active = self.active_plan(vehicle_id)
        if old_active is not None:
            old_active.status = "superseded"
            old_active.reason = f"被新计划取代：{reason}"
        plan = RoutePlan(
            plan_id=f"P{len(self.plans.get(vehicle_id, [])) + 1}-{vehicle_id}",
            vehicle_id=vehicle_id, destination=destination or resources[-1],
            resources=resources, created_at=self.now, reason=reason,
            parent_id=old_active.plan_id if old_active else None)
        self.plans.setdefault(vehicle_id, []).append(plan)
        rt.plan_id = plan.plan_id
        if rt.location is None:
            rt.progress = -1
        self._log("plan_submitted", {"vehicle_id": vehicle_id,
                                     "plan_id": plan.plan_id,
                                     "resources": list(resources),
                                     "kind": plan_kind,
                                     "reason": reason})
        return plan

    def active_plan(self, vehicle_id: str) -> Optional[RoutePlan]:
        for p in reversed(self.plans.get(vehicle_id, [])):
            if p.status == "active":
                return p
        return None

    def plan_history(self, vehicle_id: str) -> list[dict]:
        return [
            {"plan_id": p.plan_id, "status": p.status, "reason": p.reason,
             "parent_id": p.parent_id, "created_at": p.created_at,
             "destination": p.destination, "resources": list(p.resources)}
            for p in self.plans.get(vehicle_id, [])
        ]

    # =====================================================================
    # 租约申请（含通信重试幂等）
    # =====================================================================

    def request_resource(self, vehicle_id: str, resource_id: str,
                         request_id: Optional[str] = None,
                         duration: Optional[float] = None) -> dict:
        rt = self.fleet.get(vehicle_id)
        request_id = request_id or self._new_request_id()
        now = self.now

        # 通信重试：同一 request_id 永远返回原预约（无论它现在处于什么状态）
        existing = self.ledger.by_request(request_id)
        if existing is not None:
            resp = self._lease_response(existing, retry=True)
            if existing.lease_id in self._denied_leases:
                d = next((d for d in reversed(self.denials)
                          if d.request_id == request_id), None)
                if d is not None:
                    resp.update(status="denied", reason=d.reason,
                                detail=d.detail)
            elif existing.state == LeaseState.REQUESTED:
                d = next((d for d in reversed(self.denials)
                          if d.request_id == request_id), None)
                if d is not None:
                    resp["reason"] = d.detail
            return resp

        lease = self.ledger.open_request(request_id, vehicle_id, resource_id, now)
        self._log("lease_requested", {"request_id": request_id,
                                      "lease_id": lease.lease_id,
                                      "vehicle_id": vehicle_id,
                                      "resource_id": resource_id})

        denied = self._check_gates(rt, resource_id, grant=False)
        if denied is not None:
            self._denied_leases.add(lease.lease_id)
            return self._deny(lease, denied[0], denied[1])

        blocker = self._find_blocker(resource_id, vehicle_id)
        own = next((l for l in self.ledger.active_on(resource_id)
                    if l.vehicle_id == vehicle_id), None)
        if own is not None:
            # 本车已持有该资源的有效租约：不重复发放，返回既有租约状态
            resp = self._lease_response(own)
            resp["reason"] = "already_holds_lease"
            resp["detail"] = (
                f"本车已持有 {resource_id} 的 {own.state.value} 租约 "
                f"{own.lease_id}（原请求 {own.request_id}），请使用原租约")
            self._log("lease_duplicate_suppressed",
                      {"lease_id": own.lease_id,
                       "duplicate_request": request_id})
            return resp
        if blocker is not None:
            d = Denial(request_id, vehicle_id, resource_id,
                       "resource_occupied",
                       f"资源被 {blocker.vehicle_id} 的 {blocker.state.value} "
                       f"租约 {blocker.lease_id} 占用，已排队",
                       now, outcome="waiting")
            self.denials.append(d)
            self._log("lease_waiting", {"lease_id": lease.lease_id,
                                        "blocker_lease": blocker.lease_id,
                                        "blocker_vehicle": blocker.vehicle_id})
            resp = self._lease_response(lease)
            resp["reason"] = d.detail
            self.detect_situations()
            return resp

        self._grant(lease, duration)
        resp = self._lease_response(lease)
        self.detect_situations()
        return resp

    def _check_gates(self, rt: VehicleRuntime, resource_id: str,
                     grant: bool) -> Optional[tuple[str, str]]:
        # 安全急停与人工接管永远优先，不可被任何业务条件绕过
        if rt.control == ControlMode.SAFE_STOP:
            return "safe_stop", "全场/本车安全急停中，禁止新授权"
        if rt.control == ControlMode.MANUAL:
            return "manual_takeover", "车辆已被人工接管，调度不再发放租约"
        if rt.fault:
            return "vehicle_fault", f"车辆故障：{rt.fault}"
        if not rt.online:
            return "heartbeat_lost", "心跳丢失、实际位置待确认，暂停发放租约"
        res = self.topo.resources.get(resource_id)
        if res is None:
            return "unknown_resource", f"资源 {resource_id} 不存在"
        if res.closed:
            return "segment_closed", f"路段 {resource_id} 已封闭，禁止进入"
        need_caps = {t[4:] for t in res.tags if t.startswith("cap:")}
        if need_caps - rt.spec.capabilities:
            return "capability", (
                f"车辆缺少进入 {resource_id} 所需能力 "
                f"{sorted(need_caps - rt.spec.capabilities)}")
        # 只能申请相邻的下一资源（退让/撤离目标同样在拓扑上相邻）
        if rt.location is not None and resource_id != rt.location \
                and resource_id not in self.topo.neighbors(rt.location):
            return "not_adjacent", (
                f"{resource_id} 与当前位置 {rt.location} 不相邻，"
                "不能跨资源申请")
        if rt.in_fire_zone:
            zone = None
            if rt.location is not None:
                zone = self.topo.fire_zone_of(rt.location)
            if zone is None and rt.fire_exit is not None:
                zone = next((z for z in self.topo.fire_zones.values()
                             if z.exit_resource == rt.fire_exit), None)
            if zone is not None:
                path = self.topo.fire_evacuation_path(rt.location, zone) \
                    if rt.location else None
                allowed = set(path[1:]) if path else {zone.exit_resource}
                if resource_id not in allowed:
                    return "fire_zone_evacuation", (
                        f"车辆处于消防区 {zone.id}，只能沿指定出口 "
                        f"{zone.exit_resource} 撤离，{resource_id} 不在撤离路径上")
        return None

    def _find_blocker(self, resource_id: str, vehicle_id: str,
                      *, include_same_vehicle: bool = False
                      ) -> Optional[Reservation]:
        for l in self.ledger.active_on(resource_id):
            if l.vehicle_id != vehicle_id or include_same_vehicle:
                return l
        return None

    def _deny(self, lease: Reservation, reason: str, detail: str) -> dict:
        d = Denial(lease.request_id, lease.vehicle_id, lease.resource_id,
                   reason, detail, self.now, outcome="denied")
        self.denials.append(d)
        self._log("lease_denied", {"lease_id": lease.lease_id,
                                   "reason": reason, "detail": detail})
        return {"request_id": lease.request_id, "lease_id": lease.lease_id,
                "vehicle_id": lease.vehicle_id,
                "resource_id": lease.resource_id,
                "status": "denied", "state": lease.state.value,
                "reason": reason, "detail": detail}

    def _grant(self, lease: Reservation, duration: Optional[float] = None,
               note: Optional[str] = None) -> Reservation:
        end = self.now + (duration if duration is not None else self.grant_ttl)
        self.ledger.grant(lease.lease_id, self.now, end)
        if note:
            lease.note = note
        self._log("lease_granted", {"lease_id": lease.lease_id,
                                    "vehicle_id": lease.vehicle_id,
                                    "resource_id": lease.resource_id,
                                    "start": self.now, "end": end,
                                    "note": note})
        return lease

    def _lease_response(self, lease: Reservation, retry: bool = False) -> dict:
        status = {
            LeaseState.REQUESTED: "waiting",
            LeaseState.GRANTED: "granted",
            LeaseState.ENTERED: "entered",
            LeaseState.UNCERTAIN: "uncertain",
            LeaseState.RELEASED: "released",
            LeaseState.EXPIRED: "expired",
        }[lease.state]
        return {"request_id": lease.request_id, "lease_id": lease.lease_id,
                "vehicle_id": lease.vehicle_id,
                "resource_id": lease.resource_id,
                "status": status, "state": lease.state.value,
                "start": lease.start, "end": lease.end,
                "communication_retry": retry}

    # =====================================================================
    # 进入 / 心跳 / 释放
    # =====================================================================

    def enter_resource(self, vehicle_id: str, lease_id: str) -> dict:
        rt = self.fleet.get(vehicle_id)
        lease = self.ledger.get(lease_id)
        if lease.vehicle_id != vehicle_id:
            raise RequestDenied("租约不属于该车辆")
        if lease.state != LeaseState.GRANTED:
            raise RequestDenied(
                f"租约状态为 {lease.state.value}，只有 granted 租约可进入")
        if not (lease.start <= self.now < lease.end):
            raise RequestDenied("租约时间窗已失效，请重新申请")

        # 进入消防区/出口的状态翻转
        prev_location = rt.location
        in_zone_before = rt.in_fire_zone
        zone = self.topo.fire_zone_of(lease.resource_id)

        # 正常前进：先释放上一个 entered 资源（车辆物理离开）
        previous = [l for l in self.ledger.vehicle_leases(
            vehicle_id, frozenset({LeaseState.ENTERED}))]
        directive = self.directives.get(vehicle_id)

        self.ledger.enter(lease_id, self.now)
        rt.location = lease.resource_id
        rt.location_confirmed_at = self.now
        rt.last_heartbeat = self.now

        if directive and lease.resource_id == directive["retreat_to"]:
            if directive.get("type") == "retreat":
                held = directive["releases"]
                old = next((l for l in previous
                            if l.resource_id == held
                            and l.state == LeaseState.ENTERED), None)
                if old is not None:
                    self.ledger.release(old.lease_id, self.now,
                                        note="车辆已退至避让位，确认离开后释放")
                    self._log("lease_released", {"lease_id": old.lease_id,
                                                 "reason": "retreat_confirmed"})
                self._close_cycle_incidents(vehicle_id)
            else:
                # 撤离前进一步：正常释放身后资源
                for old in previous:
                    self.ledger.release(old.lease_id, self.now,
                                        note="沿指定出口撤离")
                    self._log("lease_released", {"lease_id": old.lease_id,
                                                 "reason": "evacuation_move"})
            self.directives.pop(vehicle_id, None)
        else:
            for old in previous:
                self.ledger.release(old.lease_id, self.now,
                                    note="车辆进入下一资源")
                self._log("lease_released", {"lease_id": old.lease_id,
                                             "reason": "forward_move"})

        if zone is not None and not in_zone_before:
            rt.in_fire_zone = True
            rt.fire_exit = zone.exit_resource
            self._log("fire_zone_entered", {"vehicle_id": vehicle_id,
                                            "zone": zone.id})
        if in_zone_before and lease.resource_id == rt.fire_exit:
            rt.in_fire_zone = False
            self._log("fire_zone_exited", {"vehicle_id": vehicle_id,
                                           "exit": rt.fire_exit})
            rt.fire_exit = None

        plan = self.active_plan(vehicle_id)
        if plan is not None:
            res = plan.resources
            nxt = rt.progress + 1
            if 0 <= nxt < len(res) and res[nxt] == lease.resource_id:
                rt.progress = nxt
            elif lease.resource_id in res:
                rt.progress = res.index(lease.resource_id)

        self._log("resource_entered", {"vehicle_id": vehicle_id,
                                       "lease_id": lease_id,
                                       "resource_id": lease.resource_id,
                                       "previous": prev_location})
        self._promote_waiting()
        self.detect_situations()
        return self._lease_response(lease)

    def enter_by_request(self, vehicle_id: str, request_id: str) -> dict:
        lease = self.ledger.by_request(request_id)
        if lease is None:
            raise RequestDenied(f"未知 request_id {request_id}")
        return self.enter_resource(vehicle_id, lease.lease_id)

    def heartbeat(self, vehicle_id: str, position: str) -> dict:
        rt = self.fleet.get(vehicle_id)
        rt.online = True
        rt.last_heartbeat = self.now

        uncertain = [l for l in self.ledger.vehicle_leases(
            vehicle_id, frozenset({LeaseState.UNCERTAIN}))]
        if uncertain:
            on = next((l for l in uncertain if l.resource_id == position), None)
            if on is not None:
                self.ledger.confirm_entered(on.lease_id, self.now)
                rt.location = position
                rt.location_confirmed_at = self.now
                self._log(RouteEventType.POSITION_CONFIRMED.value,
                          {"vehicle_id": vehicle_id, "resource_id": position,
                           "lease_id": on.lease_id,
                           "result": "still_occupied"})
                self._note_incident_resolved("heartbeat", vehicle_id,
                                             "心跳恢复，位置确认仍在占用")
            else:
                self._log("heartbeat_received", {"vehicle_id": vehicle_id,
                                                 "position": position,
                                                 "result": "position_mismatch"})
        else:
            active = self.ledger.vehicle_leases(
                vehicle_id, frozenset({LeaseState.ENTERED}))
            on = next((l for l in active if l.resource_id == position), None)
            if on is not None:
                on.end = max(on.end, self.now + self.grant_ttl)
                rt.location = position
                rt.location_confirmed_at = self.now
            self._log("heartbeat_received", {"vehicle_id": vehicle_id,
                                             "position": position})
        self.detect_situations()
        return {"vehicle_id": vehicle_id, "position": rt.location,
                "online": True, "control": rt.control.value}

    def confirm_position(self, vehicle_id: str, resource_id: str,
                         occupied: bool) -> dict:
        """人工/定位系统确认车辆真实位置后才能处置 uncertain 租约。"""
        rt = self.fleet.get(vehicle_id)
        leases = self.ledger.vehicle_leases(
            vehicle_id, frozenset({LeaseState.UNCERTAIN, LeaseState.ENTERED}))
        target = next((l for l in leases if l.resource_id == resource_id), None)
        if target is None:
            raise RequestDenied("该资源上没有本车的占用/待证租约")
        rt.location = resource_id
        rt.location_confirmed_at = self.now
        if occupied:
            if target.state == LeaseState.UNCERTAIN:
                self.ledger.confirm_entered(target.lease_id, self.now)
            self._log(RouteEventType.POSITION_CONFIRMED.value,
                      {"vehicle_id": vehicle_id, "resource_id": resource_id,
                       "result": "still_occupied"})
            self._note_incident_resolved("heartbeat", vehicle_id,
                                         "位置确认：车辆仍占用，租约恢复 entered")
        else:
            # 只有确认无物理占用时才允许释放
            self.ledger.release(target.lease_id, self.now,
                                note="人工确认车辆已不在该资源")
            rt.location = None
            self._log("lease_released", {"lease_id": target.lease_id,
                                         "reason": "position_confirmed_empty"})
            self._log(RouteEventType.POSITION_CONFIRMED.value,
                      {"vehicle_id": vehicle_id, "resource_id": resource_id,
                       "result": "confirmed_empty_released"})
            self._note_incident_resolved("heartbeat", vehicle_id,
                                         "位置确认：资源已空，安全释放")
            self._promote_waiting()
        self.detect_situations()
        return self._lease_response(target)

    def release_lease(self, vehicle_id: str, lease_id: str) -> dict:
        lease = self.ledger.get(lease_id)
        if lease.vehicle_id != vehicle_id:
            raise RequestDenied("租约不属于该车辆")
        if lease.state not in (LeaseState.ENTERED, LeaseState.GRANTED):
            raise RequestDenied(f"租约状态 {lease.state.value} 不可主动释放")
        self.ledger.release(lease_id, self.now, note="车辆报告离开")
        rt = self.fleet.get(vehicle_id)
        if rt.location == lease.resource_id:
            rt.location = None
        self._log("lease_released", {"lease_id": lease_id,
                                     "reason": "reported_exit"})
        self._promote_waiting()
        self.detect_situations()
        return self._lease_response(lease)

    # =====================================================================
    # 周期扫描：超时、丢心跳、排队提升
    # =====================================================================

    def tick(self) -> dict:
        now = self.now
        expired, lost = [], []
        for lease in list(self.ledger.leases.values()):
            if lease.state == LeaseState.GRANTED and lease.end <= now:
                # 从未进入的授权可安全回收；占用类租约不走这条路径
                self.ledger.expire(lease.lease_id, now)
                expired.append(lease.lease_id)
                self._log("lease_expired", {"lease_id": lease.lease_id,
                                            "resource_id": lease.resource_id})
        for vid, rt in self.fleet.vehicles.items():
            if not rt.online or rt.control != ControlMode.AUTOMATIC:
                continue
            entered = self.ledger.vehicle_leases(
                vid, frozenset({LeaseState.ENTERED}))
            if entered and rt.last_heartbeat is not None and \
                    now - rt.last_heartbeat > self.heartbeat_timeout:
                rt.online = False
                for l in entered:
                    self.ledger.mark_uncertain(
                        l.lease_id, now,
                        f"心跳丢失 {now - rt.last_heartbeat:.0f}s，位置待确认")
                    lost.append(l.lease_id)
                self._log(RouteEventType.HEARTBEAT_LOST.value,
                          {"vehicle_id": vid,
                           "leases": [l.lease_id for l in entered],
                           "silence_s": round(now - rt.last_heartbeat, 1)})
                self._raise_incident(
                    "heartbeat", "critical", [vid],
                    f"车辆 {vid} 心跳丢失，占用路段转为待确认，禁止释放",
                    [{"action": "confirm_position",
                      "hint": "先核实车辆实际位置，再决定恢复 entered 或确认清空后释放"},
                     {"action": "manual_assist",
                      "hint": "现场无法确认时派人工处置"}],
                    related=tuple(l.resource_id for l in entered))
        if expired:
            self._promote_waiting()
        self.detect_situations()
        return {"ts": now, "expired": expired, "heartbeat_lost": lost}

    def _promote_waiting(self) -> None:
        waiting = sorted(
            (l for l in self.ledger.leases.values()
             if l.state == LeaseState.REQUESTED),
            key=lambda l: (l.created_at, l.lease_id))
        for lease in waiting:
            if lease.lease_id in self._denied_leases:
                continue
            rt = self.fleet.vehicles.get(lease.vehicle_id)
            if rt is None:
                continue
            denied = self._check_gates(rt, lease.resource_id, grant=True)
            if denied is not None:
                continue
            if self._find_blocker(lease.resource_id, lease.vehicle_id) is not None:
                continue
            self._grant(lease)

    # =====================================================================
    # 等待环 / 优先级反转检测与解环
    # =====================================================================

    def _waiting_requests(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for l in self.ledger.leases.values():
            if l.state == LeaseState.REQUESTED \
                    and l.lease_id not in self._denied_leases:
                out.setdefault(l.vehicle_id, l.resource_id)
        return out

    def wait_graph(self) -> dict:
        return WaitGraph(self.ledger, self.fleet).rebuild(
            self._waiting_requests()).snapshot()

    def detect_situations(self) -> None:
        graph = WaitGraph(self.ledger, self.fleet).rebuild(
            self._waiting_requests())

        # 1) 优先级反转：高优先级车被低优先级车挡住（仅告警，绝不抢占已发租约）
        for e in graph.edges:
            w, b = self.fleet.vehicles[e.waiter], self.fleet.vehicles[e.blocker]
            if self._priority(w) > self._priority(b) + 1:
                if not any(inc.status == "open" and inc.kind == "priority_inversion"
                           and inc.vehicles == (e.waiter, e.blocker)
                           for inc in self.incidents):
                    self._raise_incident(
                        "priority_inversion", "info",
                        [e.waiter, e.blocker],
                        f"高优先级车 {e.waiter} 等待低优先级车 {e.blocker}；"
                        "已发租约不可抢占，禁止用堵塞消防通道的方式提权",
                        [{"action": "wait", "hint": "保持排队，等待租约正常释放"},
                         {"action": "resolve_cycle",
                          "hint": "若构成等待环则按解环策略处置"}],
                        related=(e.resource,))

        # 2) 等待环
        for cycle in graph.cycles():
            sig = tuple(sorted(cycle.vehicles))
            if any(inc.status == "open" and inc.kind == "wait_cycle"
                   and tuple(sorted(inc.vehicles)) == sig
                   for inc in self.incidents):
                continue
            plans = {}
            for vid in cycle.vehicles:
                p = self.active_plan(vid)
                rt = self.fleet.get(vid)
                plans[vid] = (p.resources if p else (), rt.progress)
            res = self.policy.resolve(cycle, plans, self.now)
            self._open_cycle_incident(cycle, res)
            if self.auto_resolve and res.action == "retreat":
                self.apply_resolution(self.incidents[-1].incident_id)

    def _priority(self, rt: VehicleRuntime) -> int:
        p = rt.spec.hazard * 10
        if rt.spec.deadline is not None:
            slack = rt.spec.deadline - self.now
            if slack < 60:
                p += 8
            elif slack < 180:
                p += 4
        if rt.spec.battery < 25:
            p += 2
        return p

    def _open_cycle_incident(self, cycle: Cycle, res: Resolution) -> Incident:
        suggestions = []
        if res.action == "retreat":
            suggestions = [
                {"action": "retreat",
                 "vehicle_id": res.target_vehicle,
                 "retreat_to": res.retreat_to,
                 "releases": res.releases,
                 "hint": ("指令该车退入避让位，确认其进入避让位后才释放原占用路段")},
                {"action": "human_assist", "hint": "也可改为现场人工处置"}]
        elif res.action == "evacuate_fire_zone":
            suggestions = [
                {"action": "evacuate_fire_zone",
                 "vehicle_id": res.target_vehicle,
                 "designated_exit": res.retreat_to,
                 "hint": "消防区内只能顺指定出口撤离，协调对向车让行"},
                {"action": "human_assist", "hint": "立即通知现场值守员"}]
        else:
            suggestions = [
                {"action": "human_assist",
                 "hint": "环上无安全自动解环动作，需要人工牵引或封控"}]
        resources = tuple(sorted({e.resource for e in cycle.edges}))
        inc = self._raise_incident(
            "wait_cycle", "critical", list(cycle.vehicles),
            f"检测到等待环 {' -> '.join(cycle.vehicles + cycle.vehicles[:1])}；"
            f"决策：{res.rationale}",
            suggested=suggestions, related=resources)
        inc.applied = None
        self._log("cycle_resolution_proposed",
                  {"incident_id": inc.incident_id, "action": res.action,
                   "target": res.target_vehicle, "scores": res.scores,
                   "rationale": res.rationale})
        inc.suggested = suggestions
        inc.applied = {"proposal": {
            "action": res.action, "target": res.target_vehicle,
            "retreat_to": res.retreat_to, "releases": res.releases,
            "rationale": res.rationale, "scores": res.scores}}
        return inc

    def apply_resolution(self, incident_id: str,
                         action: Optional[str] = None) -> dict:
        inc = next(i for i in self.incidents if i.incident_id == incident_id)
        if inc.status != "open":
            return {"incident_id": incident_id, "status": inc.status}
        proposal = (inc.applied or {}).get("proposal", {})
        action = action or proposal.get("action")
        if action == "human_assist":
            inc.status = "resolved"
            inc.applied = {"action": "human_assist", "ts": self.now}
            self._log("incident_resolved", {"incident_id": incident_id,
                                            "action": "human_assist"})
            return {"incident_id": incident_id, "status": "resolved"}

        if action == "retreat":
            vid = proposal["target"]
            rt = self.fleet.get(vid)
            retreat_to, releases = proposal["retreat_to"], proposal["releases"]
            if self._find_blocker(retreat_to, vid) is not None:
                raise RequestDenied("避让位此刻非空闲，无法下达退让指令")
            # 为退让发放专用租约；原路段在车辆确认进入避让位之前保持占用
            lease = self.ledger.open_request(
                self._new_request_id(), vid, retreat_to, self.now)
            self._grant(lease, note="解环退让专用租约")
            self.directives[vid] = {"type": "retreat",
                                    "retreat_to": retreat_to,
                                    "releases": releases}
            inc.applied = {"action": "retreat", "vehicle_id": vid,
                           "retreat_lease": lease.lease_id,
                           "releases_after_confirm": releases,
                           "ts": self.now}
            self._log("resolution_applied", {"incident_id": incident_id,
                                             "action": "retreat",
                                             "vehicle_id": vid,
                                             "lease_id": lease.lease_id})
            return {"incident_id": incident_id, "status": "retreat_directed",
                    "lease": self._lease_response(lease)}

        if action == "evacuate_fire_zone":
            vid = proposal["target"]
            rt = self.fleet.get(vid)
            zone = next((z for z in self.topo.fire_zones.values()
                         if rt.location in z.resources), None)
            if zone is None:
                raise RequestDenied("车辆已不在消防区")
            path = self.topo.fire_evacuation_path(rt.location, zone)
            if not path or len(path) < 2:
                raise RequestDenied("撤离路径当前不可用，需人工处置")
            nxt = path[1]
            if self._find_blocker(nxt, vid) is not None:
                return {"incident_id": incident_id,
                        "status": "evacuation_blocked",
                        "hint": f"撤离下一步 {nxt} 被占用，需对向车辆让行"}
            lease = self.ledger.open_request(
                self._new_request_id(), vid, nxt, self.now)
            self._grant(lease, note="消防撤离专用租约")
            self.directives[vid] = {"type": "evacuate", "retreat_to": nxt}
            inc.applied = {"action": "evacuate_fire_zone", "vehicle_id": vid,
                           "lease_id": lease.lease_id, "exit": zone.exit_resource,
                           "ts": self.now}
            self._log("resolution_applied", {"incident_id": incident_id,
                                             "action": "evacuate_fire_zone",
                                             "vehicle_id": vid})
            return {"incident_id": incident_id, "status": "evacuation_directed",
                    "lease": self._lease_response(lease)}

        raise RequestDenied(f"不支持的解环动作 {action}")

    def _close_cycle_incidents(self, vehicle_id: str) -> None:
        graph = WaitGraph(self.ledger, self.fleet).rebuild(
            self._waiting_requests())
        sigs = {tuple(sorted(c.vehicles)) for c in graph.cycles()}
        for inc in self.incidents:
            if inc.kind == "wait_cycle" and inc.status == "open" \
                    and tuple(sorted(inc.vehicles)) not in sigs:
                inc.status = "resolved"
                self._log("incident_resolved",
                          {"incident_id": inc.incident_id,
                           "action": "cycle_broken"})

    def _note_incident_resolved(self, kind: str, vehicle_id: str, note: str):
        for inc in self.incidents:
            if inc.kind == kind and inc.status == "open" \
                    and vehicle_id in inc.vehicles:
                inc.status = "resolved"
                inc.applied = {"note": note, "ts": self.now}
                self._log("incident_resolved",
                          {"incident_id": inc.incident_id, "note": note})

    # =====================================================================
    # 安全：急停 / 人工接管
    # =====================================================================

    def safe_stop(self, vehicle_id: Optional[str] = None,
                  reason: str = "安全急停") -> dict:
        targets = ([self.fleet.get(vehicle_id)] if vehicle_id
                   else list(self.fleet.vehicles.values()))
        affected = []
        for rt in targets:
            rt.control = ControlMode.SAFE_STOP
            for l in self.ledger.vehicle_leases(rt.id,
                                                frozenset({LeaseState.GRANTED})):
                self.ledger.expire(l.lease_id, self.now)
                affected.append(l.lease_id)
                self._log("lease_revoked_safe_stop", {"lease_id": l.lease_id})
            self._log("safe_stop", {"vehicle_id": rt.id, "reason": reason})
            self._raise_incident(
                "takeover", "critical", [rt.id],
                f"安全急停：{rt.id}（{reason}）；已占用租约保持，待人工确认",
                [{"action": "manual_assist", "hint": "现场排除危险后人工恢复"}],
                related=tuple(l.resource_id for l in self.ledger.vehicle_leases(
                    rt.id, frozenset({LeaseState.ENTERED, LeaseState.UNCERTAIN}))))
        return {"safe_stop": True, "revoked_grants": affected,
                "vehicles": [rt.id for rt in targets]}

    def manual_takeover(self, vehicle_id: str, reason: str = "人工接管") -> dict:
        rt = self.fleet.get(vehicle_id)
        rt.control = ControlMode.MANUAL
        revoked = []
        for l in self.ledger.vehicle_leases(vehicle_id,
                                            frozenset({LeaseState.GRANTED})):
            self.ledger.expire(l.lease_id, self.now)
            revoked.append(l.lease_id)
            self._log("lease_revoked_manual", {"lease_id": l.lease_id})
        held = self.ledger.vehicle_leases(
            vehicle_id, frozenset({LeaseState.ENTERED, LeaseState.UNCERTAIN}))
        suggestion = ([{"action": "evacuate_fire_zone",
                        "hint": f"沿指定出口 {rt.fire_exit} 撤离"}]
                      if rt.in_fire_zone else
                      [{"action": "await_human", "hint": "等待人工驾驶，租约暂停发放"}])
        self._raise_incident(
            "takeover", "critical", [vehicle_id],
            f"车辆 {vehicle_id} 被人工接管（{reason}），调度让权",
            suggestion, related=tuple(l.resource_id for l in held))
        self._log("manual_takeover", {"vehicle_id": vehicle_id, "reason": reason})
        return {"vehicle_id": vehicle_id, "control": "manual",
                "revoked_grants": revoked}

    def resume_automatic(self, vehicle_id: str) -> dict:
        rt = self.fleet.get(vehicle_id)
        rt.control = ControlMode.AUTOMATIC
        rt.online = True
        rt.last_heartbeat = self.now
        rt.fault = None
        self._log("automatic_resumed", {"vehicle_id": vehicle_id})
        self._promote_waiting()
        self.detect_situations()
        return {"vehicle_id": vehicle_id, "control": "automatic"}

    # =====================================================================
    # 封闭 / 故障：只重规划尚未通过的路段
    # =====================================================================

    def close_segment(self, resource_id: str,
                      reason: str = "地图封闭") -> dict:
        self.topo.close(resource_id)
        self._log(RouteEventType.SEGMENT_CLOSED.value,
                  {"resource_id": resource_id, "reason": reason})
        replanned, stranded = [], []
        for vid, rt in list(self.fleet.vehicles.items()):
            plan = self.active_plan(vid)
            if plan is None:
                continue
            if rt.location == resource_id:
                self._raise_incident(
                    "stranded", "critical", [vid],
                    f"车辆 {vid} 正处于封闭路段 {resource_id} 内，需撤离",
                    [{"action": "evacuate", "hint": "开放出口或人工牵引"}],
                    related=(resource_id,))
                stranded.append(vid)
                continue
            tail = plan.unpassed(rt.progress)
            if resource_id not in tail:
                continue
            new_resources = self._replan(rt, plan, reason, resource_id)
            if new_resources is None:
                stranded.append(vid)
            else:
                replanned.append(vid)
        self._raise_incident(
            "closure", "warning",
            [v for v in self.plans], f"路段 {resource_id} 封闭：{reason}",
            [{"action": "see_plans",
              "hint": f"重规划 {len(replanned)} 台，受阻 {len(stranded)} 台"}],
            related=(resource_id,))
        return {"closed": resource_id, "replanned": replanned,
                "stranded": stranded}

    def vehicle_fault(self, vehicle_id: str, reason: str = "车辆故障") -> dict:
        rt = self.fleet.get(vehicle_id)
        rt.fault = reason
        rt.online = False
        blocked_resources = []
        for l in self.ledger.vehicle_leases(
                vehicle_id, frozenset({LeaseState.ENTERED})):
            self.ledger.mark_uncertain(l.lease_id, self.now, f"故障：{reason}")
            blocked_resources.append(l.resource_id)
        for l in self.ledger.vehicle_leases(
                vehicle_id, frozenset({LeaseState.GRANTED})):
            self.ledger.expire(l.lease_id, self.now)
        self._log(RouteEventType.VEHICLE_FAULT.value,
                  {"vehicle_id": vehicle_id, "reason": reason,
                   "blocked_resources": blocked_resources})
        self._raise_incident(
            "stranded", "critical", [vehicle_id],
            f"车辆 {vehicle_id} 故障：{reason}；其占用路段保持待确认，禁止释放",
            [{"action": "confirm_position", "hint": "确认实际位置"},
             {"action": "human_assist", "hint": "现场救援/牵引"}],
            related=tuple(blocked_resources))

        replanned = []
        for vid, other in list(self.fleet.vehicles.items()):
            if vid == vehicle_id:
                continue
            plan = self.active_plan(vid)
            if plan is None:
                continue
            tail = plan.unpassed(other.progress)
            if any(r in blocked_resources for r in tail):
                if self._replan(rt=other, plan=plan,
                                reason=f"{vehicle_id} 故障封路",
                                trigger=blocked_resources[0]) is not None:
                    replanned.append(vid)
        return {"faulty": vehicle_id, "blocked_resources": blocked_resources,
                "others_replanned": replanned}

    def _blocked_for_routing(self, exclude_vehicle: str) -> set[str]:
        """重规划时视为不可通过的动态资源：占用待确认（丢心跳/故障）。

        封闭路段由拓扑层直接排除；正常行驶中的占用交给租约门控排队，
        不参与绕行判断，以免误判不可达。
        """
        return {l.resource_id for l in self.ledger.leases.values()
                if l.state == LeaseState.UNCERTAIN
                and l.vehicle_id != exclude_vehicle}

    def _replan(self, rt: VehicleRuntime, plan: RoutePlan,
                reason: str, trigger: str) -> Optional[tuple[str, ...]]:
        """仅重规划尚未通过的路段；原路线标记 superseded 并保留理由。"""
        caps = set(rt.spec.capabilities)
        origin = rt.location
        started = origin is not None
        if not started:
            origin = plan.resources[0]      # 尚未出发：从起点 depot 重规划
        forbid = self._blocked_for_routing(rt.id)
        path = self.topo.shortest_path(origin, plan.destination,
                                       forbid=forbid,
                                       capabilities=caps or None)
        if path is None:
            self._raise_incident(
                "stranded", "critical", [rt.id],
                f"车辆 {rt.id} 无绕行路径（{reason}），原路线保留待人工",
                [{"action": "human_assist"}], related=(trigger,))
            plan.status = "blocked"
            plan.reason = f"重规划失败：{reason}；原路线保留"
            self._log("replan_failed", {"vehicle_id": rt.id,
                                        "plan_id": plan.plan_id,
                                        "trigger": trigger})
            return None
        new_resources = tuple(path)
        plan.status = "superseded"
        plan.reason = f"仅重规划未通过路段（触发：{reason}）；原路线已归档"
        new_plan = RoutePlan(
            plan_id=f"P{len(self.plans.get(rt.id, [])) + 1}-{rt.id}",
            vehicle_id=rt.id, destination=plan.destination,
            resources=new_resources, created_at=self.now,
            reason=(f"由 {plan.plan_id} 因 {reason} 重规划；"
                    + (f"已通过的 {rt.progress + 1} 个资源保持不变"
                       if started else "车辆尚未出发，从起点重新规划")),
            parent_id=plan.plan_id)
        self.plans.setdefault(rt.id, []).append(new_plan)
        rt.plan_id = new_plan.plan_id
        rt.progress = 0 if started else -1
        self._log("plan_replanned", {"vehicle_id": rt.id,
                                     "old_plan": plan.plan_id,
                                     "new_plan": new_plan.plan_id,
                                     "resources": list(new_resources),
                                     "reason": reason})
        return new_resources

    # =====================================================================
    # 值守员态势查询
    # =====================================================================

    def timeline(self, resource_id: Optional[str] = None) -> list[dict]:
        rows = []
        for l in self.ledger.leases.values():
            if resource_id and l.resource_id != resource_id:
                continue
            if l.state in (LeaseState.REQUESTED,):
                continue
            rows.append({"lease_id": l.lease_id, "request_id": l.request_id,
                         "resource_id": l.resource_id,
                         "vehicle_id": l.vehicle_id,
                         "state": l.state.value,
                         "start": l.start, "end": l.end,
                         "granted_at": l.granted_at,
                         "entered_at": l.entered_at,
                         "released_at": l.released_at,
                         "note": l.note})
        rows.sort(key=lambda r: (r["resource_id"], r["start"], r["lease_id"]))
        return rows

    def denials_view(self) -> list[dict]:
        return [{"request_id": d.request_id, "vehicle_id": d.vehicle_id,
                 "resource_id": d.resource_id, "reason": d.reason,
                 "detail": d.detail, "outcome": d.outcome, "ts": d.ts}
                for d in self.denials]

    def incidents_view(self) -> list[dict]:
        return [i.snapshot() for i in self.incidents]

    def vehicles_view(self) -> list[dict]:
        out = []
        for vid, rt in self.fleet.vehicles.items():
            out.append({
                "vehicle_id": vid, "control": rt.control.value,
                "online": rt.online, "fault": rt.fault,
                "location": rt.location,
                "location_confirmed_at": rt.location_confirmed_at,
                "last_heartbeat": rt.last_heartbeat,
                "hazard": rt.spec.hazard, "battery": rt.spec.battery,
                "deadline": rt.spec.deadline,
                "in_fire_zone": rt.in_fire_zone,
                "fire_exit": rt.fire_exit,
                "plan_id": rt.plan_id, "progress": rt.progress,
                "priority": self._priority(rt),
                "holds": [l.resource_id for l in self.ledger.vehicle_leases(vid)],
            })
        return out

    def integrity_violations(self) -> list[dict]:
        return self.ledger.integrity_violations()
