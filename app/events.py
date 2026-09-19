"""事件日志：一切状态变更先落事件，可回放重建并校验安全不变量。

事件分两类：
- command：外部输入（注册车辆、预约申请、进入/释放、心跳、位置确认、
  控制模式切换、路段封闭、车辆故障、tick）。回放时按序重放。
- derived：协调器派生结论（授予、拒绝、抢占、uncertain、解环、重规划等）。
  回放时由同一套逻辑重新推导，不直接应用，保证回放可审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field


COMMAND_EVENTS = frozenset(
    {
        "segment_added",
        "intersection_added",
        "fire_zone_added",
        "vehicle_registered",
        "task_assigned",
        "reservation_requested",
        "lease_entered",
        "lease_released",
        "heartbeat_received",
        "position_confirmed",
        "control_mode_changed",
        "segment_closed",
        "segment_opened",
        "vehicle_fault",
        "tick",
    }
)


@dataclass
class Event:
    seq: int
    at: float
    type: str
    payload: dict = field(default_factory=dict)

    @property
    def is_command(self) -> bool:
        return self.type in COMMAND_EVENTS

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": self.at,
            "type": self.type,
            "kind": "command" if self.is_command else "derived",
            "payload": self.payload,
        }


class EventLog:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def record(self, at: float, type: str, **payload) -> Event:
        ev = Event(seq=len(self.events), at=at, type=type, payload=payload)
        self.events.append(ev)
        return ev

    def by_type(self, type: str) -> list[Event]:
        return [e for e in self.events if e.type == type]

    def to_list(self) -> list[dict]:
        return [e.to_dict() for e in self.events]
