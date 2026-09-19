"""车辆运行状态与事件溯源存储。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import ControlMode, Event, VehicleSpec


@dataclass
class VehicleRuntime:
    spec: VehicleSpec
    control: ControlMode = ControlMode.AUTOMATIC
    location: Optional[str] = None          # 最后确认所在资源
    location_confirmed_at: Optional[float] = None
    last_heartbeat: Optional[float] = None
    online: bool = True
    plan_id: Optional[str] = None
    progress: int = -1                      # 当前在路线中的下标，-1 表示尚未进入
    in_fire_zone: bool = False
    fire_exit: Optional[str] = None
    fault: Optional[str] = None

    @property
    def id(self) -> str:
        return self.spec.id


class Fleet:
    def __init__(self) -> None:
        self.vehicles: dict[str, VehicleRuntime] = {}

    def register(self, spec: VehicleSpec) -> VehicleRuntime:
        rt = VehicleRuntime(spec=spec)
        self.vehicles[spec.id] = rt
        return rt

    def get(self, vid: str) -> VehicleRuntime:
        return self.vehicles[vid]

    def __contains__(self, vid: str) -> bool:
        return vid in self.vehicles


class EventStore:
    """只追加事件日志，是服务所有状态变化的唯一去向，供审计与回放。"""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def append(self, ts: float, etype: str, payload: Optional[dict] = None) -> Event:
        ev = Event(seq=len(self.events) + 1, ts=ts, type=etype, payload=dict(payload or {}))
        self.events.append(ev)
        return ev

    def since(self, seq: int = 0) -> list[Event]:
        return [e for e in self.events if e.seq > seq]

    def all(self) -> list[Event]:
        return list(self.events)
