"""领域模型：拓扑资源、车辆、租约、路线、事件。

时间窗一律采用左闭右开 ``[start, end)`` 语义。
租约状态与控制权模式与 ``domain_contract.json`` 保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---- 与 domain_contract.json 对齐的字面值 ----------------------------------

class LeaseState(str, Enum):
    REQUESTED = "requested"   # 已排队，尚未获准
    GRANTED = "granted"       # 已获准，车辆尚未进入
    ENTERED = "entered"       # 车辆已进入，物理占用中
    RELEASED = "released"     # 车辆确认离开，资源空闲
    UNCERTAIN = "uncertain"   # 心跳丢失/超时，占用状态待人工或定位确认
    EXPIRED = "expired"       # 授权超期且未进入（无物理占用，可直接回收）


ACTIVE_STATES = frozenset({LeaseState.GRANTED, LeaseState.ENTERED, LeaseState.UNCERTAIN})
OCCUPYING_STATES = frozenset({LeaseState.ENTERED, LeaseState.UNCERTAIN})


class ControlMode(str, Enum):
    AUTOMATIC = "automatic"
    SAFE_STOP = "safe_stop"
    MANUAL = "manual"


class RouteEventType(str, Enum):
    SEGMENT_CLOSED = "segment_closed"
    HEARTBEAT_LOST = "heartbeat_lost"
    VEHICLE_FAULT = "vehicle_fault"
    POSITION_CONFIRMED = "position_confirmed"


class ResourceKind(str, Enum):
    SEGMENT = "segment"
    INTERSECTION = "intersection"
    FIRE_ZONE = "fire_zone"   # 消防通道/消防区内部路段
    BAY = "bay"               # 避让位/会车区
    DEPOT = "depot"


@dataclass(frozen=True)
class Resource:
    id: str
    kind: ResourceKind
    zone: Optional[str] = None
    tags: frozenset[str] = field(default_factory=frozenset)
    closed: bool = False


@dataclass(frozen=True)
class FireZone:
    id: str
    resources: frozenset[str]
    exit_resource: str       # 指定出口路段


@dataclass
class VehicleSpec:
    id: str
    hazard: int = 0          # 载荷危险级 0 普通 .. 2 高危（易爆/易燃）
    battery: float = 100.0   # 剩余电量百分比
    deadline: Optional[float] = None   # 任务时限（绝对时间）
    capabilities: frozenset[str] = field(default_factory=frozenset)
    retreat_cost: float = 5.0          # 一次退让预计耗电（百分点）


@dataclass
class RoutePlan:
    """一条路线：资源序列 + 版本与决策理由（重规划时旧版本保留）。"""
    plan_id: str
    vehicle_id: str
    destination: str
    resources: tuple[str, ...]
    created_at: float
    status: str = "active"            # active | superseded
    reason: Optional[str] = None      # 被取代/生成的理由
    parent_id: Optional[str] = None

    def unpassed(self, progress: int) -> tuple[str, ...]:
        """progress 为当前所在资源下标；尚未通过的是其后的部分。"""
        return self.resources[progress + 1:]


@dataclass
class Reservation:
    lease_id: str
    request_id: str
    resource_id: str
    vehicle_id: str
    state: LeaseState
    start: float
    end: float
    created_at: float
    granted_at: Optional[float] = None
    entered_at: Optional[float] = None
    released_at: Optional[float] = None
    note: Optional[str] = None

    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    def window(self) -> tuple[float, float]:
        return self.start, self.end


@dataclass
class Denial:
    request_id: str
    vehicle_id: str
    resource_id: str
    reason: str
    detail: str
    ts: float
    outcome: str = "denied"           # denied | waiting


@dataclass
class Event:
    seq: int
    ts: float
    type: str
    payload: dict = field(default_factory=dict)


@dataclass
class Incident:
    incident_id: str
    kind: str                 # wait_cycle | heartbeat | closure | stranded | fire | takeover
    severity: str             # info | warning | critical
    ts: float
    vehicles: tuple[str, ...]
    summary: str
    suggested: list[dict] = field(default_factory=list)
    applied: Optional[dict] = None
    status: str = "open"      # open | resolved
    related_resources: tuple[str, ...] = ()

    def snapshot(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "severity": self.severity,
            "ts": self.ts,
            "status": self.status,
            "vehicles": list(self.vehicles),
            "summary": self.summary,
            "related_resources": list(self.related_resources),
            "suggested_actions": self.suggested,
            "applied_action": self.applied,
        }

