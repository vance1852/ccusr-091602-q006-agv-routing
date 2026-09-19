"""时间窗预约台账。

- 时间窗左闭右开：两预约冲突当且仅当 ``start < other.end and end > other.start``。
- ``entered`` / ``uncertain`` 表示物理占用或占用待证，绝不与任何车辆的有效租约共存。
- ``granted`` 是已发放的授权，同样参与冲突检测，防止重复发放。
"""

from __future__ import annotations

from typing import Optional

from .models import ACTIVE_STATES, LeaseState, OCCUPYING_STATES, Reservation


class Ledger:
    def __init__(self) -> None:
        self.leases: dict[str, Reservation] = {}
        self.request_index: dict[str, str] = {}   # request_id -> lease_id
        self._seq = 0

    def new_lease_id(self) -> str:
        self._seq += 1
        return f"L{self._seq:04d}"

    # ---- 基本存取 -----------------------------------------------------------

    def get(self, lease_id: str) -> Reservation:
        return self.leases[lease_id]

    def by_request(self, request_id: str) -> Optional[Reservation]:
        lid = self.request_index.get(request_id)
        return self.leases[lid] if lid else None

    def vehicle_leases(self, vehicle_id: str, states=None) -> list[Reservation]:
        states = states or ACTIVE_STATES
        return [l for l in self.leases.values()
                if l.vehicle_id == vehicle_id and l.state in states]

    def active_on(self, resource_id: str) -> list[Reservation]:
        return [l for l in self.leases.values()
                if l.resource_id == resource_id and l.active()]

    def occupying_on(self, resource_id: str) -> list[Reservation]:
        return [l for l in self.leases.values()
                if l.resource_id == resource_id and l.state in OCCUPYING_STATES]

    # ---- 状态机 -------------------------------------------------------------

    def open_request(self, request_id: str, vehicle_id: str, resource_id: str,
                     now: float) -> Reservation:
        lease_id = self.new_lease_id()
        lease = Reservation(
            lease_id=lease_id, request_id=request_id, resource_id=resource_id,
            vehicle_id=vehicle_id, state=LeaseState.REQUESTED,
            start=now, end=now, created_at=now,
        )
        self.leases[lease_id] = lease
        self.request_index[request_id] = lease_id
        return lease

    def grant(self, lease_id: str, start: float, end: float) -> Reservation:
        lease = self.leases[lease_id]
        if lease.state not in (LeaseState.REQUESTED,):
            raise IllegalTransition(f"lease {lease_id} cannot grant from {lease.state}")
        lease.state = LeaseState.GRANTED
        lease.start, lease.end, lease.granted_at = start, end, start
        return lease

    def enter(self, lease_id: str, now: float) -> Reservation:
        lease = self.leases[lease_id]
        if lease.state not in (LeaseState.GRANTED,):
            raise IllegalTransition(f"lease {lease_id} cannot enter from {lease.state}")
        lease.state = LeaseState.ENTERED
        lease.entered_at = now
        return lease

    def release(self, lease_id: str, now: float, note: Optional[str] = None) -> Reservation:
        lease = self.leases[lease_id]
        lease.state = LeaseState.RELEASED
        lease.released_at = now
        if note:
            lease.note = note
        return lease

    def mark_uncertain(self, lease_id: str, now: float, note: str) -> Reservation:
        lease = self.leases[lease_id]
        if lease.state == LeaseState.ENTERED:
            lease.state = LeaseState.UNCERTAIN
            lease.note = note
        return lease

    def confirm_entered(self, lease_id: str, now: float) -> Reservation:
        lease = self.leases[lease_id]
        if lease.state == LeaseState.UNCERTAIN:
            lease.state = LeaseState.ENTERED
            lease.note = None
        return lease

    def expire(self, lease_id: str, now: float) -> Reservation:
        """只有从未进入的 granted 授权可直接过期回收。"""
        lease = self.leases[lease_id]
        if lease.state != LeaseState.GRANTED:
            raise IllegalTransition(f"lease {lease_id} cannot expire from {lease.state}")
        lease.state = LeaseState.EXPIRED
        lease.released_at = now
        return lease

    # ---- 冲突检测 -----------------------------------------------------------

    def conflicts(self, resource_id: str, start: float, end: float,
                  *, exclude_lease: Optional[str] = None) -> list[Reservation]:
        out = []
        for l in self.active_on(resource_id):
            if l.lease_id == exclude_lease:
                continue
            if start < l.end and end > l.start:
                out.append(l)
        return out

    def integrity_violations(self) -> list[dict]:
        """全台账扫描：同一资源上不得存在相互重叠的不同车辆有效租约。"""
        bad = []
        by_res: dict[str, list[Reservation]] = {}
        for l in self.leases.values():
            if l.active():
                by_res.setdefault(l.resource_id, []).append(l)
        for rid, leases in by_res.items():
            for i, a in enumerate(leases):
                for b in leases[i + 1:]:
                    if a.vehicle_id == b.vehicle_id:
                        continue
                    if a.start < b.end and b.start < a.end:
                        bad.append({"resource": rid, "a": a.lease_id, "b": b.lease_id})
        return bad


class IllegalTransition(RuntimeError):
    pass
