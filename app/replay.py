"""命令录制与确定性回放。

每个场景步骤记录 ``(时间, 动作, 参数)``；回放时在全新服务实例上重放，
并在**每一步之后**检查台账完整性：绝不允许两车同时持有冲突资源的有效租约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .clock import SimClock
from .service import RightOfWayService


@dataclass
class Step:
    t: float
    action: str
    kwargs: dict = field(default_factory=dict)
    note: str = ""


class Recorder:
    def __init__(self, service: RightOfWayService, clock: SimClock):
        self.service = service
        self.clock = clock
        self.steps: list[Step] = []

    def do(self, t: float, action: str, note: str = "", **kwargs) -> Any:
        self.clock.set(t)
        self.steps.append(Step(t, action, kwargs, note))
        return getattr(self.service, action)(**kwargs)

    def replay(self, factory: Callable[[], tuple[RightOfWayService, SimClock]],
               *, fail_fast: bool = True) -> dict:
        """``factory`` 返回 (全新服务, 仿真钟)；拓扑应在工厂内构建。"""
        svc, clock = factory()
        violations: list[dict] = []
        trace: list[dict] = []
        for i, step in enumerate(self.steps):
            clock.set(step.t)
            error = None
            try:
                getattr(svc, step.action)(**step.kwargs)
            except Exception as exc:  # noqa: BLE001 - 回放要记录全部失败
                error = f"{type(exc).__name__}: {exc}"
                if fail_fast:
                    raise
            bad = svc.integrity_violations()
            if bad:
                violations.append({"step": i, "t": step.t,
                                   "action": step.action, "violations": bad})
                if fail_fast:
                    raise AssertionError(f"step {i} {step.action}: {bad}")
            trace.append({"step": i, "t": step.t, "action": step.action,
                          "note": step.note, "error": error,
                          "violations": len(bad)})
        return {"steps": len(self.steps),
                "integrity_violations": violations,
                "trace": trace,
                "final_events": len(svc.events.all())}


def assert_no_conflicting_grants(events: list) -> list[dict]:
    """纯事件流审计：同一资源上两张"有效"租约的时间窗不得跨车重叠。

    有效区间按授权生命周期裁剪：提前释放/急停撤销/过期按事件时刻截短。
    """
    intervals: dict[str, list[tuple[float, float, str, str]]] = {}
    granted: dict[str, dict] = {}
    for e in events:
        p = e.payload
        if e.type == "lease_granted":
            granted[p["lease_id"]] = dict(p)
        elif e.type in ("lease_released", "lease_expired"):
            g = granted.get(p.get("lease_id"))
            if g is not None:
                g["end"] = min(g["end"], e.ts)
        elif e.type in ("lease_revoked_safe_stop", "lease_revoked_manual"):
            g = granted.get(p.get("lease_id"))
            if g is not None:
                g["end"] = e.ts  # 撤销时刻即失效
    for g in granted.values():
        if g["end"] <= g["start"]:
            continue
        intervals.setdefault(g["resource_id"], []).append(
            (g["start"], g["end"], g["vehicle_id"], g["lease_id"]))
    bad = []
    for rid, ws in intervals.items():
        for i, (a0, a1, av, al) in enumerate(ws):
            for b0, b1, bv, bl in ws[i + 1:]:
                if av != bv and a0 < b1 and b0 < a1:
                    bad.append({"resource": rid, "a": al, "b": bl})
    return bad
