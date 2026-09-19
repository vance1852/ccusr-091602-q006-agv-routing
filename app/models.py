"""领域模型：租约、车辆、任务与枚举。

状态取值与 domain_contract.json 对齐：
- lease_states: requested / granted / entered / released / uncertain / expired
- control_modes: automatic / safe_stop / manual

预约时间窗一律采用左闭右开语义 [start, end)。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class LeaseState(str, enum.Enum):
    REQUESTED = "requested"    # 已申请，等待授予
    GRANTED = "granted"        # 已授予，车辆尚未进入
    ENTERED = "entered"        # 车辆已确认进入，物理占用中
    RELEASED = "released"      # 车辆已离开，资源释放
    UNCERTAIN = "uncertain"    # 心跳/窗口超时，位置待确认，资源不得释放
    EXPIRED = "expired"        # 确认车辆不在资源上（或从未进入），租约终结


# 持有资源的租约状态：这些状态下资源不可再授予他人。
ACTIVE_STATES = frozenset(
    {LeaseState.GRANTED, LeaseState.ENTERED, LeaseState.UNCERTAIN}
)


class ControlMode(str, enum.Enum):
    AUTOMATIC = "automatic"
    SAFE_STOP = "safe_stop"
    MANUAL = "manual"


class RejectReason(str, enum.Enum):
    UNKNOWN_VEHICLE = "unknown_vehicle"
    UNKNOWN_RESOURCE = "unknown_resource"
    RESOURCE_CLOSED = "resource_closed"
    INVALID_WINDOW = "invalid_window"
    CONTROL_MODE = "control_mode_not_automatic"
    VEHICLE_FAULT = "vehicle_fault"
    FIRE_ZONE_EXIT_ONLY = "fire_zone_exit_only"


@dataclass
class Lease:
    lease_id: str
    request_id: str          # 幂等键：通信重试凭它找回原预约
    vehicle_id: str
    resource_id: str
    start: float             # 左闭
    end: float               # 右开
    state: LeaseState = LeaseState.REQUESTED
    priority: float = 0.0
    granted_at: float | None = None
    entered_at: float | None = None
    released_at: float | None = None
    # 进入 uncertain 前的状态，位置确认后据此恢复。
    resume_state: LeaseState | None = None
    note: str = ""

    def overlaps(self, other: "Lease") -> bool:
        """左闭右开区间重叠判定：[10,20) 与 [20,30) 不冲突。"""
        return self.start < other.end and other.start < self.end

    def to_dict(self) -> dict:
        return {
            "lease_id": self.lease_id,
            "request_id": self.request_id,
            "vehicle_id": self.vehicle_id,
            "resource_id": self.resource_id,
            "start": self.start,
            "end": self.end,
            "state": self.state.value,
            "priority": round(self.priority, 3),
            "granted_at": self.granted_at,
            "entered_at": self.entered_at,
            "released_at": self.released_at,
            "note": self.note,
        }


@dataclass
class Rejection:
    request_id: str
    vehicle_id: str
    resource_id: str
    reason: str
    detail: str
    at: float

    def to_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "vehicle_id": self.vehicle_id,
            "resource_id": self.resource_id,
            "reason": self.reason,
            "detail": self.detail,
            "at": self.at,
        }


@dataclass
class RouteRevision:
    """一次局部重规划记录：保留被替换的尾段与原因。"""

    at: float
    reason: str
    old_tail: list[str]
    new_tail: list[str]

    def to_dict(self) -> dict:
        return {
            "at": self.at,
            "reason": self.reason,
            "old_tail": list(self.old_tail),
            "new_tail": list(self.new_tail),
        }


@dataclass
class Task:
    task_id: str
    goal_node: str
    route: list[str]                 # 路段 id 序列；已通过前缀不可改写
    deadline: float | None = None
    passed: int = 0                  # 已通过的路段数
    rationale: list[str] = field(default_factory=list)   # 决策理由
    revisions: list[RouteRevision] = field(default_factory=list)

    @property
    def remaining(self) -> list[str]:
        return self.route[self.passed:]

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal_node": self.goal_node,
            "route": list(self.route),
            "passed": self.passed,
            "remaining": self.remaining,
            "deadline": self.deadline,
            "rationale": list(self.rationale),
            "revisions": [r.to_dict() for r in self.revisions],
        }


@dataclass
class Vehicle:
    vehicle_id: str
    can_reverse: bool = True         # 退让可行性：能否倒车
    hazard_level: int = 0            # 载荷危险级 0(普通)..3(高危)
    battery: float = 1.0             # 剩余电量 0..1
    control_mode: ControlMode = ControlMode.AUTOMATIC
    position: str | None = None      # 最近确认所在资源（路段）
    node: str | None = None          # 最近确认所在节点
    last_heartbeat: float = 0.0
    fault: bool = False
    task: Task | None = None

    def to_dict(self) -> dict:
        return {
            "vehicle_id": self.vehicle_id,
            "can_reverse": self.can_reverse,
            "hazard_level": self.hazard_level,
            "battery": self.battery,
            "control_mode": self.control_mode.value,
            "position": self.position,
            "node": self.node,
            "last_heartbeat": self.last_heartbeat,
            "fault": self.fault,
            "task": self.task.to_dict() if self.task else None,
        }
